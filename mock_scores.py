"""Пробные оценки для участника 4, пока настоящий скоринг не готов:  prs.json -> scores.json

Запуск:
    python mock_scores.py                 # из корня проекта: читает data/prs.json, пишет в mock_out/
    python mock_scores.py --seed 2        # другой набор случайных баллов

Из корня проекта пробные оценки пишутся в отдельную папку mock_out/ вместе с копиями
prs.json, all_prs.json и author_history.json: в data/ лежат настоящие данные, и случайным
баллам рядом с ними не место. В папке, где prs.json лежит рядом, файлы пишутся туда же.

Баллы СЛУЧАЙНЫЕ. Они нужны только затем, чтобы на настоящем prs.json отладить расчёт
множителя, разметку и график раньше, чем участник 2 пришлёт scores.json. Формат файла
тот же, что у score.py, поэтому замена на настоящие оценки ничего не ломает.

Защита от путаницы
    - в run_stats.json записано «model: fake-mock», поэтому отчёт и график сами
      помечаются как прогон на заглушке;
    - если в папке уже лежит scores.json от настоящей модели, скрипт откажется его
      затирать. Перезаписать можно только явно, с --force.
"""
from __future__ import annotations

import argparse
import random
import re
import shutil
from pathlib import Path

from common import CRITERIA, DataError, data_dir, die, read_json, read_list, setup_output, write_json, use_data_dir

MODEL = "fake-mock"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def mock(prs: list, seed: int = 1, runs: int = 3) -> list:
    """Случайные, но воспроизводимые баллы: один и тот же PR при том же seed получает то же самое."""
    records = []
    for pr in prs:
        if not isinstance(pr.get("number"), int):
            raise DataError(f"в prs.json у записи нет целого number: {str(pr)[:80]}")
        rng = random.Random(f"{seed}:{pr['number']}")
        evidence = []
        for f in pr.get("files") or []:
            m = HUNK.search(f.get("patch") or "")
            if m:
                start = int(m.group(1))
                evidence = [{"path": f["path"], "lines": f"{start}-{start + max(int(m.group(2) or 1), 1) - 1}"}]
                break
        scores = {c: {"score": rng.choices([1, 2, 3, 4, 5], [1, 3, 4, 3, 1])[0],
                      "reason": "Пробный балл: поставлен случайно, не моделью.",
                      "evidence": [] if c == "clarity" else list(evidence)} for c in CRITERIA}
        records.append({"number": pr["number"], "summary": "Пробная запись без оценки модели.", "scores": scores,
                        "runs": runs, "unstable": rng.random() < 0.08})
    return records


def is_real(stats_path: Path) -> bool:
    """Рядом лежит статистика настоящего прогона?"""
    if not stats_path.is_file():
        return False
    try:
        return not str(read_json(stats_path).get("model", "")).startswith("fake")
    except (DataError, AttributeError):
        return True   # файл есть, но не читается: считаем настоящим, чтобы ничего не затереть


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="Пробные случайные оценки в формате scores.json")
    ap.add_argument("--prs", default="prs.json")
    ap.add_argument("--out", default="scores.json")
    ap.add_argument("--stats", default="run_stats.json")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="перезаписать, даже если рядом оценки настоящей модели")
    ap.add_argument("--mock-dir", default="mock_out",
                    help="куда писать, когда настоящие данные лежат в data/: пробные оценки рядом с ними не кладутся")
    use_data_dir(ap, "prs")
    args = ap.parse_args()
    out, stats = Path(args.out), Path(args.stats)
    separate = data_dir() != Path(".") and args.out == "scores.json" and args.stats == "run_stats.json"
    if separate:
        # Настоящие данные лежат в data/. Случайные баллы туда не пишутся: иначе их легко
        # закоммитить как оценки модели. Всё нужное для расчётов копируется в отдельную папку.
        folder = Path(args.mock_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("prs.json", "all_prs.json", "author_history.json"):
            source = Path(args.prs).parent / name
            if source.is_file():
                shutil.copyfile(source, folder / name)
        out, stats = folder / "scores.json", folder / "run_stats.json"
    if out.is_file() and not args.force and (is_real(stats) or not stats.is_file()):
        die(f"{out} уже существует и не похож на пробный файл. Чтобы не затереть настоящие оценки, скрипт остановлен. "
            "Укажите другой файл через --out или добавьте --force.")
    try:
        records = mock(read_list(args.prs, "файл PR"), args.seed)
    except DataError as e:
        die(str(e))
    write_json(out, records)
    write_json(stats, {"model": MODEL, "prompt_version": f"mock-seed-{args.seed}", "input": Path(args.prs).name,
                       "runs_per_pr": 3, "prs_in": len(records), "prs_scored": len(records), "failed": [],
                       "unstable": [r["number"] for r in records if r["unstable"]],
                       "all_runs_identical": sum(1 for r in records if not r["unstable"]),
                       "runs_from_cache": 0, "answers_rejected_and_retried": 0, "evidence_removed": 0,
                       "evidence_lines_cleared": 0, "author_login_still_visible": [], "cost_usd": 0})
    print(f"Пробные оценки для {len(records)} PR -> {out} (модель в {stats}: {MODEL})")
    print("Баллы случайные: связи с откатами в них нет, и отчёт это покажет. Это нормально.")
    if separate:
        print(f"Настоящие данные в data/ не тронуты. Дальше: python p4.py --dir {out.parent}   и   "
              f"python app.py serve --dir {out.parent}")


if __name__ == "__main__":
    main()
