"""Разметка исходов (участник 4):  prs.json + all_prs.json -> outcomes.json

Запуск:
    python outcomes.py
    python outcomes.py --prs prs.json --all-prs all_prs.json --window-days 14 --out outcomes.json

Что считается проблемным PR
    откат      принятый PR со словом revert в заголовке (или «Reverts #N» в описании),
               который указывает на наш PR: номером или цитатой его заголовка;
    исправление принятый PR со словами fix / hotfix / regression / broke в заголовке,
               который ссылается на наш PR по номеру и принят не позже чем через
               --window-days дней после него.
    Ссылка на PR в исправлении не всегда значит «это он сломал», поэтому есть и строгий
    вариант: рядом с номером стоит причинная фраза (regression from #N, introduced in #N,
    broke in #N, caused by #N, follow-up fix for #N). Валидация считается по обоим вариантам.

Модель в этом шаге не участвует: исходы берутся только из истории GitHub.

Чего здесь не видно
    - откаты прямым коммитом в main, без PR;
    - поломки, которые чинили без ссылки на исходный PR;
    - PR, у которых окно наблюдения ещё не прошло: они помечены "observable": false,
      и валидация их не считает.
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict

from common import DataError, days_between, die, read_list, setup_output, write_json, use_data_dir

# Откат объявляют в начале заголовка: «Revert "..."», «chore: revert #12», «Partially revert #12».
REVERT_START = re.compile(r"^\W*(?:[\w-]+\W+){0,2}?revert(s|ed|ing)?\b", re.I)
REVERTS_REF = re.compile(r"\breverts?\s+(?:pull request\s+)?(?:[\w.-]+/[\w.-]+)?#(\d+)", re.I)
QUOTED_TITLE = re.compile(r"revert\w*\W*\"(.+)\"", re.I | re.S)
FIX_WORD = re.compile(r"\b(hot-?fix(es|ed)?|fix(es|ed|ing)?|regress(ion|ions|ed)?|broke|broken)\b", re.I)
# #123, но не other/repo#123 и не часть слова
PR_REF = re.compile(r"(?<![\w/.-])#(\d+)\b|/pull/(\d+)\b")
CAUSAL = (
    r"(regress\w*|introduc\w*|caus\w*|broke\w*|break\w*|breakage|bug|since|after|"
    r"follow[- ]?up|fix(es|ed|ing)?|hot-?fix)"
)


def is_revert_title(title: str) -> bool:
    """«Revert "Fix cache"» да; «Fix revert button» нет: это исправление, а не откат."""
    m = REVERT_START.search(title or "")
    if not m:
        return False
    prefix = re.sub(r"revert\w*$", "", title[:m.end()], flags=re.I)
    return not FIX_WORD.search(prefix)


def references(text: str) -> set:
    """Номера PR, упомянутые в тексте."""
    return {int(a or b) for a, b in PR_REF.findall(text or "")}


def causal_references(text: str) -> set:
    """Номера PR, рядом с которыми стоит причинная фраза: «regression from #12», «introduced in #12»."""
    found = set()
    for m in re.finditer(CAUSAL + r"(?:\W+\w+){0,3}?\W+#(?P<number>\d+)\b", text or "", re.I):
        found.add(int(m.group("number")))
    return found


def revert_targets(pr: dict, merged_by_title: dict) -> set:
    """На какие PR указывает этот откат. Пусто, если это не откат.

    merged_by_title: заголовок в нижнем регистре -> [(merged_at, номер), ...]."""
    title, body = pr.get("title") or "", pr.get("body") or ""
    targets = {int(n) for n in REVERTS_REF.findall(body)} | {int(n) for n in REVERTS_REF.findall(title)}
    if not is_revert_title(title):
        return targets  # заголовок не про откат: верим только явному «Reverts #N»
    quoted = QUOTED_TITLE.search(title)
    original = quoted.group(1).strip() if quoted else ""
    # Откат отката: Revert "Revert "Fix cache (#123)"" отменяет откат, а не PR #123.
    # Номер внутри вложенной цитаты поэтому не берётся; цель ищется по «Reverts #N» или по заголовку отката.
    if not is_revert_title(original):
        targets |= references(title)                         # Revert "Fix cache (#123)", Revert #123
    if quoted and not targets:
        # Номера нигде нет, есть только цитата заголовка. Одинаковые заголовки встречаются,
        # поэтому берётся один PR: последний с таким заголовком, принятый до отката.
        plain = re.sub(r"\s*\(#\d+\)\s*$", "", original)
        earlier = [item for candidate in {original.lower(), plain.lower()}
                   for item in merged_by_title.get(candidate, ())
                   if item[1] != pr.get("number") and days_between(item[0], pr["merged_at"]) > 0]
        if earlier:
            targets.add(max(earlier)[1])
    return targets


