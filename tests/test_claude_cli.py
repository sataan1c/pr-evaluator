"""score.py с Claude Code CLI вместо ключа API, и REST-отчёты app.py.

Настоящий claude не запускается: на его место в PATH кладётся заглушка, которая
записывает, с какими параметрами её вызвали, и отвечает так, как ответил бы CLI.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.helpers import ROOT, make_pr, make_score, read, run_script, write
sys.path.insert(0, str(ROOT / "devtools"))

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
text = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "stdin": text}) + "\n")
mode = os.environ.get("FAKE_CLAUDE", "ok")
if mode == "auth":
    print(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"})); sys.exit(1)
if mode == "limit":
    print(json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1760000000"})); sys.exit(1)
if mode == "garbage":
    print("this is not json"); sys.exit(1)
n = int(re.search(r"#(\d+)", text).group(1)) if re.search(r"#(\d+)", text) else 0
ans = {"summary": "суть", "scores": {c: {"score": (n + i) % 5 + 1, "reason": "потому что", "evidence": []}
       for i, c in enumerate(["complexity", "quality", "risk", "clarity"])}}
print(json.dumps({"is_error": False, "structured_output": ans, "total_cost_usd": 0.002,
                  "usage": {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 7, "output_tokens": 50}}))
'''


class ClaudeCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cli-test-"))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        fake = self.bin / ("claude.py" if os.name == "nt" else "claude")
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "calls.jsonl"
        write(self.dir / "prs.json", [make_pr(n, author="octocat", title=f"Change {n}") for n in (11, 12, 13)])
        self.env = {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""), "FAKE_LOG": str(self.log)}
        if os.name == "nt":
            self.env["SCORER_CLAUDE_BIN"] = str(fake)

    def tearDown(self):
        shutil.rmtree(self.dir, True)

    def score(self, *extra, env=None):
        args = ["--provider", "claude-cli", "--prs", self.dir / "prs.json", "--out", self.dir / "scores.json",
                "--stats", self.dir / "stats.json", "--cache", self.dir / "cache", "--runs", "1", *extra]
        return run_script("score.py", args, cwd=self.dir, env=dict(self.env, **(env or {})))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    @unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
    def test_scores_without_any_api_key(self):
        code, out = self.score()
        self.assertEqual(code, 0, out)
        self.assertEqual([r["number"] for r in read(self.dir / "scores.json")], [11, 12, 13])
        self.assertIn("none billed", out)
        stats = read(self.dir / "stats.json")
        self.assertEqual(stats["model"], "claude-haiku-5-5")
        self.assertAlmostEqual(stats["api_list_price_usd"], 0.006, places=4)

    @unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
    def test_call_is_safe_and_the_author_is_hidden(self):
        self.score()
        call = self.calls()[0]
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "", "инструменты должны быть выключены")
        self.assertIn("--system-prompt", args)
        self.assertIn("RUBRIC", args[args.index("--system-prompt") + 1])
        schema = json.loads(args[args.index("--json-schema") + 1])
        self.assertEqual(schema["required"], ["summary", "scores"])
        self.assertNotIn("octocat", call["stdin"])

    @unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
    def test_second_launch_takes_everything_from_the_cache(self):
        self.score()
        before = len(self.calls())
        code, out = self.score()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), before)

    @unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
    def test_not_logged_in_stops_with_a_clear_message(self):
        code, out = self.score(env={"FAKE_CLAUDE": "auth"})
        self.assertNotEqual(code, 0)
        self.assertIn("not logged in", out)
        self.assertNotIn("Traceback", out)
        self.assertFalse((self.dir / "scores.json").exists())

    @unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
    def test_used_up_limit_stops_and_keeps_the_cache(self):
        self.score("--limit", "1")
        code, out = self.score(env={"FAKE_CLAUDE": "limit"})
        self.assertNotEqual(code, 0)
        self.assertIn("subscription limit", out)
        self.assertEqual(len(list((self.dir / "cache").glob("*.json"))), 1)

    @unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
    def test_broken_cli_output_fails_the_pr_not_the_program(self):
        code, out = self.score("--http-retries", "1", env={"FAKE_CLAUDE": "garbage"})
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED", out)
        self.assertNotIn("Traceback", out)

    def test_missing_cli_is_explained(self):
        code, out = self.score(env={"PATH": str(self.dir / "nothing-here"), "SCORER_CLAUDE_BIN": ""})
        self.assertNotEqual(code, 0)
        self.assertIn("Claude Code CLI is not installed", out)
        self.assertNotIn("Traceback", out)

    def test_run_all_treats_the_subscription_as_a_key(self):
        sys.path.insert(0, str(ROOT))
        import run_all
        old = os.environ.get("SCORER_PROVIDER")
        os.environ["SCORER_PROVIDER"] = "claude-cli"
        try:
            self.assertTrue(run_all.model_key_is_set(self.dir))
        finally:
            if old is None:
                del os.environ["SCORER_PROVIDER"]
            else:
                os.environ["SCORER_PROVIDER"] = old


class Reports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="reports-test-"))
        prs = [make_pr(1, author="ann", merged_at="2026-05-01T10:00:00Z"),
               make_pr(2, author="ann", merged_at="2026-05-10T10:00:00Z"),
               make_pr(3, author="ann", merged_at="2026-06-01T10:00:00Z"),
               make_pr(4, author="bob", merged_at="2026-05-05T10:00:00Z")]
        write(cls.dir / "prs.json", prs)
        write(cls.dir / "scores.json", [make_score(1, complexity=4), make_score(2, complexity=2),
                                       make_score(3, complexity=5), make_score(4, complexity=1)])
        write(cls.dir / "metrics.json", [
            {"author": "ann", "level": "middle", "pr_count": 3, "medians": {}, "composite": 3.4, "norm": 3.2,
             "multiplier": 1.03, "flags": []},
            {"author": "bob", "level": "junior", "pr_count": 1, "medians": {}, "composite": 2.0, "norm": 2.6,
             "multiplier": None, "flags": ["insufficient_data"]}])
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "serve", "--dir", str(cls.dir), "--port", "0", "--no-browser"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUTF8="1"))
        first = cls.proc.stdout.readline().decode("utf-8", "replace")
        cls.url = re.search(r"http://127\.0\.0\.1:\d+", first).group(0)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        shutil.rmtree(cls.dir, True)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_employee_report(self):
        status, body = self.get("/api/reports/employees/ann")
        self.assertEqual(status, 200)
        self.assertEqual(body["pr_count"], 3)
        self.assertEqual(body["medians"]["complexity"], 4)
        self.assertEqual(body["team_medians"]["complexity"], 3)
        self.assertEqual(body["vs_team"]["complexity"], 1)
        self.assertEqual(body["multiplier"], 1.03)
        self.assertEqual([p["number"] for p in body["prs"]], [3, 2, 1])

    def test_period_filter(self):
        status, body = self.get("/api/reports/employees/ann?since=2026-05-01&until=2026-05-31")
        self.assertEqual(status, 200)
        self.assertEqual(body["pr_count"], 2)
        self.assertEqual(body["multiplier_period"], "all")

    def test_team_report(self):
        status, body = self.get("/api/reports/team")
        self.assertEqual(status, 200)
        self.assertEqual(body["pr_count"], 4)
        self.assertEqual([e["employee_id"] for e in body["employees"]], ["ann", "bob"])
        self.assertIsNone(body["employees"][1]["multiplier"])

    def test_unknown_employee_is_404(self):
        status, body = self.get("/api/reports/employees/nobody")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_login_is_case_insensitive_like_on_github(self):
        status, body = self.get("/api/reports/employees/ANN")
        self.assertEqual(status, 200)
        self.assertEqual(body["employee_id"], "ann")
        self.assertEqual(body["multiplier"], 1.03)

    def test_bad_period_is_400_not_an_empty_report(self):
        for query in ("since=2026-5-1", "until=yesterday", "since=2026-06-01&until=2026-05-01"):
            status, body = self.get("/api/reports/team?" + query)
            self.assertEqual(status, 400, query)
            self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()


try:
    import requests  # noqa: F401
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False


@unittest.skipUnless(HAVE_REQUESTS, "нужна библиотека requests: pip install requests")
class UpdateMode(unittest.TestCase):
    """run_all.py --update: как cron-задача из схемы — новый список PR, оцениваются только новые."""

    def test_second_update_scores_only_new_prs(self):
        import demo_repo
        import fake_github
        import fake_llm
        folder = Path(tempfile.mkdtemp(prefix="update-test-"))
        github, github_url = fake_github.start()
        llm, llm_url, _ = fake_llm.start()
        env = {"GITHUB_API_URL": github_url, "GITHUB_TOKEN": "t", "FETCH_CACHE_DIR": str(folder / "raw"),
               "FETCH_SEARCH_PAUSE": "0", "SCORER_BASE_URL": llm_url, "SCORER_MODEL": "fake-llm", "SCORER_API_KEY": "local"}
        common = ["--no-app", "--workdir", folder, "--repo", demo_repo.REPO, "--min-age-days", "7",
                  "--runs", "1", "--no-style", "--update"]
        try:
            code, out = run_script("run_all.py", common + ["--limit", "20"], env=env, timeout=300)
            self.assertEqual(code, 0, out[-2000:])
            self.assertEqual(len(read(folder / "scores.json")), 20)
            code, out = run_script("run_all.py", common + ["--limit", "25"], env=env, timeout=300)
            self.assertEqual(code, 0, out[-2000:])
            self.assertEqual(len(read(folder / "scores.json")), 25)
            self.assertRegex(out, r"Requests in this launch: 5;")      # только 5 новых PR, 20 из кэша
            self.assertTrue((folder / "metrics.json").is_file())
        finally:
            github.stop()
            llm.stop()
            shutil.rmtree(folder, True)
