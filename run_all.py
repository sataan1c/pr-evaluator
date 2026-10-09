"""Весь конвейер одной командой: посчитать всё, чего не хватает, и открыть дашборд.

    python run_all.py                 # данные из data/: досчитать недостающее, открыть дашборд
    python run_all.py --no-app        # только посчитать, дашборд не открывать
    python run_all.py --force         # заново выгрузить PR из GitHub и оценить всё, чего нет в кэше
    python run_all.py --rescore       # оценить заново после правки рубрики, без выгрузки
    python run_all.py --update --no-app   # для cron: свежий список PR из GitHub, оценить только новые
    python run_all.py --no-style --runs 1                       # быстрый и дешёвый прогон
    python run_all.py --repo owner/name --workdir other_data    # другой репозиторий в другой папке

Что нужно каждому шагу
    Сеть и токен GitHub    только выгрузке. Она запускается с --force или --repo.
    Ключ модели            только оценке. Она запускается, если есть неоценённые PR и ключ настроен
                           (один раз: python score.py, он спросит сервис и ключ и сохранит их в .env).
    Ничего                 остальным шагам: это расчёты на готовых файлах, они идут секунды
                           и пересчитываются при каждом запуске.

Поэтому там, где оценки уже лежат в data/, команда не просит ни токена, ни ключа, ни сети
и сразу открывает дашборд. Если оценок нет и ключа нет, она считает то, что от оценок не
зависит, и открывает дашборд с подсказкой, как получить оценки.

Шаги
    1. fetch_prs.py   GitHub -> prs.json, all_prs.json, author_history.json
    2. check_data.py  формат prs.json
    3. score.py       prs.json + rubric.md -> scores.json, run_stats.json
    4. outcomes.py    prs.json + all_prs.json -> outcomes.json
    5. metrics.py     prs.json + scores.json -> metrics.json
    6. style_test.py  prs_style.json, scores_style.json, style_report.json     (нет при --no-style)
    7. validate.py    -> validation.json, validation.md; chart.py -> risk_chart.png
    8. check_data.py  формат и согласованность всех файлов

Уже оценённые PR берутся из кэша (папка cache/), поэтому повторный запуск после сбоя
продолжает с того же места и не тратит деньги дважды.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Общие настройки проекта. Поменять репозиторий или число PR — только здесь.
REPO = "pydantic/pydantic"
LIMIT = 150

KEY_VARS = ("SCORER_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def step(title: str, script: str, args: list, workdir: Path, allow: tuple = (0,)) -> int:
    print(f"\n=== {title} ===", flush=True)
    started = time.time()
    code = subprocess.call([sys.executable, str(HERE / script)] + [str(a) for a in args], cwd=str(workdir))
    print(f"--- {script}: код выхода {code}, {time.time() - started:.1f} с", flush=True)
    if code not in allow:
        print(f"\nКонвейер остановлен на шаге «{title}». Исправьте причину и запустите ещё раз: "
              "сделанное сохранено, оценённые PR возьмутся из кэша.", file=sys.stderr)
        sys.exit(code or 1)
    return code


def model_key_is_set(work: Path) -> bool:
    """Есть ли у score.py ключ: в переменных окружения или в .env рядом со скриптами или с данными."""
    if any(os.environ.get(name) for name in KEY_VARS):
        return True
    if os.environ.get("SCORER_PROVIDER", "").strip().lower() == "claude-cli":   # подписка Claude Code: ключ не нужен
        return True
    for folder in dict.fromkeys((HERE, work)):
        try:
            lines = (folder / ".env").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            name, _, value = line.strip().partition("=")
            if name.strip() in KEY_VARS and value.strip().strip("\"'"):
                return True
            if name.strip() == "SCORER_PROVIDER" and value.strip().strip("\"'").lower() == "claude-cli":
                return True
    return False


def numbers(path: Path) -> set:
    """Номера PR в файле-списке. Нет файла или он не читается: пустое множество."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return {item.get("number") for item in data if isinstance(item, dict)}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def score_step(title: str, work: Path, source: str, out: str, stats: str, runs: int, force: bool, have_key: bool) -> str:
    """Оценивает PR из source, если есть что оценивать и чем. Возвращает, что получилось:
    'done' (все оценены), 'partial' (оценена часть), 'none' (оценок нет)."""
    wanted, scored = numbers(work / source), numbers(work / out)
    left = wanted - scored
    if not left and scored and not force:
        print(f"\n=== {title}: пропущено, оценки уже есть для всех {len(wanted)} PR ({out}) ===")
        return "done"
    if not have_key:
        print(f"\n=== {title}: пропущено, ключ модели не настроен ===")
        print(f"Не оценено {len(left)} PR из {len(wanted)}. Чтобы оценить: запустите один раз `python score.py`, "
              "он спросит сервис и ключ и сохранит их в .env, затем снова `python run_all.py`.")
        return "partial" if scored else "none"
    command = ["--prs", source, "--out", out, "--stats", stats, "--runs", runs]
    if work == HERE / "data":
        command += ["--cache", HERE / "cache"]     # тот же кэш, что у `python score.py` из корня проекта
    code = step(title, "score.py", command, work, allow=(0, 1))
    if not (work / out).is_file():
        print("\nscore.py не оценил ни одного PR, продолжать не с чем.", file=sys.stderr)
        sys.exit(1)
    return "partial" if code == 1 else "done"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Запуск всего конвейера по шагам, затем дашборд")
    ap.add_argument("--repo", help=f"owner/name: выгрузить PR этого репозитория. С --force без --repo берётся {REPO}")
    ap.add_argument("--limit", type=int, default=LIMIT, help="сколько принятых PR выгружать")
    ap.add_argument("--min-age-days", type=float, default=30,
                    help="брать PR не моложе стольких дней: у свежих ещё не было времени на откат")
    ap.add_argument("--window-days", type=float, default=14, help="окно, в котором ищутся исправления")
    ap.add_argument("--runs", type=int, default=3, help="прогонов модели на один PR")
    ap.add_argument("--style-count", type=int, default=30, help="сколько PR взять в тест стиля")
    ap.add_argument("--no-style", action="store_true", help="пропустить тест стиля (он стоит ещё один прогон модели)")
    ap.add_argument("--workdir", default=str(HERE / "data"), help="папка для данных и результатов (по умолчанию data/)")
    ap.add_argument("--force", action="store_true", help="заново выгрузить PR и оценить всё, чего нет в кэше")
    ap.add_argument("--rescore", action="store_true",
                    help="запустить оценку, даже если оценки есть для всех PR: нужно после правки рубрики")
    ap.add_argument("--update", action="store_true",
                    help="режим для cron/планировщика: заново взять список PR из GitHub (сами PR из кэша raw/), "
                         "оценить только новые (остальное из кэша cache/) и пересчитать метрики")
    ap.add_argument("--no-app", action="store_true", help="не открывать дашборд")
    ap.add_argument("--port", type=int, default=8765, help="порт дашборда")
    args = ap.parse_args()

    work = Path(args.workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    warnings = []
    repo = args.repo or (REPO if args.force or args.update else None)

    if repo:
        step("1/8 Выгрузка PR из GitHub", "fetch_prs.py",
             ["--repo", repo, "--limit", args.limit, "--min-age-days", args.min_age_days,
              "--out", "prs.json", "--all-prs", "all_prs.json", "--history", "author_history.json"]
             + (["--refresh-list"] if args.update else []), work)
    elif not (work / "prs.json").is_file():
        print(f"В {work} нет prs.json. Выгрузить его: python run_all.py --force (репозиторий {REPO}) "
              "или python run_all.py --repo owner/name.", file=sys.stderr)
        return 2
    else:
        print("\n=== 1/8 Выгрузка пропущена: берётся готовый prs.json (заново: --force или --repo) ===")

    step("2/8 Проверка prs.json", "check_data.py", ["--dir", ".", "--only-prs"], work)

    have_key = model_key_is_set(work)
    scored = score_step("3/8 Оценка PR моделью", work, "prs.json", "scores.json", "run_stats.json",
                        args.runs, args.force or args.rescore, have_key)
    if scored == "partial":
        warnings.append("оценена только часть PR: расчёты идут по оценённым")

    if (work / "all_prs.json").is_file():
        step("4/8 Разметка откатов и исправлений", "outcomes.py",
             ["--prs", "prs.json", "--all-prs", "all_prs.json", "--window-days", args.window_days, "--out", "outcomes.json"], work)
    else:
        print("\n=== 4/8 Разметка пропущена: нет all_prs.json (он создаётся выгрузкой) ===")
        warnings.append("нет all_prs.json: связь риска с откатами не посчитана")

    if scored == "none":
        print("\n=== 5–8 пропущены: множителю и проверке метода нужны оценки ===")
        warnings.append("оценок модели пока нет: множители и проверка метода не посчитаны")
    else:
        step("5/8 Множитель", "metrics.py", ["--prs", "prs.json", "--scores", "scores.json", "--out", "metrics.json"], work)

        if args.no_style:
            print("\n=== 6/8 Тест стиля пропущен (--no-style) ===")
        else:
            step("6/8 Тест стиля: подготовка", "style_test.py", ["prepare", "--prs", "prs.json", "--count", args.style_count], work)
            styled = score_step("6/8 Тест стиля: оценка", work, "prs_style.json", "scores_style.json", "run_stats_style.json",
                                args.runs, args.force or args.rescore, have_key)
            if styled == "partial":
                warnings.append("в тесте стиля часть PR не оценена")
            if styled == "none":
                warnings.append("тест стиля не оценён: в отчёте не будет третьего числа")
            else:
                step("6/8 Тест стиля: сравнение", "style_test.py", ["compare"], work)

        step("7/8 Валидация", "validate.py", [], work)
        if (work / "outcomes.json").is_file():
            # График не обязателен: без matplotlib шаг пропускается и на остальное не влияет.
            if step("7/8 График для слайда", "chart.py", [], work, allow=(0, 2)):
                print("График не построен (обычно помогает: pip install matplotlib). Остальное от него не зависит.")
        if step("8/8 Проверка всех файлов", "check_data.py", ["--dir", "."], work, allow=(0, 1)):
            warnings.append("проверка файлов нашла ошибки, см. вывод шага 8")

    print("\n" + "=" * 60)
    print(f"Готово. Результаты в {work}:")
    for name, what in [("scores.json", "оценки PR с объяснениями"), ("metrics.json", "множители по разработчикам"),
                       ("outcomes.json", "откаты и исправления"), ("validation.md", "отчёт о валидации словами"),
                       ("validation.json", "числа для графика и слайда"), ("run_stats.json", "стоимость и стабильность прогона")]:
        if (work / name).is_file():
            print(f"  {name:<18}{what}")
    for w in warnings:
        print("ВНИМАНИЕ: " + w)
    code = 1 if warnings else 0

    if not args.no_app:
        print()
        subprocess.call([sys.executable, str(HERE / "app.py"), "serve", "--dir", str(work), "--port", str(args.port)])
    return code


if __name__ == "__main__":
    sys.exit(main())