def label(prs: list, all_prs: list, window_days: float = 14) -> tuple:
    """Возвращает (записи outcomes, сведения о данных)."""
    scored = {}
    for pr in prs:
        if not isinstance(pr.get("number"), int) or not pr.get("merged_at"):
            raise DataError(f"в prs.json у PR {pr.get('number')!r} нет number или merged_at")
        scored[pr["number"]] = pr
    if not scored:
        raise DataError("в prs.json нет ни одного PR")

    merged_by_title, listed = defaultdict(list), set()
    for pr in list(all_prs) + list(prs):
        if pr.get("title") and pr.get("merged_at") and isinstance(pr.get("number"), int) and pr["number"] not in listed:
            listed.add(pr["number"])
            merged_by_title[pr["title"].strip().lower()].append((pr["merged_at"], pr["number"]))

    reverted_by, fixed_by, fixed_explicit = defaultdict(set), defaultdict(set), defaultdict(set)
    for later in all_prs:
        merged = later.get("merged_at")
        if not merged or later.get("is_bot"):
            continue  # закрытый без мержа откат ничего не откатил
        number, title, body = later.get("number"), later.get("title") or "", later.get("body") or ""
        targets = revert_targets(later, merged_by_title)
        for t in targets:
            if t in scored and t != number and days_between(scored[t]["merged_at"], merged) > 0:
                reverted_by[t].add(number)
        if is_revert_title(title) or not FIX_WORD.search(title):
            continue
        text = title + "\n" + body
        explicit = causal_references(text)
        for t in references(text):
            if t not in scored or t == number or t in targets:
                continue
            gap = days_between(scored[t]["merged_at"], merged)
            if 0 < gap <= window_days:
                fixed_by[t].add(number)
                if t in explicit:
                    fixed_explicit[t].add(number)

    stamps = [p.get(k) for p in all_prs for k in ("updated_at", "closed_at", "merged_at") if p.get(k)]
    if not stamps:
        raise DataError("в all_prs.json нет ни одной даты: нечем определить момент снимка данных")
    snapshot = max(stamps)
    updated = [p["updated_at"] for p in all_prs if p.get("updated_at")]
    covered_since = min(updated) if updated else None
    period_start = min(pr["merged_at"] for pr in scored.values())

    records = []
    for number in sorted(scored):
        merged_at = scored[number]["merged_at"]
        observed = max(0.0, days_between(merged_at, snapshot))
        records.append({
            "number": number,
            "merged_at": merged_at,
            "reverted": bool(reverted_by[number]),
            "reverted_by": sorted(reverted_by[number]),
            "fixed_by": sorted(fixed_by[number]),
            "fixed_by_explicit": sorted(fixed_explicit[number]),
            "problem": bool(reverted_by[number] or fixed_by[number]),
            "problem_strict": bool(reverted_by[number] or fixed_explicit[number]),
            "observed_days": round(observed, 1),
            "observable": observed >= window_days,
        })
    info = {
        "window_days": window_days,
        "snapshot": snapshot,
        "period_start": period_start,
        "covered_since": covered_since,
        # Список закрытых PR должен начинаться не позже первого оцениваемого PR, иначе часть откатов не попала в данные.
        "history_covers_period": covered_since is None or covered_since <= period_start,
        "closed_prs_seen": len(all_prs),
    }
    return records, info


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="Разметка откатов и срочных исправлений по истории GitHub")
    ap.add_argument("--prs", default="prs.json")
    ap.add_argument("--all-prs", default="all_prs.json")
    ap.add_argument("--window-days", type=float, default=14, help="сколько дней после мержа ждём исправления")
    ap.add_argument("--out", default="outcomes.json")
    use_data_dir(ap, 'prs', 'all_prs', 'out')
    args = ap.parse_args()
    try:
        records, info = label(read_list(args.prs, "файл PR"), read_list(args.all_prs, "список закрытых PR"),
                              args.window_days)
    except DataError as e:
        die(str(e))
    write_json(args.out, records)

    seen = [r for r in records if r["observable"]]
    print(f"Размечено {len(records)} PR -> {args.out}")
    print(f"  окно наблюдения {args.window_days:g} дн. прошло у {len(seen)} PR, не прошло у {len(records) - len(seen)}")
    print(f"  среди наблюдаемых: откачено {sum(r['reverted'] for r in seen)}, "
          f"исправляли {sum(bool(r['fixed_by']) for r in seen)}, "
          f"всего проблемных {sum(r['problem'] for r in seen)} "
          f"(по строгому правилу {sum(r['problem_strict'] for r in seen)})")
    if not info["history_covers_period"]:
        print(f"ВНИМАНИЕ: список закрытых PR начинается с {info['covered_since'][:10]}, а первый оцениваемый PR "
              f"принят {info['period_start'][:10]}. Часть откатов могла не попасть в данные: "
              "перезапустите fetch_prs.py с большим --all-pages.")
    if not seen:
        print("ВНИМАНИЕ: ни у одного PR окно наблюдения не прошло. Возьмите PR постарше: "
              "fetch_prs.py --min-age-days 30.")
    for r in records:
        if r["problem"]:
            how = (f"откат в #{r['reverted_by'][0]}" if r["reverted"] else f"исправление в #{r['fixed_by'][0]}")
            print(f"  #{r['number']}: {how}{'' if r['observable'] else ' (окно не прошло)'}")


if __name__ == "__main__":
    main()
