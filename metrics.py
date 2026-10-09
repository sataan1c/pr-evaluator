"""Множитель по нормам уровней (участник 4):  prs.json + scores.json -> metrics.json

Запуск:
    python metrics.py
    python metrics.py --prs prs.json --scores scores.json --history author_history.json \
        --config config/levels.json --out metrics.json

Как считается
    1. По каждому разработчику берётся медиана баллов за период по каждому критерию.
       Медиана, а не сумма: десять мелких PR не дают больше, чем три.
    2. Сводный балл S = сумма медиан сложности, качества и ясности с весами уровня.
       Риск в S не входит: рискованность изменения не заслуга и не вина автора.
    3. Множитель m = 1 + slope * (S - N) / N, ограниченный диапазоном [min, max],
       где N — норма уровня.
    4. Меньше min_prs PR за период: множитель не считается (null), флаг insufficient_data.

Откуда уровень
    level_overrides из конфига (в компании сюда кладётся выгрузка из HR-системы), иначе
    число PR автора, принятых до начала периода (author_history.json), иначе default_level
    с флагом level_unknown. Для открытого репозитория это допущение, а не факт.

Откуда норма
    calibrate_norms = "auto": если на уровне набралось calibrate_min_authors разработчиков
    с достаточным числом PR, норма равна медиане их сводных баллов, иначе берётся из конфига.
    Откалиброванная норма значит «типичный разработчик этого уровня в этом репозитории»,
    поэтому около половины людей уровня получат множитель выше 1 по построению.

Флаги
    insufficient_data   мало PR за период, множителя нет
    many_small_prs      большинство PR совсем маленькие: возможное дробление работы
    unstable_scores     у заметной доли PR оценки модели расходились между прогонами
    level_unknown       истории автора нет, уровень взят по умолчанию
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

from common import CRITERIA, HERE, LEVELS, DataError, die, read_json, read_list, setup_output, write_json, use_data_dir

COMPOSITE = ["complexity", "quality", "clarity"]


def load_config(path) -> dict:
    cfg = read_json(path, "конфиг уровней")
    if not isinstance(cfg, dict):
        raise DataError(f"{path}: конфиг должен быть JSON-объектом")
    try:
        for level in LEVELS:
            weights = cfg["weights"][level]
            if set(weights) != set(COMPOSITE):
                raise DataError(f"{path}: веса уровня {level} должны быть заданы ровно для {', '.join(COMPOSITE)}")
            if any(not isinstance(w, (int, float)) or w < 0 for w in weights.values()):
                raise DataError(f"{path}: веса уровня {level} должны быть неотрицательными числами")
            if abs(sum(weights.values()) - 1) > 1e-6:
                raise DataError(f"{path}: веса уровня {level} в сумме дают {sum(weights.values()):g}, а должны 1")
            if not 1 <= cfg["norms"][level] <= 5:
                raise DataError(f"{path}: норма уровня {level} должна лежать между 1 и 5")
        mult = cfg["multiplier"]
        if not (0 < mult["min"] <= 1 <= mult["max"]) or mult["slope"] <= 0:
            raise DataError(f"{path}: в multiplier нужно min <= 1 <= max и slope > 0")
        bands = cfg["level_by_merged_before"]
        if [b["level"] for b in bands] != LEVELS or bands[-1]["max"] is not None:
            raise DataError(f"{path}: level_by_merged_before должен перечислять junior, middle, senior, у последнего max = null")
        if cfg.get("default_level", "middle") not in LEVELS:
            raise DataError(f"{path}: default_level должен быть одним из {LEVELS}")
        for login, level in (cfg.get("level_overrides") or {}).items():
            if level not in LEVELS:
                raise DataError(f"{path}: в level_overrides у {login} неизвестный уровень {level!r}")
        if cfg.get("calibrate_norms", "auto") not in ("auto", "always", "never"):
            raise DataError(f"{path}: calibrate_norms должен быть auto, always или never")
        if cfg["min_prs"] < 1:
            raise DataError(f"{path}: min_prs должен быть не меньше 1")
    except (KeyError, TypeError) as e:
        raise DataError(f"{path}: в конфиге не хватает поля или оно не того типа ({e})")
    return cfg


def level_of(author: str, merged_before: dict, cfg: dict) -> tuple:
    """(уровень, откуда он взят)."""
    overrides = cfg.get("level_overrides") or {}
    if author in overrides:
        return overrides[author], "override"
    count = merged_before.get(author)
    if not isinstance(count, int) or isinstance(count, bool):
        return cfg.get("default_level", "middle"), "unknown"
    for band in cfg["level_by_merged_before"]:
        if band["max"] is None or count <= band["max"]:
            return band["level"], "history"
    return LEVELS[-1], "history"


def multiplier(composite: float, norm: float, cfg: dict) -> float:
    rule = cfg["multiplier"]
    raw = 1 + rule["slope"] * (composite - norm) / norm
    return round(min(rule["max"], max(rule["min"], raw)), 2)


def compute(prs: list, scores: list, history: dict, cfg: dict, since: str = "", until: str = "") -> tuple:
    """Возвращает (записи metrics.json, сведения о расчёте)."""
    by_number = {pr.get("number"): pr for pr in prs}
    merged_before = (history or {}).get("merged_before") or {}
    rows, orphan = defaultdict(list), []
    for record in scores:
        pr = by_number.get(record.get("number"))
        if pr is None:
            orphan.append(record.get("number"))
            continue
        day = (pr.get("merged_at") or "")[:10]
        if (since and day < since) or (until and day > until):
            continue
        try:
            values = {c: record["scores"][c]["score"] for c in CRITERIA}
        except (KeyError, TypeError):
            raise DataError(f"в scores.json у PR #{record.get('number')} нет баллов по всем четырём критериям")
        rows[pr.get("author") or "ghost"].append({
            "scores": values,
            "lines": (pr.get("additions") or 0) + (pr.get("deletions") or 0),
            "unstable": bool(record.get("unstable")),
        })

    people = []
    for author in sorted(rows, key=str.lower):
        items = rows[author]
        level, level_source = level_of(author, merged_before, cfg)
        medians = {c: statistics.median(item["scores"][c] for item in items) for c in CRITERIA}
        composite = sum(cfg["weights"][level][c] * medians[c] for c in COMPOSITE)
        people.append({"author": author, "level": level, "level_source": level_source, "items": items,
                       "medians": medians, "composite": composite, "enough": len(items) >= cfg["min_prs"]})

    # Норма уровня: из конфига или медиана по тем, у кого достаточно PR.
    mode = cfg.get("calibrate_norms", "auto")
    norms, norm_source = {}, {}
    for level in LEVELS:
        peers = [p["composite"] for p in people if p["level"] == level and p["enough"]]
        calibrate = bool(peers) and (mode == "always" or (mode == "auto" and len(peers) >= cfg.get("calibrate_min_authors", 5)))
        norms[level] = statistics.median(peers) if calibrate else cfg["norms"][level]
        norm_source[level] = "calibrated" if calibrate else "config"

    records = []
    for p in people:
        items, flags = p["items"], []
        if not p["enough"]:
            flags.append("insufficient_data")
        small = sum(1 for item in items if item["lines"] <= cfg.get("small_pr_lines", 20))
        if len(items) >= cfg.get("small_pr_min_count", 5) and small / len(items) >= cfg.get("small_pr_share", 0.6):
            flags.append("many_small_prs")
        if p["enough"] and sum(item["unstable"] for item in items) / len(items) >= cfg.get("unstable_share", 0.3):
            flags.append("unstable_scores")
        if p["level_source"] == "unknown":
            flags.append("level_unknown")
        norm = norms[p["level"]]
        records.append({
            "author": p["author"],
            "level": p["level"],
            "pr_count": len(items),
            "medians": {c: _tidy(p["medians"][c]) for c in CRITERIA},
            "composite": round(p["composite"], 2),
            "norm": round(norm, 2),
            "multiplier": multiplier(p["composite"], norm, cfg) if p["enough"] else None,
            "flags": flags,
            "level_source": p["level_source"],
            "norm_source": norm_source[p["level"]],
        })
    info = {"norms": {k: round(v, 2) for k, v in norms.items()}, "norm_source": norm_source,
            "scores_without_pr": orphan, "prs_used": sum(len(v) for v in rows.values())}
    return records, info


def _tidy(value):
    """3.0 -> 3, а 3.5 остаётся 3.5: медиана чётного числа PR бывает дробной."""
    return int(value) if float(value).is_integer() else value


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="Расчёт множителя по нормам уровней")
    ap.add_argument("--prs", default="prs.json")
    ap.add_argument("--scores", default="scores.json")
    ap.add_argument("--history", default="author_history.json", help="можно не давать: уровни возьмутся из конфига")
    ap.add_argument("--config", default=str(HERE / "config" / "levels.json"))
    ap.add_argument("--since", default="", help="начало периода, ГГГГ-ММ-ДД (по дате мержа)")
    ap.add_argument("--until", default="", help="конец периода, ГГГГ-ММ-ДД")
    ap.add_argument("--out", default="metrics.json")
    use_data_dir(ap, 'prs', 'scores', 'history', 'out')
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
        history = read_json(args.history, "история авторов") if Path(args.history).is_file() else {}
        records, info = compute(read_list(args.prs, "файл PR"), read_list(args.scores, "файл оценок"),
                                history, cfg, args.since, args.until)
    except DataError as e:
        die(str(e))
    if not records:
        die("нет ни одного оценённого PR за выбранный период")
    write_json(args.out, records)

    print(f"Посчитано {len(records)} разработчиков по {info['prs_used']} PR -> {args.out}")
    if not history:
        print(f"  {args.history} не найден: уровни взяты из level_overrides, остальным поставлен уровень по умолчанию")
    print("  нормы: " + ", ".join(f"{lvl} {info['norms'][lvl]:g} ({'по данным' if info['norm_source'][lvl] == 'calibrated' else 'из конфига'})"
                                  for lvl in LEVELS))
    if info["scores_without_pr"]:
        print(f"ВНИМАНИЕ: {len(info['scores_without_pr'])} оценок без записи в {args.prs}, они пропущены: "
              f"{info['scores_without_pr'][:10]}")
    print(f"\n  {'разработчик':<22}{'уровень':<9}{'PR':>4}{'балл':>7}{'норма':>7}{'множ.':>7}  флаги")
    for r in records:
        mult = "—" if r["multiplier"] is None else f"{r['multiplier']:.2f}"
        print(f"  {r['author'][:21]:<22}{r['level']:<9}{r['pr_count']:>4}{r['composite']:>7.2f}{r['norm']:>7.2f}{mult:>7}  "
              + ", ".join(r["flags"]))


if __name__ == "__main__":
    main()
