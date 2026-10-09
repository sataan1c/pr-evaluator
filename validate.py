"""Валидация (участник 4): можно ли верить оценкам.

Запуск:
    python validate.py
    python validate.py --human human_scores.csv
    python validate.py --human-template 20        # бланк для ручной оценки 20 PR

Читает scores.json, prs.json, outcomes.json и, если есть, human_scores.csv,
scores_style.json и run_stats.json. Пишет validation.json (числа для графика и слайда)
и validation.md (тот же отчёт словами).

Четыре проверки
    1. Риск против исходов. Чаще ли PR с высоким баллом риска потом откатывали или чинили.
       Сравнение с простым размером PR показывает, видит ли модель что-то сверх «большой PR опасен».
    2. Человек против модели. Доля совпадений в пределах одного балла на PR, оценённых вручную.
    3. Тест стиля. Сдвиг баллов, когда то же изменение описано небрежно.
    4. Стабильность. Как часто три прогона одного PR дают одинаковые баллы.

Проблемных PR в любом репозитории мало, поэтому у каждой доли стоит интервал, а вывод
«связь есть» делается, только если его нижняя граница выше случайного угадывания.
"""
from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path

import style_test
from common import CRITERIA, DataError, die, read_json, read_list, setup_output, write_json, write_text, use_data_dir
from stats_util import auc, auc_interval, fisher_greater, mean, wilson

RISK_GROUPS = [("1–2", (1, 2)), ("3", (3,)), ("4–5", (4, 5))]
MIN_PROBLEMS = 5   # при меньшем числе проблемных PR вывод не делается вообще


def _rate(problems: int, total: int) -> dict:
    ci = wilson(problems, total)
    return {"n": total, "problems": problems,
            "rate": round(problems / total, 3) if total else None,
            "ci95": [round(ci[0], 3), round(ci[1], 3)] if ci else None}


def risk_validation(scores: list, prs: list, outcomes: list, key: str = "problem") -> dict:
    """Связь балла риска с откатами и исправлениями. Считаются только PR с прошедшим окном наблюдения."""
    outcome_by_number = {o["number"]: o for o in outcomes}
    lines_by_number = {p["number"]: (p.get("additions") or 0) + (p.get("deletions") or 0) for p in prs}
    rows, not_observable, no_outcome = [], 0, 0
    for record in scores:
        outcome = outcome_by_number.get(record["number"])
        if outcome is None:
            no_outcome += 1
        elif not outcome.get("observable"):
            not_observable += 1
        else:
            rows.append({"number": record["number"], "risk": record["scores"]["risk"]["score"],
                         "lines": lines_by_number.get(record["number"], 0), "problem": bool(outcome.get(key))})

    groups = []
    for name, values in RISK_GROUPS:
        members = [r for r in rows if r["risk"] in values]
        groups.append(dict(_rate(sum(r["problem"] for r in members), len(members)), risk=name))
    low, high = groups[0], groups[-1]
    problem_risk = [r["risk"] for r in rows if r["problem"]]
    clean_risk = [r["risk"] for r in rows if not r["problem"]]
    problem_lines = [r["lines"] for r in rows if r["problem"]]
    clean_lines = [r["lines"] for r in rows if not r["problem"]]

    def _auc(pos, neg):
        value, ci = auc(pos, neg), auc_interval(pos, neg)
        return None if value is None else {"value": round(value, 3), "ci95": [round(ci[0], 3), round(ci[1], 3)]}

    risk_auc = _auc(problem_risk, clean_risk)
    if len(problem_risk) < MIN_PROBLEMS or not clean_risk:
        verdict = "too_few_problems"
    elif risk_auc["ci95"][0] > 0.5:
        verdict = "supported"
    else:
        verdict = "not_shown"
    return {
        "definition": key,
        "prs_scored": len(scores),
        "prs_used": len(rows),
        "excluded_window_not_passed": not_observable,
        "excluded_no_outcome": no_outcome,
        "problems": len(problem_risk),
        "groups": groups,
        "high_vs_low_p": (round(fisher_greater(high["problems"], high["n"] - high["problems"],
                                               low["problems"], low["n"] - low["problems"]), 4)
                          if high["n"] and low["n"] else None),
        "auc_risk": risk_auc,
        "auc_size": _auc(problem_lines, clean_lines),
        "verdict": verdict,
        "problem_prs": sorted(r["number"] for r in rows if r["problem"]),
    }


