"""Репетиция всего конвейера без сети, без токена и без ключа модели.

    python demo.py

Поднимает две локальные заглушки (GitHub с синтетическим репозиторием demo/shop и «модель»,
которая ставит баллы простыми правилами) и прогоняет через них все восемь шагов.
Результаты ложатся в demo_out/.

Зачем это нужно
    - убедиться, что на этом компьютере всё запускается, до того как тратить ключ;
    - дать участникам 3 и 4 файлы правильного формата раньше, чем готов настоящий скоринг.

Баллы в demo_out/ ставит НЕ нейросеть, а репозиторий выдуман: цифры оттуда ничего не
доказывают и на слайд не идут. В отчёте validation.md об этом написано первой строкой.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "devtools"))
import demo_repo      # noqa: E402
import fake_github    # noqa: E402
import fake_llm       # noqa: E402

CRITERIA = ["complexity", "quality", "risk", "clarity"]


def human_scores_for_demo(out: Path) -> None:
    """Имитация ручной оценки 20 PR, чтобы шаг «человек против модели» тоже отработал.

    Это не оценки человека: баллы заглушки сдвинуты по фиксированному правилу."""
    scores = json.loads((out / "scores.json").read_text(encoding="utf-8"))
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(["number"] + CRITERIA)
    for i, record in enumerate(scores[:: max(1, len(scores) // 20)][:20]):
        row = [record["number"]]
        for j, c in enumerate(CRITERIA):
            shift = (0, 0, 1, 0, -1, 0, 2)[(i * 3 + j) % 7]
            row.append(max(1, min(5, record["scores"][c]["score"] + shift)))
        writer.writerow(row)
    (out / "human_scores.csv").write_text(buf.getvalue(), encoding="utf-8")


def main(limit: int = 70, workdir: str = "demo_out", runs: int = 3) -> int:
    out = (Path.cwd() / workdir).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    github, github_url = fake_github.start(flaky=True)
    llm, llm_url, _ = fake_llm.start()
    env = dict(os.environ,
               GITHUB_API_URL=github_url, GITHUB_TOKEN="demo-token", FETCH_CACHE_DIR=str(out / "raw"), FETCH_SEARCH_PAUSE="0",
               SCORER_BASE_URL=llm_url, SCORER_MODEL="fake-llm", SCORER_API_KEY="local-demo-key", SCORER_PROVIDER="",
               SCORER_PRICE_IN="", SCORER_PRICE_OUT="", SCORER_JSON_MODE="schema", PYTHONUTF8="1",
               SCORER_IGNORE_PROJECT_ENV="1")   # настройки из .env проекта репетиции не нужны
    print(f"Репетиция: заглушка GitHub {github_url}, заглушка модели {llm_url}. В сеть ничего не уходит.\n", flush=True)
    try:
        base = [sys.executable, str(HERE / "run_all.py"), "--workdir", str(out), "--runs", str(runs), "--min-age-days", "7", "--no-app"]
        code = subprocess.call(base + ["--repo", demo_repo.REPO, "--limit", str(limit)], env=env)
        if code in (0, 1) and (out / "scores.json").is_file():
            human_scores_for_demo(out)
            code = subprocess.call([sys.executable, str(HERE / "validate.py")], cwd=str(out), env=env)
    finally:
        github.stop()
        llm.stop()
    print(f"\nРепетиция {'прошла' if code == 0 else 'завершилась с кодом ' + str(code)}. Файлы в {out}")
    print("Напоминание: это заглушка на выдуманном репозитории, цифры из demo_out на слайд не идут.")
    return code


if __name__ == "__main__":
    sys.exit(main())
