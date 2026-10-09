"""Проверка файлов конвейера: формат каждого файла и согласованность между ними.

Запуск:
    python check_data.py            # проверяет всё, что найдено в текущей папке
    python check_data.py --dir demo_out
    python check_data.py --only-prs # только prs.json, пока остального ещё нет

Файлы передаются между людьми, поэтому проверяется именно договорённость:
    prs.json       поля и типы, значения ci, нет повторов номеров
    scores.json    четыре критерия, баллы 1–5, причина у каждого балла, каждая ссылка
                   указывает на файл из diff этого PR, каждый PR оценён
    outcomes.json  номера из prs.json, флаги согласованы со списками
    change_types.json  номера из prs.json, тип из списка classify.py, причина у каждого типа
    metrics.json   поля, уровень, множитель в разрешённом диапазоне, и главное:
                   пересчёт из текущих prs.json и scores.json даёт те же числа
                   (иначе файл устарел и дашборд показывает вчерашнее)

Код выхода 0, если ошибок нет (предупреждения допустимы), иначе 1.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import metrics as metrics_module
from common import CI_VALUES, CRITERIA, HERE, LEVELS, DataError, parse_time, read_json, setup_output, use_data_dir

LINES = re.compile(r"^(\d+(-\d+)?)?$")


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, where, message):
        self.errors.append(f"{where}: {message}")

    def warn(self, where, message):
        self.warnings.append(f"{where}: {message}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_time(value) -> bool:
    try:
        parse_time(value)
        return isinstance(value, str)
    except (ValueError, TypeError):
        return False


def _records(data, name, rep):
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        rep.error(name, "должен быть JSON-списком записей")
        return []
    if not data:
        rep.error(name, "список пуст")
    return data


def check_prs(prs, rep, name="prs.json"):
    seen = set()
    for pr in _records(prs, name, rep):
        n = pr.get("number")
        where = f"{name} #{n}"
        if not _is_int(n):
            rep.error(where, "number должен быть целым числом")
            continue
        if n in seen:
            rep.error(where, "номер встречается дважды")
        seen.add(n)
        for key in ("title", "body", "author"):
            if not isinstance(pr.get(key), str):
                rep.error(where, f"{key} должен быть строкой")
        if not pr.get("author"):
            rep.error(where, "пустой author: без него PR не попадёт ни в чей профиль")
        if not _is_time(pr.get("merged_at")):
            rep.error(where, "merged_at должен быть датой вида 2026-05-14T10:22:00Z")
        for key in ("additions", "deletions"):
            if not _is_int(pr.get(key)) or pr[key] < 0:
                rep.error(where, f"{key} должен быть неотрицательным целым числом")
        if pr.get("ci") not in CI_VALUES:
            rep.error(where, f"ci должен быть одним из {sorted(CI_VALUES)}, а не {pr.get('ci')!r}")
        files = pr.get("files")
        if not isinstance(files, list):
            rep.error(where, "files должен быть списком")
            files = []
        paths = set()
        for f in files:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not f.get("path"):
                rep.error(where, "у каждого файла должен быть непустой path")
                continue
            if f["path"] in paths:
                rep.error(where, f"файл {f['path']} указан дважды")
            paths.add(f["path"])
            if not isinstance(f.get("patch"), str) or not isinstance(f.get("truncated"), bool):
                rep.error(where, f"у файла {f['path']} patch должен быть строкой, truncated — true/false")
        if isinstance(pr.get("files"), list) and not any(isinstance(f, dict) and f.get("patch") for f in files):
            rep.warn(where, "ни у одного файла нет diff: модели не на что смотреть, оценка будет слабой")
        reviews = pr.get("reviews")
        if not isinstance(reviews, list) or not all(
                isinstance(r, dict) and isinstance(r.get("state"), str) and isinstance(r.get("body"), str) for r in reviews):
            rep.error(where, "reviews должен быть списком записей с полями state и body")
        if not isinstance(pr.get("noise_removed"), list):
            rep.error(where, "noise_removed должен быть списком путей")
    return seen


def check_scores(scores, prs_by_number, rep, name="scores.json", require_all=True):
    seen = set()
    for record in _records(scores, name, rep):
        n = record.get("number")
        where = f"{name} #{n}"
        if n in seen:
            rep.error(where, "PR оценён дважды")
        seen.add(n)
        pr = prs_by_number.get(n)
        if prs_by_number and pr is None:
            rep.error(where, "такого PR нет в prs.json")
        if not isinstance(record.get("summary"), str) or not record.get("summary", "").strip():
            rep.error(where, "summary должен быть непустой строкой")
        if not _is_int(record.get("runs")) or record.get("runs", 0) < 1:
            rep.error(where, "runs должен быть целым числом от 1")
        if not isinstance(record.get("unstable"), bool):
            rep.error(where, "unstable должен быть true или false")
        block = record.get("scores")
        if not isinstance(block, dict) or set(block) != set(CRITERIA):
            rep.error(where, f"scores должен содержать ровно критерии {', '.join(CRITERIA)}")
            continue
        shown = {f["path"] for f in (pr or {}).get("files") or [] if isinstance(f, dict) and f.get("patch")}
        for c in CRITERIA:
            item = block[c] if isinstance(block[c], dict) else {}
            if not _is_int(item.get("score")) or not 1 <= item["score"] <= 5:
                rep.error(where, f"{c}.score должен быть целым числом от 1 до 5")
            if not isinstance(item.get("reason"), str) or not item.get("reason", "").strip():
                rep.error(where, f"{c}.reason пуст: балл без объяснения показывать нельзя")
            evidence = item.get("evidence")
            if not isinstance(evidence, list):
                rep.error(where, f"{c}.evidence должен быть списком")
                continue
            for ev in evidence:
                if not isinstance(ev, dict) or not isinstance(ev.get("path"), str) or not isinstance(ev.get("lines"), str):
                    rep.error(where, f"{c}.evidence: у каждой ссылки должны быть строки path и lines")
                elif pr is not None and ev["path"] not in shown:
                    rep.error(where, f"{c}.evidence ссылается на {ev['path']}, а такого файла нет в показанном diff")
                elif not LINES.match(ev["lines"]):
                    rep.error(where, f"{c}.evidence: lines должен быть вида 10-18, 10 или пустым, а не {ev['lines']!r}")
    if require_all:
        missing = sorted(set(prs_by_number) - seen)
        if missing:
            rep.warn(name, f"не оценено {len(missing)} PR из prs.json: {missing[:10]}{' …' if len(missing) > 10 else ''}")
    return seen


def check_outcomes(outcomes, prs_by_number, rep, name="outcomes.json"):
    seen = set()
    for o in _records(outcomes, name, rep):
        n = o.get("number")
        where = f"{name} #{n}"
        seen.add(n)
        if prs_by_number and n not in prs_by_number:
            rep.error(where, "такого PR нет в prs.json")
        for key in ("reverted", "problem", "problem_strict", "observable"):
            if not isinstance(o.get(key), bool):
                rep.error(where, f"{key} должен быть true или false")
        for key in ("reverted_by", "fixed_by", "fixed_by_explicit"):
            if not isinstance(o.get(key), list) or not all(_is_int(x) for x in o.get(key) or []):
                rep.error(where, f"{key} должен быть списком номеров PR")
                break
        else:
            if o.get("reverted") != bool(o["reverted_by"]):
                rep.error(where, "reverted не согласован с reverted_by")
            if o.get("problem") != bool(o["reverted_by"] or o["fixed_by"]):
                rep.error(where, "problem не согласован с reverted_by и fixed_by")
            if not set(o["fixed_by_explicit"]) <= set(o["fixed_by"]):
                rep.error(where, "fixed_by_explicit должен быть частью fixed_by")
            if n in o["reverted_by"] or n in o["fixed_by"]:
                rep.error(where, "PR указан как откат или исправление самого себя")
    missing = sorted(set(prs_by_number) - seen)
    if missing:
        rep.warn(name, f"нет разметки для {len(missing)} PR из prs.json: файл устарел, запустите outcomes.py")
    if outcomes and isinstance(outcomes, list) and not any(isinstance(o, dict) and o.get("observable") for o in outcomes):
        rep.warn(name, "ни у одного PR не прошло окно наблюдения: валидации будет не на чем считать")


CHANGE_TYPES = ["feature", "bugfix", "performance", "refactor", "docs", "tests", "build"]   # как в classify.py


def check_types(data, prs_by_number, rep, name="change_types.json"):
    if not isinstance(data, dict) or not isinstance(data.get("prs"), list):
        rep.error(name, "должен быть объектом с полем prs: списком записей {number, type, reason}")
        return
    seen = set()
    for i, r in enumerate(data["prs"]):
        where = f"{name} #{r.get('number', '?') if isinstance(r, dict) else '?'}"
        if not isinstance(r, dict) or not _is_int(r.get("number")):
            rep.error(f"{name}[{i}]", "у записи должен быть целый number")
            continue
        if r["number"] in seen:
            rep.error(where, "тип указан дважды")
        seen.add(r["number"])
        if r["number"] not in prs_by_number:
            rep.error(where, "такого PR нет в prs.json")
        if r.get("type") not in CHANGE_TYPES:
            rep.error(where, f"type должен быть одним из {', '.join(CHANGE_TYPES)}, а не {r.get('type')!r}")
        if not isinstance(r.get("reason"), str) or not r["reason"].strip():
            rep.error(where, "reason пуст: тип без объяснения показывать нельзя")
    missing = sorted(set(prs_by_number) - seen)
    if missing:
        rep.warn(name, f"тип не определён у {len(missing)} PR из prs.json: запустите classify.py")


def check_metrics(data, rep, cfg=None, expected=None, name="metrics.json"):
    low, high = (cfg["multiplier"]["min"], cfg["multiplier"]["max"]) if cfg else (0, 10)
    seen = set()
    for m in _records(data, name, rep):
        where = f"{name} {m.get('author')!r}"
        if not isinstance(m.get("author"), str) or not m.get("author"):
            rep.error(where, "author должен быть непустой строкой")
        if m.get("author") in seen:
            rep.error(where, "разработчик встречается дважды")
        seen.add(m.get("author"))
        if m.get("level") not in LEVELS:
            rep.error(where, f"level должен быть одним из {LEVELS}")
        if not _is_int(m.get("pr_count")) or m.get("pr_count", 0) < 1:
            rep.error(where, "pr_count должен быть целым числом от 1")
        med = m.get("medians")
        if not isinstance(med, dict) or set(med) != set(CRITERIA) or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 5 for v in med.values()):
            rep.error(where, "medians должен содержать четыре критерия со значениями от 1 до 5")
        for key in ("composite", "norm"):
            if not isinstance(m.get(key), (int, float)) or isinstance(m.get(key), bool) or not 1 <= m[key] <= 5:
                rep.error(where, f"{key} должен быть числом от 1 до 5")
        mult, flags = m.get("multiplier"), m.get("flags")
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            rep.error(where, "flags должен быть списком строк")
            flags = []
        if mult is None:
            if "insufficient_data" not in flags:
                rep.error(where, "multiplier пуст, но нет флага insufficient_data")
        elif not isinstance(mult, (int, float)) or isinstance(mult, bool) or not low - 1e-9 <= mult <= high + 1e-9:
            rep.error(where, f"multiplier должен лежать между {low} и {high}, а не {mult!r}")
        elif "insufficient_data" in flags:
            rep.error(where, "стоит флаг insufficient_data, но множитель посчитан")
    if expected is not None and isinstance(data, list):
        if data != expected:
            got = {m.get("author"): m for m in data if isinstance(m, dict)}
            want = {m["author"]: m for m in expected}
            diff = sorted(set(got) ^ set(want)) or sorted(a for a in want if got.get(a) != want[a])
            rep.error(name, "не совпадает с пересчётом из текущих prs.json и scores.json. Либо файл устарел "
                            "(запустите metrics.py), либо его считали за часть периода (--since, --until) или с другим "
                            f"конфигом. Расходятся: {diff[:8]}")


def check_folder(folder: Path, config_path: Path, only_prs: bool = False) -> Report:
    rep = Report()

    def load(file_name):
        path = folder / file_name
        if not path.is_file():
            return None
        try:
            return read_json(path, file_name)
        except DataError as e:
            rep.error(file_name, str(e))
            return None

    prs = load("prs.json")
    if prs is None:
        rep.error("prs.json", "файл не найден или не читается: без него остальное не проверить")
        return rep
    check_prs(prs, rep)
    by_number = {p["number"]: p for p in prs if isinstance(p, dict) and _is_int(p.get("number"))} if isinstance(prs, list) else {}
    if only_prs:
        return rep

    scores = load("scores.json")
    if scores is not None:
        check_scores(scores, by_number, rep)
    style_prs, style_scores = load("prs_style.json"), load("scores_style.json")
    if style_prs is not None:
        check_prs(style_prs, rep, "prs_style.json")
    if style_scores is not None:
        style_by_number = {p["number"]: p for p in style_prs or [] if isinstance(p, dict) and _is_int(p.get("number"))}
        check_scores(style_scores, style_by_number or by_number, rep, "scores_style.json", require_all=bool(style_by_number))

    outcomes = load("outcomes.json")
    if outcomes is not None:
        check_outcomes(outcomes, by_number, rep)

    types = load("change_types.json")
    if types is not None:
        check_types(types, by_number, rep)

    metrics = load("metrics.json")
    if metrics is not None:
        cfg = expected = None
        try:
            cfg = metrics_module.load_config(config_path)
            if scores is not None and not rep.errors:
                history = load("author_history.json") or {}
                expected, _ = metrics_module.compute(prs, scores, history, cfg)
        except DataError as e:
            rep.warn("metrics.json", f"пересчёт для сверки не выполнен: {e}")
        check_metrics(metrics, rep, cfg, expected)

    stats = load("run_stats.json")
    if isinstance(stats, dict):
        if stats.get("failed"):
            rep.warn("run_stats.json", f"не оценено PR: {len(stats['failed'])}, первый: {stats['failed'][0]}")
        if stats.get("author_login_still_visible"):
            rep.warn("run_stats.json", f"логин автора остался виден модели в PR {stats['author_login_still_visible']}")
        if isinstance(scores, list) and stats.get("prs_scored") != len(scores):
            rep.warn("run_stats.json", "число оценённых PR не совпадает со scores.json: файлы от разных запусков")
    return rep


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="Проверка формата и согласованности файлов конвейера")
    ap.add_argument("--dir", default=".", help="папка с prs.json, scores.json и остальными файлами")
    ap.add_argument("--config", default=str(HERE / "config" / "levels.json"))
    ap.add_argument("--only-prs", action="store_true", help="проверить только prs.json (перед оценкой)")
    use_data_dir(ap, 'dir')
    args = ap.parse_args()
    rep = check_folder(Path(args.dir), Path(args.config), args.only_prs)
    for line in rep.warnings:
        print("предупреждение  " + line)
    for line in rep.errors[:60]:
        print("ОШИБКА  " + line)
    if len(rep.errors) > 60:
        print(f"... и ещё {len(rep.errors) - 60} ошибок")
    print(f"\nПроверка {'НЕ пройдена' if rep.errors else 'пройдена'}: ошибок {len(rep.errors)}, предупреждений {len(rep.warnings)}")
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
