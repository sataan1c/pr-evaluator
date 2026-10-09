"""Отчёты для REST API: по сотруднику и по команде.

Это часть 3 схемы POC («REST API для отчётов»). Данные те же, что видит дашборд:
prs.json, scores.json, metrics.json из папки с результатами. Сервер их отдаёт так
(его поднимает `python app.py`):

    GET /api/reports/employees/{employeeId}      employeeId = логин автора на GitHub
    GET /api/reports/team
    у обоих: ?since=ГГГГ-ММ-ДД&until=ГГГГ-ММ-ДД  — период по дате мержа

Ответ — JSON. Множитель и уровень берутся из metrics.json (их считает metrics.py за
весь период). Если задан период, медианы и список PR пересчитываются по нему, а
множитель остаётся общим: в ответе это помечено полем multiplier_period = "all".
"""
from __future__ import annotations

import statistics
from datetime import date

CRITERIA = ["complexity", "quality", "risk", "clarity"]


def period_problem(since: str, until: str) -> str | None:
    """Почему период задан неверно, или None. Даты сравниваются как строки, поэтому формат должен быть
    строго ГГГГ-ММ-ДД: «2026-8-1» иначе молча дал бы пустой отчёт вместо ошибки."""
    for name, value in (("since", since), ("until", until)):
        if not value:
            continue
        try:
            if len(value) != 10:
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            return f"{name} must be a date in the form YYYY-MM-DD, got {value!r}"
    if since and until and since > until:
        return f"since ({since}) is later than until ({until})"
    return None


def _in_period(pr: dict, since: str, until: str) -> bool:
    day = (pr.get("merged_at") or "")[:10]
    return (not since or day >= since) and (not until or day <= until)


def _scored_prs(data: dict, since: str = "", until: str = "") -> list[dict]:
    """PR с оценками за период: [{number, title, author, merged_at, type, scores{...}, summary, unstable}]."""
    by_number = {s.get("number"): s for s in data.get("scores") or [] if isinstance(s, dict)}
    types = data.get("types") if isinstance(data.get("types"), dict) else {}
    type_by = {t.get("number"): t for t in types.get("prs") or [] if isinstance(t, dict)}
    out = []
    for pr in data.get("prs") or []:
        s = by_number.get(pr.get("number"))
        if not s or not _in_period(pr, since, until):
            continue
        out.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "author": pr.get("author", ""),
            "merged_at": pr.get("merged_at"),
            "summary": s.get("summary", ""),
            "unstable": bool(s.get("unstable")),
            "type": (type_by.get(pr.get("number")) or {}).get("type"),
            "scores": {c: (s.get("scores") or {}).get(c, {}).get("score") for c in CRITERIA},
        })
    return out


def _medians(items: list[dict]) -> dict:
    res = {}
    for c in CRITERIA:
        values = [i["scores"][c] for i in items if isinstance(i["scores"].get(c), (int, float))]
        res[c] = statistics.median(values) if values else None
    return res


def _type_mix(items: list[dict]) -> dict:
    """Сколько PR каждого типа изменения: {"bugfix": 5, "feature": 2}. Без типов — пустой словарь."""
    mix = {}
    for i in items:
        if i.get("type"):
            mix[i["type"]] = mix.get(i["type"], 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: -kv[1]))


def _pr_url(repo: str, number) -> str:
    return f"https://github.com/{repo}/pull/{number}" if repo else ""


def employee_report(data: dict, employee_id: str, since: str = "", until: str = "") -> dict | None:
    team = _scored_prs(data, since, until)
    # Логины GitHub не различают регистр: /employees/viicos и /employees/Viicos — один человек.
    mine = [p for p in team if str(p["author"]).lower() == str(employee_id).lower()]
    if not mine:
        return None
    employee_id = mine[0]["author"]
    metric = next((m for m in data.get("metrics") or [] if m.get("author") == employee_id), None) or {}
    mine_med, team_med = _medians(mine), _medians(team)
    level = metric.get("level")
    peers = [m for m in data.get("metrics") or [] if level and m.get("level") == level and m.get("multiplier") is not None]
    repo = data.get("repo", "")
    return {
        "employee_id": employee_id,
        "period": {"since": since or None, "until": until or None},
        "pr_count": len(mine),
        "medians": mine_med,
        "team_medians": team_med,
        "vs_team": {c: (None if mine_med[c] is None or team_med[c] is None else round(mine_med[c] - team_med[c], 2))
                    for c in CRITERIA},
        "level": level,
        "multiplier": metric.get("multiplier"),
        "multiplier_period": "all",
        "composite": metric.get("composite"),
        "norm": metric.get("norm"),
        "flags": metric.get("flags", []),
        "change_types": _type_mix(mine),
        "same_level": {
            "level": level,
            "people": len(peers),
            "median_multiplier": statistics.median([m["multiplier"] for m in peers]) if peers else None,
        },
        "prs": [dict(p, url=_pr_url(repo, p["number"]))
                for p in sorted(mine, key=lambda p: p.get("merged_at") or "", reverse=True)],
    }


def team_report(data: dict, since: str = "", until: str = "") -> dict:
    team = _scored_prs(data, since, until)
    metrics = {m.get("author"): m for m in data.get("metrics") or []}
    people = {}
    for p in team:
        people.setdefault(p["author"], []).append(p)
    return {
        "period": {"since": since or None, "until": until or None},
        "pr_count": len(team),
        "people": len(people),
        "team_medians": _medians(team),
        "change_types": _type_mix(team),
        "employees": sorted([{
            "employee_id": author,
            "pr_count": len(items),
            "medians": _medians(items),
            "level": metrics.get(author, {}).get("level"),
            "multiplier": metrics.get(author, {}).get("multiplier"),
            "flags": metrics.get(author, {}).get("flags", []),
            "change_types": _type_mix(items),
        } for author, items in people.items()], key=lambda e: (-e["pr_count"], e["employee_id"])),
    }
