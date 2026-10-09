"""Тест стиля: меняет ли оценку то, как написано описание PR.

Модель не видит автора, поэтому подменять имя бессмысленно. Проверяется то, что она
видит: одни и те же изменения в коде с аккуратным описанием и с небрежным. Ожидание:
ясность падает, а сложность, качество и риск остаются на месте. Если они тоже
сдвигаются, система наказывает за стиль письма, например людей, для которых
английский не родной.

Запуск в три шага:
    python style_test.py prepare                      # prs.json -> prs_style.json (30 PR)
    python score.py --prs prs_style.json --out scores_style.json --stats run_stats_style.json
    python style_test.py compare                      # scores.json + scores_style.json -> style_report.json

Порча описания детерминирована: модель для неё не нужна, повторный запуск даёт тот же файл.
"""
from __future__ import annotations

import argparse
import re

from common import CRITERIA, DataError, die, read_list, setup_output, write_json, use_data_dir

MIN_BODY_CHARS = 80   # у PR с пустым описанием портить нечего
OTHERS = ["complexity", "quality", "risk"]


def _sloppy(text: str) -> str:
    """Строчными буквами, без пунктуации, в каждом втором длинном слове переставлены две буквы."""
    words = re.sub(r"[^\w\s-]", " ", text.lower()).split()
    out, long_words = [], 0
    for word in words:
        if len(word) > 4 and word.isalpha():
            if long_words % 2 == 0:
                mid = len(word) // 2
                word = word[:mid - 1] + word[mid] + word[mid - 1] + word[mid + 1:]
            long_words += 1
        out.append(word)
    return " ".join(out)


def degrade(title: str, body: str) -> tuple:
    """Короткий небрежный вариант заголовка и описания. Код и ревью не трогаются."""
    plain = re.sub(r"```.*?```", " ", body or "", flags=re.S)        # блоки кода
    plain = re.sub(r"<!--.*?-->", " ", plain, flags=re.S)            # комментарии шаблона PR
    plain = re.sub(r"https?://\S+|[#*_`>|\[\]()]", " ", plain)       # ссылки и разметка
    lines = [line.strip() for line in plain.splitlines() if len(line.strip().split()) >= 3]
    first = re.split(r"(?<=[.!?])\s", lines[0])[0] if lines else ""
    return _sloppy(" ".join((title or "").split()[:4])), _sloppy(" ".join(first.split()[:10]))


def prepare(prs: list, count: int = 30) -> list:
    """Равномерно по списку выбирает PR с содержательным описанием и портит описание."""
    eligible = [pr for pr in prs if len((pr.get("body") or "").strip()) >= MIN_BODY_CHARS]
    if not eligible:
        raise DataError(f"нет PR с описанием длиннее {MIN_BODY_CHARS} символов: тест стиля не на чем проводить")
    count = min(count, len(eligible))
    picked = [eligible[int(i * len(eligible) / count)] for i in range(count)]
    out = []
    for pr in picked:
        title, body = degrade(pr.get("title"), pr.get("body"))
        out.append(dict(pr, title=title, body=body, style_degraded=True))
    return out


def compare(base: list, style: list) -> dict:
    """Сдвиг каждого балла после порчи описания (испорченный минус исходный)."""
    base_by_number = {r["number"]: r for r in base}
    pairs = [(base_by_number[r["number"]], r) for r in style if r.get("number") in base_by_number]
    if not pairs:
        raise DataError("в двух файлах оценок нет общих PR: сначала оцените prs.json, затем prs_style.json")
    report = {"n": len(pairs), "criteria": {}}
    for c in CRITERIA:
        shifts = [s["scores"][c]["score"] - b["scores"][c]["score"] for b, s in pairs]
        report["criteria"][c] = {
            "mean_shift": round(sum(shifts) / len(shifts), 2),
            "mean_abs_shift": round(sum(abs(x) for x in shifts) / len(shifts), 2),
            "unchanged_share": round(sum(1 for x in shifts if x == 0) / len(shifts), 2),
            "moved_2_or_more": sum(1 for x in shifts if abs(x) >= 2),
        }
    crit = report["criteria"]
    report["clarity_dropped"] = crit["clarity"]["mean_shift"] < 0
    # Порог: в среднем меньше трети балла. Жёсткость порога можно обсуждать, поэтому рядом лежат сами сдвиги.
    report["others_stable"] = all(abs(crit[c]["mean_shift"]) <= 0.33 for c in OTHERS)
    report["worst_other"] = max(OTHERS, key=lambda c: abs(crit[c]["mean_shift"]))
    report["pairs"] = [{"number": b["number"],
                        **{c: [b["scores"][c]["score"], s["scores"][c]["score"]] for c in CRITERIA}} for b, s in pairs]
    return report


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="Тест стиля: устойчивость оценки к небрежному описанию")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="подготовить PR с испорченным описанием")
    p.add_argument("--prs", default="prs.json")
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--out", default="prs_style.json")
    c = sub.add_parser("compare", help="сравнить оценки до и после")
    c.add_argument("--base", default="scores.json")
    c.add_argument("--style", default="scores_style.json")
    c.add_argument("--out", default="style_report.json")
    use_data_dir(p, 'prs', 'out')
    use_data_dir(c, 'base', 'style', 'out')
    args = ap.parse_args()
    try:
        if args.command == "prepare":
            out = prepare(read_list(args.prs, "файл PR"), args.count)
            write_json(args.out, out)
            print(f"Подготовлено {len(out)} PR с испорченным описанием -> {args.out}")
            print(f"  пример, PR #{out[0]['number']}:  заголовок «{out[0]['title']}», описание «{out[0]['body']}»")
            print(f"Дальше: python score.py --prs {args.out} --out scores_style.json --stats run_stats_style.json")
        else:
            report = compare(read_list(args.base, "файл оценок"), read_list(args.style, "файл оценок теста стиля"))
            write_json(args.out, report)
            print(f"Тест стиля на {report['n']} PR -> {args.out}")
            for crit in CRITERIA:
                item = report["criteria"][crit]
                shift = f"{item['mean_shift']:+.2f}".replace(".", ",")
                print(f"  {crit:<11} средний сдвиг {shift}, без изменений {item['unchanged_share']:.0%}, "
                      f"сдвиг на 2 и больше у {item['moved_2_or_more']} PR")
            print("  ясность упала: " + ("да" if report["clarity_dropped"] else "НЕТ"))
            print("  остальные три балла устойчивы: " + ("да" if report["others_stable"] else
                  f"НЕТ, сильнее всего сдвинулся {report['worst_other']}"))
    except DataError as e:
        die(str(e))


if __name__ == "__main__":
    main()
