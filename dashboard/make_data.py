"""Собирает файлы конвейера в dashboard/data.js, чтобы дашборд открывался двойным кликом.

Запуск из корня проекта (данные в data/) или из папки, где лежит prs.json:
    python dashboard/make_data.py
    python dashboard/make_data.py --dir demo_out
    python dashboard/make_data.py --single dashboard.html    # всё одним файлом: удобно переслать или держать запасным на демо

Обычно этот скрипт запускать не нужно: `python app.py` отдаёт дашборду свежие данные сам.
Он нужен, когда дашборд открывают без приложения, двойным кликом по index.html.

Берёт prs.json (обязателен), scores.json и всё, что найдёт рядом: metrics.json,
outcomes.json, validation.json, run_stats.json, author_history.json, config/levels.json.
Сервер не нужен: data.js подключается обычным тегом script, поэтому index.html
работает и с диска, и без сети.

Чтобы файл не разрастался, из PR оставляются diff только тех файлов, на которые
ссылаются оценки: остальные дашборд не показывает.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPTIONAL = {"metrics": "metrics.json", "outcomes": "outcomes.json", "validation": "validation.json",
            "run_stats": "run_stats.json", "history": "author_history.json"}


class DataProblem(Exception):
    """Данных нет или они не того вида. Текст показывается человеку."""


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise DataProblem(f"не удалось прочитать {path}: {e}")


def slim_prs(prs: list, scores: list) -> list:
    """Оставляет diff только у файлов, на которые ссылаются оценки, и подрезает длинные тексты."""
    cited = {}
    for record in scores:
        paths = cited.setdefault(record.get("number"), set())
        for item in (record.get("scores") or {}).values():
            for ev in (item or {}).get("evidence") or []:
                paths.add(ev.get("path"))
    out = []
    for pr in prs:
        keep = cited.get(pr.get("number"), set())
        files = [{"path": f.get("path"), "truncated": bool(f.get("truncated")),
                  "patch": f.get("patch") or "" if f.get("path") in keep else ""} for f in pr.get("files") or []]
        reviews = [{"state": r.get("state"), "body": (r.get("body") or "")[:600]} for r in (pr.get("reviews") or [])[:12]]
        out.append({"number": pr.get("number"), "title": pr.get("title"), "body": (pr.get("body") or "")[:4000],
                    "author": pr.get("author"), "merged_at": pr.get("merged_at"), "additions": pr.get("additions"),
                    "deletions": pr.get("deletions"), "ci": pr.get("ci"), "files": files, "reviews": reviews,
                    "noise_removed": pr.get("noise_removed") or []})
    return out


def collect(folder: Path, config: Path | None) -> dict:
    if not (folder / "prs.json").is_file():
        raise DataProblem(f"в {folder.resolve()} нет prs.json. Запустите скрипт из папки с результатами или укажите её через --dir.")
    # Оценок может ещё не быть: тогда дашборд показывает PR и исходы и подсказывает, как оценить.
    scores = load(folder / "scores.json") if (folder / "scores.json").is_file() else []
    prs = load(folder / "prs.json")
    try:
        data = {"prs": slim_prs(prs, scores), "scores": scores}
    except (AttributeError, TypeError):
        raise DataProblem("prs.json или scores.json не того вида: нужны списки записей. Проверьте их командой python check_data.py")
    for key, name in OPTIONAL.items():
        if (folder / name).is_file():
            data[key] = load(folder / name)
    if "run_stats" in data and isinstance(data["run_stats"], dict):   # дашборду нужны модель и баллы по прогонам
        data["run_stats"] = {k: data["run_stats"].get(k) for k in ("model", "prompt_version", "per_pr", "runs_per_pr")}
    for candidate in (config, folder / "config" / "levels.json", HERE.parent / "config" / "levels.json"):
        if candidate and candidate.is_file():
            data["config"] = load(candidate)
            break
    data["repo"] = (data.get("history") or {}).get("repo", "")
    return data


def as_script(data: dict) -> str:
    # Знак "<" экранируется целиком: текст из PR не должен ни закрыть тег script, ни открыть
    # комментарий или вложенный script, если данные встроены прямо в страницу.
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    body = body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return "window.DASHBOARD_DATA = " + body + ";\n"


def single_page(script: str) -> str:
    """index.html со встроенными стилями, данными и кодом: один файл без соседей."""
    html = (HERE / "index.html").read_text(encoding="utf-8")
    css = (HERE / "styles.css").read_text(encoding="utf-8")
    app = (HERE / "app.js").read_text(encoding="utf-8").replace("</", "<\\/")
    texts = (HERE / "i18n.js").read_text(encoding="utf-8").replace("</", "<\\/")
    for old, new in (('<link rel="stylesheet" href="styles.css">', "<style>\n" + css + "</style>"),
                     ('<script src="i18n.js"></script>', "<script>\n" + texts + "</script>"),
                     ('<script src="data.js"></script>', "<script>\n" + script + "</script>"),
                     ('<script src="app.js"></script>', "<script>\n" + app + "</script>")):
        if old not in html:
            raise DataProblem(f"в index.html не найдена строка {old}: файл меняли, соберите страницу вручную.")
        html = html.replace(old, new, 1)
    return html


def main():
    ap = argparse.ArgumentParser(description="Собрать данные конвейера в dashboard/data.js")
    ap.add_argument("--dir", default="data" if not Path("prs.json").is_file() and Path("data").is_dir() else ".",
                    help="папка с prs.json, scores.json и остальными файлами (по умолчанию data/ или текущая)")
    ap.add_argument("--config", help="путь к levels.json, если он не в config/")
    ap.add_argument("--out", default=str(HERE / "data.js"))
    ap.add_argument("--single", metavar="FILE", help="дополнительно записать всё одним HTML-файлом")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):          # консоль Windows не всегда умеет кириллицу
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        data = collect(Path(args.dir), Path(args.config) if args.config else None)
        script = as_script(data)
        page = single_page(script) if args.single else None
    except DataProblem as e:
        sys.exit(f"ОШИБКА: {e}")
    Path(args.out).write_text(script, encoding="utf-8")
    have = [k for k in ("metrics", "outcomes", "validation", "run_stats", "history", "config") if k in data]
    missing = [OPTIONAL.get(k, "config/levels.json") for k in ("metrics", "outcomes", "validation") if k not in data]
    print(f"Данные -> {args.out}: {len(data['prs'])} PR, {len(data['scores'])} оценок, {len(script) // 1024} КБ")
    print("  найдено: " + (", ".join(have) or "только PR и оценки"))
    if missing:
        print("  нет " + ", ".join(missing) + ": соответствующие разделы дашборда покажут подсказку, как их получить")
    if args.single:
        Path(args.single).write_text(page, encoding="utf-8")
        print(f"Один файл -> {args.single}")
    print(f"Откройте {HERE / 'index.html'} в браузере.")


if __name__ == "__main__":
    main()
