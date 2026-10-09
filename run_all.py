"""
run_all.py — запускает весь конвейер одной командой и открывает дашборд.

    python run_all.py            # шаги, у которых уже есть результат, пропускаются
    python run_all.py --force    # пересчитать всё заново (нужны токен GitHub и ключ API)
    python run_all.py --no-app   # только посчитать, дашборд не открывать

Правило пропуска: если все выходные файлы шага уже лежат в data/, шаг не
запускается. Поэтому у организаторов, у которых в репозитории уже есть готовые
данные, сразу откроется дашборд — без токенов, ключей и интернета.

Если скрипта шага ещё нет в репозитории (участник его не дописал), шаг
пропускается с предупреждением, а не роняет всё.
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

# Общие настройки проекта. Поменять репозиторий или число PR — только здесь.
REPO = "pydantic/pydantic"
LIMIT = 150

# Конвейер: (скрипт, аргументы, файлы-результаты в data/, кто отвечает)
STEPS = [
    ("fetch_prs.py",
     ["--repo", REPO, "--limit", str(LIMIT),
      "--out", "data/prs.json",
      "--all-prs", "data/all_prs.json",
      "--history", "data/author_history.json"],
     ["prs.json", "all_prs.json", "author_history.json"],
     "участник 1"),
    ("score.py", [], ["scores.json"], "участник 2"),
    ("outcomes.py", [], ["outcomes.json"], "участник 4"),
    ("metrics.py", [], ["metrics.json"], "участник 4"),
]
APP = "app.py"  # дашборд, участник 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="пересчитать все шаги")
    ap.add_argument("--no-app", action="store_true", help="не запускать дашборд")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)

    for script, step_args, outputs, owner in STEPS:
        if not (HERE / script).exists():
            print(f"⏭  {script}: скрипта пока нет ({owner}), пропускаю")
            continue
        if not args.force and all((DATA / o).exists() for o in outputs):
            print(f"✔  {script}: результат уже есть ({', '.join(outputs)}), пропускаю")
            continue
        print(f"▶  {script} ({owner})")
        # cwd=HERE — скрипты запускаются из корня проекта, пути data/... работают.
        # check=True — если шаг упал, конвейер останавливается с ошибкой.
        subprocess.run([sys.executable, script, *step_args], cwd=HERE, check=True)

    if args.no_app:
        return
    if not (HERE / APP).exists():
        print(f"⏭  {APP}: дашборда пока нет (участник 3)")
        return
    print(f"▶  открываю дашборд {APP}")
    subprocess.run([sys.executable, "-m", "streamlit", "run", APP], cwd=HERE)


if __name__ == "__main__":
    main()
