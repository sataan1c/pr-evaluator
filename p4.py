"""Вся работа участника 4 одной командой.

    python p4.py                # файлы в data/ (или в текущей папке, если prs.json лежит в ней)
    python p4.py --dir sample   # на образце из комплекта

Шаги
    1. outcomes.py    prs.json + all_prs.json -> outcomes.json        (откаты и исправления)
    2. metrics.py     prs.json + scores.json  -> metrics.json         (множитель по разработчикам)
    3. style_test.py  scores.json + scores_style.json -> style_report.json   (если есть scores_style.json)
    4. validate.py    -> validation.json, validation.md               (отчёт и три числа)
    5. chart.py       -> risk_chart.png                               (график для слайда)
    6. check_data.py  формат и согласованность всех файлов

Перед запуском печатает, какие входные файлы на месте и кто из команды должен прислать
недостающие. Без необязательных файлов шаг пропускается, а отчёт говорит, чего не хватило.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from common import use_data_dir

HERE = Path(__file__).resolve().parent

INPUTS = [
    # файл, обязателен, от кого, зачем
    ("prs.json", True, "участник 1", "PR с авторами и датами мержа"),
    ("scores.json", True, "участник 2", "оценки модели (до них: python mock_scores.py)"),
    ("all_prs.json", False, "участник 1", "история закрытых PR: без неё нет разметки откатов и графика"),
    ("author_history.json", False, "участник 1", "число прошлых PR автора: без неё уровень у всех по умолчанию"),
    ("human_scores.csv", False, "участник 5", "ручные оценки: без них нет сравнения «человек против модели»"),
    ("scores_style.json", False, "участник 2", "оценки теста стиля: без них нет третьего числа"),
]


def run(title: str, script: str, args: list, work: Path, allow: tuple = (0,)) -> int:
    print(f"\n=== {title} ===", flush=True)
    code = subprocess.call([sys.executable, str(HERE / script)] + [str(a) for a in args], cwd=str(work))
    if code not in allow:
        print(f"\nОстановлено на шаге «{title}» (код {code}). Сообщение об ошибке выше.", file=sys.stderr)
        sys.exit(code or 1)
    return code


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Все шаги участника 4: исходы, множитель, валидация, график, проверка")
    ap.add_argument("--dir", default=".", help="папка с prs.json, scores.json и остальными файлами")
    ap.add_argument("--window-days", type=float, default=14, help="окно, в котором ищутся исправления")
    ap.add_argument("--dark", action="store_true", help="график на тёмном фоне")
    use_data_dir(ap, 'dir')
    args = ap.parse_args()
    work = Path(args.dir).resolve()
    if not work.is_dir():
        print(f"Папки {work} нет.", file=sys.stderr)
        sys.exit(2)

    print(f"Входные файлы в {work}:")
    have = {}
    for name, required, owner, why in INPUTS:
        have[name] = (work / name).is_file()
        mark = "есть " if have[name] else ("НЕТ  " if required else "нет  ")
        print(f"  {mark}{name:<22}{owner:<12}{why}")
    missing = [name for name, required, _, _ in INPUTS if required and not have[name]]
    if missing:
        print(f"\nБез {', '.join(missing)} считать нечего.", file=sys.stderr)
        sys.exit(2)

    notes = []
    if have["all_prs.json"]:
        run("1/6 Откаты и исправления", "outcomes.py", ["--window-days", args.window_days], work)
    else:
        print("\n=== 1/6 Пропущено: нет all_prs.json ===")
        notes.append("нет all_prs.json: разметки откатов и графика не будет")
    run("2/6 Множитель", "metrics.py", [], work)
    if have["scores_style.json"]:
        run("3/6 Тест стиля", "style_test.py", ["compare"], work)
    else:
        print("\n=== 3/6 Пропущено: нет scores_style.json ===")
    run("4/6 Валидация", "validate.py", [], work)
    if (work / "outcomes.json").is_file():
        # График не должен блокировать остальное: без matplotlib шаг пропускается с предупреждением.
        if run("5/6 График", "chart.py", ["--dark"] if args.dark else [], work, allow=(0, 2)):
            notes.append("график не построен, причина в выводе шага 5 (обычно: pip install matplotlib)")
    else:
        print("\n=== 5/6 Пропущено: без outcomes.json график строить не из чего ===")
    if run("6/6 Проверка файлов", "check_data.py", ["--dir", "."], work, allow=(0, 1)):
        notes.append("проверка файлов нашла ошибки, см. шаг 6")

    print("\n" + "=" * 60)
    print("Готово. Что отдавать дальше:")
    for name, whom in [("metrics.json", "участнику 3 для профиля и сводки"), ("validation.json", "участнику 3 для графика на дашборде"),
                       ("risk_chart.png", "участнику 5 на слайд"), ("validation.md", "участнику 5: три числа и ограничения")]:
        if (work / name).is_file():
            print(f"  {name:<18}{whom}")
    for note in notes:
        print("ВНИМАНИЕ: " + note)
    sys.exit(1 if notes else 0)


if __name__ == "__main__":
    main()
