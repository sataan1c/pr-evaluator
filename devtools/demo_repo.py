"""Синтетический репозиторий для репетиции и тестов. К настоящим данным отношения не имеет.

Генератор детерминирован (зерно фиксировано) и намеренно содержит все трудные случаи,
на которых проверяется конвейер: откаты, откат отката, закрытый без мержа откат,
исправления с причинной ссылкой и без неё, служебные PR и PR ботов, lock-файлы и
картинки, слишком большой diff, переименование без изменений, комментарии после мержа,
логин автора внутри кода и внутри пути, давно принятый PR с недавним комментарием.

ВАЖНО: вероятность отката здесь задана самим генератором и зависит от типа PR.
Связь «риск — откат» в этих данных заложена нами и ничего не доказывает.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

REPO = "demo/shop"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

# логин -> сколько PR было принято до начала периода (отсюда уровень)
AUTHORS = {"alina": 3, "bahruz": 6, "chingiz": 22, "dilara": 35, "elvin": 48, "farid": 120, "gunel": 210, "hasan": 0}
SENIORS = ("farid", "gunel")

KINDS = {
    #  тип         (вероятность проблемы, файлы, размер правки в строках)
    "docs":        (0.02, ["docs/{name}.md"], (4, 18)),
    "config":      (0.04, ["config/{name}.yaml"], (2, 10)),
    "refactor":    (0.08, ["src/{name}.py", "tests/test_{name}.py"], (20, 70)),
    "feature":     (0.12, ["src/{name}.py", "src/{name}_service.py", "tests/test_{name}.py"], (40, 120)),
    "migration":   (0.40, ["migrations/00{i}_{name}.sql", "src/models/{name}.py"], (30, 90)),
    "concurrency": (0.38, ["src/worker/{name}_queue.py", "src/worker/locks.py"], (35, 110)),
    "auth":        (0.34, ["src/auth/{name}.py", "src/auth/session.py", "tests/test_auth_{name}.py"], (30, 100)),
}
NAMES = ["cart", "checkout", "catalog", "pricing", "orders", "invoice", "search", "profile", "shipping", "coupons", "stock", "refunds"]
SNIPPETS = {
    "docs": ["Describe how the {name} page is configured.", "Add an example request for {name}.", "Fix a typo in the {name} section."],
    "config": ["timeout_seconds: {n}", "retries: {n}", "feature_{name}_enabled: true"],
    "refactor": ["def load_{name}(repo, key):", "    return repo.get(key)", "    items = [normalize(x) for x in items]", "    assert result is not None"],
    "feature": ["def create_{name}(payload):", "    validate(payload)", "    record = {name}_repo.save(payload)", "    events.publish('{name}.created', record.id)", "    return record"],
    "migration": ["ALTER TABLE {name} ADD COLUMN archived_at TIMESTAMP NULL;", "UPDATE {name} SET archived_at = NOW() WHERE status = 'closed';",
                  "DROP INDEX idx_{name}_status;", "DELETE FROM {name}_tmp WHERE created_at < NOW() - INTERVAL '30 days';"],
    "concurrency": ["with self._lock:", "    self._pending.append(job)", "thread = threading.Thread(target=self._drain, daemon=True)",
                    "    if not self._lock.acquire(timeout={n}):", "        raise TimeoutError('queue is busy')"],
    "auth": ["def refresh_session(token):", "    claims = jwt.decode(token, key, algorithms=['HS256'])", "    if claims['exp'] < now():",
             "        raise PermissionError('session expired')", "    return issue_token(claims['sub'])"],
}
GOOD_BODY = ("## What\n{what}\n\n## Why\nSupport asked for this after several customer reports; the old behaviour made {name} "
             "hard to reason about.\n\n## How to test\nRun the {name} test suite, then try the flow manually on staging with an empty cart.")
POOR_BODY = ["fix", "update", "wip, see ticket", ""]


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_patch(rng, kind, name, lines, start=None):
    """Один hunk в формате GitHub: заголовок @@ и строки с +, - и пробелом."""
    start = start or rng.randint(5, 180)
    pool = SNIPPETS[kind]
    added = [("+" + rng.choice(pool).format(name=name, n=rng.randint(2, 60))) for _ in range(lines)]
    removed = ["-" + rng.choice(pool).format(name=name, n=rng.randint(2, 60)) for _ in range(max(1, lines // 4))]
    body = [" # context before"] + removed + added + [" # context after"]
    return f"@@ -{start},{len(removed) + 2} +{start},{len(added) + 2} @@ def handler():\n" + "\n".join(body), len(added), len(removed)


def generate(seed: int = 7) -> dict:
    rng = random.Random(seed)
    pulls, files, reviews, comments, details, checks = [], {}, {}, {}, {}, {}
    number = [100]

    def add_pr(title, body, author, merged, kind=None, name="cart", created_before_h=30, is_bot=False, merged_ok=True,
               ci="success", updated=None, extra_files=(), size=None):
        number[0] += 1
        n = number[0]
        closed = merged
        pr = {
            "number": n, "title": title, "body": body,
            "user": {"login": author, "type": "Bot" if is_bot else "User"},
            "created_at": stamp(merged - timedelta(hours=created_before_h)),
            "merged_at": stamp(merged) if merged_ok else None,
            "closed_at": stamp(closed),
            "updated_at": stamp(updated or merged),
            "head": {"sha": f"{n:040x}"},
        }
        pulls.append(pr)
        file_list, add_total, del_total = [], 0, 0
        if kind:
            _, templates, (low, high) = KINDS[kind]
            for i, template in enumerate(templates):
                lines = size or rng.randint(low, high) // len(templates) + 1
                patch, a, d = make_patch(rng, "refactor" if "test" in template else kind, name, lines)
                file_list.append({"filename": template.format(name=name, i=rng.randint(10, 99)), "status": "modified",
                                  "changes": a + d, "patch": patch})
                add_total, del_total = add_total + a, del_total + d
        file_list.extend(extra_files)
        files[n] = file_list
        details[n] = {"additions": add_total + sum(f.get("changes", 0) for f in extra_files), "deletions": del_total}
        checks[n] = ci
        reviews[n], comments[n] = [], []
        return pr

    def review(n, state, body, when, login="reviewer-one", bot=False):
        reviews[n].append({"state": state, "body": body, "submitted_at": stamp(when),
                           "user": {"login": login, "type": "Bot" if bot else "User"}})

    # ---- обычные PR за 110 дней ----
    plan = []
    for author in AUTHORS:
        count = {"hasan": 1, "bahruz": 10, "alina": 6}.get(author, rng.randint(9, 13))
        for _ in range(count):
            if author == "bahruz":
                kind = rng.choice(["docs", "config", "docs", "config", "refactor"])   # много мелких PR
            elif author in SENIORS:
                kind = rng.choice(["feature", "migration", "concurrency", "auth", "refactor", "feature"])
            else:
                kind = rng.choice(["docs", "config", "refactor", "refactor", "feature", "feature", "migration", "auth", "concurrency"])
            plan.append((author, kind))
    rng.shuffle(plan)

    later = []  # (когда, что создать): откаты и исправления добавляются в хронологическом порядке
    for i, (author, kind) in enumerate(plan):
        merged = NOW - timedelta(days=110 * (1 - i / len(plan)) + rng.uniform(0, 0.8), hours=rng.randint(0, 9))
        name = rng.choice(NAMES)
        what = {"docs": f"Document the {name} settings", "config": f"Tune {name} timeouts", "refactor": f"Simplify {name} loading",
                "feature": f"Add bulk actions to {name}", "migration": f"Archive closed {name} rows",
                "concurrency": f"Make the {name} queue thread-safe", "auth": f"Rotate session tokens for {name}"}[kind]
        careful = rng.random() < (0.8 if author in SENIORS else 0.55)
        body = GOOD_BODY.format(what=what + ".", name=name) if careful else rng.choice(POOR_BODY)
        ci = rng.choices(["success", "failure", "mixed", "unknown"], [0.82, 0.06, 0.06, 0.06])[0]
        pr = add_pr(what, body, author, merged, kind, name, ci=ci, size=rng.randint(3, 16) if author == "bahruz" else None)
        n = pr["number"]
        review(n, "APPROVED", "", merged - timedelta(hours=2))
        if rng.random() < 0.35:
            review(n, "CHANGES_REQUESTED", f"Please add a test for the empty case, @{author}.", merged - timedelta(hours=20))
            comments[n].append({"path": files[n][0]["filename"], "line": 12, "body": "This branch is never covered.",
                                "created_at": stamp(merged - timedelta(hours=19)), "user": {"login": "reviewer-two", "type": "User"}})
        review(n, "COMMENTED", "Automated coverage report: 81%", merged - timedelta(hours=3), "codecov[bot]", bot=True)

        if rng.random() < KINDS[kind][0]:
            delay = timedelta(days=rng.uniform(0.5, 9))
            fixer = rng.choice([a for a in AUTHORS if a != author])
            if rng.random() < 0.4:
                later.append((merged + delay, "revert", pr, fixer))
            else:
                later.append((merged + delay, "fix", pr, fixer))
            # комментарий ПОСЛЕ мержа выдаёт исход: в prs.json его быть не должно
            review(n, "COMMENTED", "LEAK-AFTER-MERGE: this broke production, we are rolling it back.", merged + delay / 2)
            comments[n].append({"path": files[n][0]["filename"], "line": 8, "body": "LEAK-AFTER-MERGE: here is the bug.",
                                "created_at": stamp(merged + delay / 2), "user": {"login": "reviewer-two", "type": "User"}})

    for when, what, target, fixer in sorted(later, key=lambda item: item[0]):
        if when > NOW:
            continue
        t, title = target["number"], target["title"]
        if what == "revert":
            style = rng.randint(0, 2)
            if style == 0:
                add_pr(f'Revert "{title}"', f"Reverts {REPO}#{t}\n\nBroke checkout on staging.", fixer, when, "refactor", size=6)
            elif style == 1:
                add_pr(f'Revert "{title} (#{t})"', "This reverts the change, see incident notes.", fixer, when, "refactor", size=6)
            else:
                add_pr(f'Revert "{title}"', "", fixer, when, "refactor", size=6)   # только цитата заголовка
        elif rng.random() < 0.6:
            add_pr(f"Fix crash after {title.lower()}", f"Regression introduced in #{t}: the empty case was not handled.", fixer, when, "refactor", size=8)
        else:
            add_pr(f"Hotfix {title.lower()}", f"Small correction on top of the previous change, see #{t}.", fixer, when, "refactor", size=5)

    # ---- особые случаи ----
    base = [p for p in pulls if p["merged_at"] and not p["title"].startswith(("Revert", "Fix crash", "Hotfix"))]
    old, mid = base[3], base[len(base) // 2]

    # 1. Откат, закрытый без мержа: ничего не откатил.
    add_pr(f'Revert "{mid["title"]}"', f"Reverts {REPO}#{mid['number']}", "elvin", NOW - timedelta(days=40), "refactor", merged_ok=False, size=4)
    # 2. Исправление, которое лишь упоминает другой PR без причинной связи и позже окна: не считается.
    add_pr("Fix flaky search test", f"Unrelated to #{old['number']}, same file though.", "dilara", NOW - timedelta(days=20), "refactor", size=5)
    # 3. Служебные PR и бот: не оцениваются.
    add_pr("Release v2.4.0", "Changelog inside.", "gunel", NOW - timedelta(days=33), "config", size=3)
    add_pr("Bump requests from 2.31 to 2.32", "Dependency update.", "dependabot[bot]", NOW - timedelta(days=31), "config", is_bot=True, size=2)
    # 4. Шум, бинарные файлы, слишком большой diff, чистое переименование, длинный patch.
    big_patch, a, d = make_patch(rng, "feature", "catalog", 400)
    add_pr("Import the new catalog format", GOOD_BODY.format(what="Import the new catalog format.", name="catalog"), "farid",
           NOW - timedelta(days=45), "feature", "catalog", extra_files=[
               {"filename": "package-lock.json", "status": "modified", "changes": 5200, "patch": "@@ -1,2 +1,2 @@\n-a\n+b"},
               {"filename": "assets/logo.png", "status": "added", "changes": 0},
               {"filename": "src/catalog_data.py", "status": "modified", "changes": 9000},          # GitHub не прислал patch
               {"filename": "src/catalog_old.py", "status": "renamed", "changes": 0},
               {"filename": "src/catalog_import.py", "status": "added", "changes": a + d, "patch": big_patch},
           ])
    # 5. Логин автора в коде и в пути к файлу.
    todo_patch = "@@ -40,3 +40,5 @@ def total():\n context\n+# TODO(alina): remove after the sale\n+discount = 0.1\n context"
    add_pr("Add a sale discount", "Temporary discount for the autumn sale, by @alina.\n\nSigned-off-by: Alina A <alina@example.com>",
           "alina", NOW - timedelta(days=50), "feature", "pricing", extra_files=[
               {"filename": "src/pricing_sale.py", "status": "modified", "changes": 2, "patch": todo_patch},
               {"filename": "docs/authors/alina.md", "status": "added", "changes": 1, "patch": "@@ -0,0 +1 @@\n+Maintainer notes"},
           ])
    # 6. Давно принятый PR с недавним комментарием: стоит в начале списка «по обновлению», но в выборку попасть не должен.
    add_pr("Initial payment gateway", "Old change.", "gunel", NOW - timedelta(days=400), "feature", "orders", updated=NOW - timedelta(days=1))
    # 7. Откат отката: исходный PR остаётся откаченным, сам откат тоже.
    reverts = [p for p in pulls if p["merged_at"] and p["title"].startswith("Revert ")]
    if reverts:
        first = reverts[0]
        when = datetime.strptime(first["merged_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(days=2)
        add_pr(f'Revert "{first["title"]}"', f"Reverts {REPO}#{first['number']}\n\nThe original change was fine after all.",
               "gunel", min(when, NOW - timedelta(days=1)), "refactor", size=6)
    # 8. Очень старые PR, чтобы список закрытых занимал больше одной страницы.
    for k in range(45):
        add_pr(f"Old maintenance change {k}", "Housekeeping.", rng.choice(list(AUTHORS)), NOW - timedelta(days=200 + k * 3), "config", size=3)

    return {"repo": REPO, "now": stamp(NOW), "pulls": pulls, "files": files, "reviews": reviews, "comments": comments,
            "details": details, "checks": checks, "merged_before": dict(AUTHORS)}