def read_human(path) -> list:
    """human_scores.csv: столбец number и столбцы критериев; пустая ячейка = не оценивал.

    Разделитель запятая или точка с запятой (так сохраняет русский Excel)."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise DataError(f"не удалось прочитать ручные оценки {path}: {e}")
    header = text.splitlines()[0] if text.strip() else ""
    reader = csv.DictReader(io.StringIO(text), delimiter=";" if header.count(";") > header.count(",") else ",")
    if not reader.fieldnames or "number" not in [f.strip().lower() for f in reader.fieldnames]:
        raise DataError(f"{path}: в первой строке нужен столбец number и столбцы {', '.join(CRITERIA)}")
    rows = []
    for line_no, raw in enumerate(reader, 2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not row.get("number"):
            continue
        try:
            item = {"number": int(row["number"].lstrip("#"))}
            for c in CRITERIA:
                if row.get(c):
                    item[c] = int(row[c])
                    if not 1 <= item[c] <= 5:
                        raise ValueError
        except ValueError:
            raise DataError(f"{path}, строка {line_no}: номер PR и баллы должны быть целыми числами, баллы от 1 до 5")
        if len(item) > 1:
            rows.append(item)
    return rows


def human_agreement(scores: list, human: list) -> dict:
    by_number = {r["number"]: r for r in scores}
    report = {"prs": len({h["number"] for h in human if h["number"] in by_number}),
              "not_in_scores": sorted({h["number"] for h in human if h["number"] not in by_number}),
              "criteria": {}}
    all_diffs = []
    for c in CRITERIA:
        diffs = [by_number[h["number"]]["scores"][c]["score"] - h[c]
                 for h in human if c in h and h["number"] in by_number]
        all_diffs += diffs
        if diffs:
            report["criteria"][c] = {
                "n": len(diffs),
                "exact": round(sum(1 for d in diffs if d == 0) / len(diffs), 3),
                "within_1": round(sum(1 for d in diffs if abs(d) <= 1) / len(diffs), 3),
                "mean_model_minus_human": round(mean(diffs), 2),
            }
    report["ratings"] = len(all_diffs)
    report["within_1"] = round(sum(1 for d in all_diffs if abs(d) <= 1) / len(all_diffs), 3) if all_diffs else None
    report["exact"] = round(sum(1 for d in all_diffs if d == 0) / len(all_diffs), 3) if all_diffs else None
    return report


def stability(scores: list, run_stats: dict) -> dict:
    multi = [r for r in scores if r.get("runs", 1) > 1]
    return {
        "prs_with_several_runs": len(multi),
        "unstable": sum(1 for r in multi if r.get("unstable")),
        "all_runs_identical": (run_stats or {}).get("all_runs_identical") if multi else None,
        "evidence_removed": (run_stats or {}).get("evidence_removed"),
        "evidence_lines_cleared": (run_stats or {}).get("evidence_lines_cleared"),
        "answers_rejected_and_retried": (run_stats or {}).get("answers_rejected_and_retried"),
        "author_login_still_visible": (run_stats or {}).get("author_login_still_visible"),
    }


def build_report(scores, prs, outcomes=None, human=None, style_scores=None, run_stats=None) -> dict:
    model = (run_stats or {}).get("model")
    report = {
        "model": model,
        "synthetic": bool(model and str(model).startswith("fake")),
        "prs_scored": len(scores),
        "risk": risk_validation(scores, prs, outcomes) if outcomes else None,
        "risk_strict": risk_validation(scores, prs, outcomes, "problem_strict") if outcomes else None,
        "human": human_agreement(scores, human) if human else None,
        "style": style_test.compare(scores, style_scores) if style_scores else None,
        "stability": stability(scores, run_stats),
    }
    if report["style"]:
        report["style"].pop("pairs", None)
    return report


# ---------- отчёт словами ----------
def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _num(value, digits: int = 2, sign: bool = False) -> str:
    """Число с десятичной запятой, как принято в русском тексте."""
    return format(value, f"{'+' if sign else ''}.{digits}f").replace(".", ",")


VERDICTS = {
    "supported": "Связь есть: PR с более высоким баллом риска действительно чаще оказывались проблемными, "
                 "и нижняя граница интервала выше случайного угадывания.",
    "not_shown": "На этой выборке связь не доказана: интервал включает случайное угадывание (0,5). "
                 "Показывайте цифры как есть и не называйте это подтверждением.",
    "too_few_problems": f"Проблемных PR меньше {MIN_PROBLEMS}: для вывода данных недостаточно. "
                        "Возьмите больше PR или репозиторий, где откаты случаются чаще.",
}


def render_markdown(report: dict) -> str:
    out = ["# Отчёт о валидации", ""]
    if report["synthetic"]:
        out += ["> **Это прогон на заглушке модели.** Баллы поставила не нейросеть, а простые правила из "
                "`devtools/fake_llm.py`. Числа ниже проверяют только то, что конвейер работает; "
                "показывать их как результат нельзя.", ""]
    out += [f"Модель: {report['model'] or 'не указана'}. Оценено PR: {report['prs_scored']}.", ""]

    risk = report["risk"]
    out += ["## 1. Риск против исходов", ""]
    if not risk:
        out += ["Не посчитано: нет outcomes.json. Запустите `python outcomes.py`.", ""]
    else:
        out += [f"В расчёте {risk['prs_used']} PR, проблемных среди них {risk['problems']}. "
                f"Исключено: {risk['excluded_window_not_passed']} PR, у которых ещё не прошло окно наблюдения"
                + (f", и {risk['excluded_no_outcome']} без разметки" if risk["excluded_no_outcome"] else "") + ".", "",
                "| Балл риска | PR | Проблемных | Доля | 95% интервал |", "| --- | --- | --- | --- | --- |"]
        for g in risk["groups"]:
            ci = "—" if not g["ci95"] else f"{g['ci95'][0] * 100:.0f}–{g['ci95'][1] * 100:.0f}%"
            out.append(f"| {g['risk']} | {g['n']} | {g['problems']} | {_pct(g['rate'])} | {ci} |")
        out.append("")
        if risk["auc_risk"]:
            a, s = risk["auc_risk"], risk["auc_size"]
            out += [f"Способность различать проблемные и чистые PR (AUC; 0,5 = наугад, 1 = идеально): "
                    f"балл риска {_num(a['value'])} (95% интервал {_num(a['ci95'][0])}–{_num(a['ci95'][1])}), "
                    f"простой размер PR в строках {_num(s['value'])} ({_num(s['ci95'][0])}–{_num(s['ci95'][1])}).", ""]
        if risk["high_vs_low_p"] is not None:
            out += [f"Вероятность получить такой разрыв между группами 4–5 и 1–2 случайно: {_num(risk['high_vs_low_p'], 3)}.", ""]
        out += ["**Вывод.** " + VERDICTS[risk["verdict"]], ""]
        strict = report["risk_strict"]
        if strict and strict["problems"] != risk["problems"]:
            a = strict["auc_risk"]
            out += [f"По строгому правилу (откат или исправление с явной причинной ссылкой) проблемных {strict['problems']}"
                    + (f", AUC балла риска {_num(a['value'])} ({_num(a['ci95'][0])}–{_num(a['ci95'][1])})." if a else "."), ""]

    human = report["human"]
    out += ["## 2. Человек против модели", ""]
    if not human:
        out += ["Не посчитано: нет human_scores.csv. Бланк делается командой `python validate.py --human-template 20`.", ""]
    else:
        out += [f"Вручную оценено {human['prs']} PR, всего {human['ratings']} оценок. "
                f"Совпадение с моделью в пределах одного балла: {_pct(human['within_1'])}, точное: {_pct(human['exact'])}.", "",
                "| Критерий | Оценок | В пределах 1 балла | Точно | Модель минус человек |", "| --- | --- | --- | --- | --- |"]
        for c, item in human["criteria"].items():
            out.append(f"| {c} | {item['n']} | {_pct(item['within_1'])} | {_pct(item['exact'])} | {_num(item['mean_model_minus_human'], sign=True)} |")
        out.append("")
        if human["not_in_scores"]:
            out += [f"Нет оценок модели для PR: {human['not_in_scores']}.", ""]

    style = report["style"]
    out += ["## 3. Тест стиля", ""]
    if not style:
        out += ["Не посчитано: нет scores_style.json. Порядок запуска описан в начале `style_test.py`.", ""]
    else:
        out += [f"Описание {style['n']} PR переписано коротко и небрежно, код тот же. Средний сдвиг балла:", "",
                "| Критерий | Средний сдвиг | Без изменений | Сдвиг на 2 и больше |", "| --- | --- | --- | --- |"]
        for c in CRITERIA:
            item = style["criteria"][c]
            out.append(f"| {c} | {_num(item['mean_shift'], sign=True)} | {_pct(item['unchanged_share'])} | {item['moved_2_or_more']} |")
        out += ["", "**Вывод.** Ясность " + ("упала, как и должна" if style["clarity_dropped"] else "не упала: тест не сработал") + ". "
                + ("Остальные три балла устойчивы к стилю описания." if style["others_stable"] else
                   f"Балл {style['worst_other']} заметно сдвинулся: модель реагирует на стиль письма, а не только на код."), ""]

    stab = report["stability"]
    out += ["## 4. Стабильность и проверки ответа", ""]
    if stab["prs_with_several_runs"]:
        line = f"Несколько прогонов было у {stab['prs_with_several_runs']} PR; расхождение больше одного балла у {stab['unstable']}"
        if stab["all_runs_identical"] is not None:
            line += f"; все прогоны совпали полностью у {stab['all_runs_identical']}"
        out += [line + ".", ""]
    else:
        out += ["Каждый PR оценён один раз, стабильность не проверялась.", ""]
    if stab["evidence_removed"] is not None:
        out += [f"Ссылок на файлы вне diff удалено: {stab['evidence_removed']}; номеров строк вне изменённых мест очищено: "
                f"{stab['evidence_lines_cleared'] or 0}; ответов отклонено и запрошено заново: {stab['answers_rejected_and_retried']}.", ""]
    if stab["author_login_still_visible"]:
        out += [f"Логин автора остался виден модели (в пути к файлу или как обычное слово в коде) в PR: "
                f"{stab['author_login_still_visible']}.", ""]

    out += ["## Ограничения", "",
            "- Исходы берутся из истории PR: откаты прямым коммитом и исправления без ссылки на исходный PR не видны.",
            "- Ссылка в PR-исправлении не доказывает, что сломал именно тот PR. Строгое правило это частично снимает.",
            "- Проверен только критерий риска. Для сложности, качества и ясности независимого исхода нет, "
            "их подтверждает только сравнение с человеком.",
            "- Открытый репозиторий мог попасть в обучающие данные модели, и она могла видеть, чем закончился PR.",
            "- Уровни разработчиков в открытом репозитории заданы допущением по числу прошлых PR.", ""]
    return "\n".join(out)


def human_template(prs: list, count: int, repo: str = "") -> str:
    """Бланк для слепой ручной оценки: оценок модели в нём нет."""
    count = max(1, min(count, len(prs)))
    picked = [prs[int(i * len(prs) / count)] for i in range(count)]
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(["number", "title", "url"] + CRITERIA)
    for pr in picked:
        url = f"https://github.com/{repo}/pull/{pr['number']}" if repo else ""
        writer.writerow([pr["number"], (pr.get("title") or "").replace(";", ","), url, "", "", "", ""])
    return buf.getvalue()


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="Валидация оценок: риск против исходов, человек, стиль, стабильность")
    ap.add_argument("--prs", default="prs.json")
    ap.add_argument("--scores", default="scores.json")
    ap.add_argument("--outcomes", default="outcomes.json")
    ap.add_argument("--human", default="human_scores.csv")
    ap.add_argument("--style-scores", default="scores_style.json")
    ap.add_argument("--stats", default="run_stats.json")
    ap.add_argument("--out", default="validation.json")
    ap.add_argument("--report", default="validation.md")
    ap.add_argument("--human-template", type=int, metavar="N", help="записать бланк ручной оценки N PR и выйти")
    ap.add_argument("--repo", default="", help="owner/name для ссылок в бланке")
    use_data_dir(ap, 'prs', 'scores', 'outcomes', 'human', 'style_scores', 'stats', 'out', 'report')
    args = ap.parse_args()
    try:
        prs = read_list(args.prs, "файл PR")
        if args.human_template:
            repo = args.repo
            folder = Path(args.prs).parent          # бланк и история лежат там же, где PR
            if not repo and (folder / "author_history.json").is_file():
                repo = read_json(folder / "author_history.json").get("repo", "")
            blank = folder / "human_scores_template.csv"
            write_text(blank, "﻿" + human_template(prs, args.human_template, repo))
            print(f"Бланк на {min(args.human_template, len(prs))} PR -> {blank}. Заполните столбцы "
                  f"{', '.join(CRITERIA)} баллами от 1 до 5, не глядя в scores.json, и сохраните как {args.human}.")
            return
        scores = read_list(args.scores, "файл оценок")
        optional = lambda path, reader, what: reader(path, what) if Path(path).is_file() else None
        report = build_report(
            scores, prs,
            outcomes=optional(args.outcomes, read_list, "файл исходов"),
            human=read_human(args.human) if Path(args.human).is_file() else None,
            style_scores=optional(args.style_scores, read_list, "файл оценок теста стиля"),
            run_stats=optional(args.stats, read_json, "статистика прогона"),
        )
    except (DataError, KeyError, TypeError) as e:
        die(f"{e}" if isinstance(e, DataError) else f"файл не того вида, не хватает поля {e}. Запустите python check_data.py")
    write_json(args.out, report)
    write_text(args.report, render_markdown(report))

    print(f"Отчёт -> {args.report}, числа -> {args.out}")
    if report["synthetic"]:
        print("  ВНИМАНИЕ: оценки поставлены заглушкой, это не результат.")
    risk, human, style = report["risk"], report["human"], report["style"]
    if risk:
        high, low = risk["groups"][-1], risk["groups"][0]
        print(f"  1) проблемных при риске 4–5: {_pct(high['rate'])} ({high['problems']} из {high['n']}), "
              f"при риске 1–2: {_pct(low['rate'])} ({low['problems']} из {low['n']})")
        print("     " + VERDICTS[risk["verdict"]])
    else:
        print("  1) риск против исходов: нет outcomes.json")
    print(f"  2) совпадение с человеком в пределах 1 балла: {_pct(human['within_1'])} на {human['ratings']} оценках"
          if human else "  2) человек против модели: нет human_scores.csv")
    print("  3) тест стиля, средний сдвиг: " + ", ".join(f"{c} {_num(style['criteria'][c]['mean_shift'], sign=True)}" for c in CRITERIA)
          if style else "  3) тест стиля: нет scores_style.json")


if __name__ == "__main__":
    main()
