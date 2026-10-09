"""Общее для тестов: образцы записей и запуск скриптов в отдельной папке."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devtools"))

try:
    import requests  # noqa: F401
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

PATCH = "@@ -10,4 +10,6 @@ def total():\n context\n-old = 1\n+new = 2\n+extra = 3\n+more = 4\n context"


def make_pr(number=1, author="octocat", title="Fix cache invalidation", body="", merged_at="2026-05-01T10:00:00Z",
            additions=3, deletions=1, files=None, reviews=None, ci="success"):
    return {
        "number": number, "title": title,
        "body": body or "Invalidate the cache when the price changes, because stale prices reached checkout. "
                        "Tested with the pricing suite and manually on staging.",
        "author": author, "merged_at": merged_at, "additions": additions, "deletions": deletions,
        "files": files if files is not None else [{"path": "src/cache.py", "patch": PATCH, "truncated": False}],
        "reviews": reviews if reviews is not None else [{"state": "APPROVED", "body": ""}],
        "ci": ci, "noise_removed": [],
    }


def make_score(number=1, complexity=3, quality=3, risk=3, clarity=3, unstable=False, runs=3, evidence=None):
    values = {"complexity": complexity, "quality": quality, "risk": risk, "clarity": clarity}
    return {"number": number, "summary": "what it does",
            "scores": {c: {"score": v, "reason": "because", "evidence": list(evidence or [])} for c, v in values.items()},
            "runs": runs, "unstable": unstable}


def run_script(script, args=(), cwd=None, env=None, timeout=120):
    """Запускает скрипт проекта отдельным процессом. Возвращает (код выхода, весь вывод)."""
    full_env = dict(os.environ, PYTHONUTF8="1")
    for key in list(full_env):   # настройки с этого компьютера не должны попасть в тест
        if key.startswith(("SCORER_", "GITHUB_", "FETCH_")) or key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            del full_env[key]
    full_env.update(env or {})
    done = subprocess.run([sys.executable, str(ROOT / script)] + [str(a) for a in args], cwd=str(cwd) if cwd else None,
                          env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
