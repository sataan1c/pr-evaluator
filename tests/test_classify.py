"""classify.py: тип изменения каждого PR, и как он доходит до проверки файлов, отчётов и дашборда.

Настоящий claude не запускается: на его место в PATH кладётся заглушка.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, make_pr, make_score, read, run_script, write

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import check_data  # noqa: E402
import make_data  # noqa: E402
import reports  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
text = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "stdin": text}) + "\n")
if os.environ.get("FAKE_MODE") == "bad":
    print(json.dumps({"is_error": False, "structured_output": {"type": "nonsense", "reason": "x"}, "usage": {}})); sys.exit(0)
n = int(re.search(r"#(\d+)", text).group(1))
kind = ["feature", "bugfix", "docs"][n % 3]
print(json.dumps({"is_error": False, "structured_output": {"type": kind, "reason": "потому что"},
                  "usage": {"input_tokens": 1, "output_tokens": 5}, "total_cost_usd": 0.001}))
'''


@unittest.skipIf(os.name == "nt", "заглушка-скрипт без расширения запускается только на Unix")
class Classify(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="classify-test-"))
        (self.dir / "bin").mkdir()
        fake = self.dir / "bin" / "claude"
        fake.write_text(FAKE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "calls.jsonl"
        write(self.dir / "prs.json", [make_pr(n, author="octocat", title=f"Change {n} by octocat") for n in (30, 31, 32)])
        self.env = {"PATH": str(self.dir / "bin") + os.pathsep + os.environ.get("PATH", ""), "FAKE_LOG": str(self.log)}

    def tearDown(self):
        shutil.rmtree(self.dir, True)

    def run_classify(self, *extra, env=None):
        args = ["--provider", "claude-cli", "--prs", self.dir / "prs.json", "--cache", self.dir / "cache", *extra]
        return run_script("classify.py", args, cwd=self.dir, env=dict(self.env, **(env or {})))

    def calls(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_types_are_written_next_to_prs(self):
        code, out = self.run_classify()
        self.assertEqual(code, 0, out)
        data = read(self.dir / "change_types.json")
        self.assertEqual({r["number"]: r["type"] for r in data["prs"]}, {30: "feature", 31: "bugfix", 32: "docs"})
        self.assertEqual(data["model"], "claude-haiku-5-5")
        self.assertTrue(all(r["reason"] for r in data["prs"]))

    def test_author_hidden_tools_off_and_schema_limits_the_types(self):
        self.run_classify()
        call = self.calls()[0]
        self.assertNotIn("octocat", call["stdin"])
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "")
        schema = json.loads(args[args.index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["type"]["enum"],
                         ["feature", "bugfix", "performance", "refactor", "docs", "tests", "build"])

    def test_second_launch_uses_the_cache(self):
        self.run_classify()
        before = len(self.calls())
        code, out = self.run_classify()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), before)

    def test_partial_run_keeps_other_types(self):
        self.run_classify()
        code, out = self.run_classify("--only", "31", "--fresh")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(read(self.dir / "change_types.json")["prs"]), 3)

    def test_unknown_type_is_rejected_not_saved(self):
        code, out = self.run_classify("--limit", "1", env={"FAKE_MODE": "bad"})
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED", out)
        self.assertNotIn("Traceback", out)

    def test_without_model_settings_it_explains(self):
        # сервис, которого нет: так тест не зависит от .env на этом компьютере и не зовёт настоящую модель
        code, out = run_script("classify.py", ["--prs", self.dir / "prs.json", "--provider", "no-such-service"], cwd=self.dir)
        self.assertNotEqual(code, 0)
        self.assertIn("not set up", out)


class TypesDownstream(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="types-test-"))
        self.prs = [make_pr(1, author="ann"), make_pr(2, author="ann"), make_pr(3, author="bob")]
        write(self.dir / "prs.json", self.prs)
        write(self.dir / "scores.json", [make_score(1), make_score(2), make_score(3)])
        self.types = {"model": "m", "prompt_version": "v", "types": ["feature", "bugfix"],
                      "prs": [{"number": 1, "type": "bugfix", "reason": "r"}, {"number": 2, "type": "bugfix", "reason": "r"},
                              {"number": 3, "type": "feature", "reason": "r"}]}
        write(self.dir / "change_types.json", self.types)

    def tearDown(self):
        shutil.rmtree(self.dir, True)

    def test_check_data_accepts_good_file(self):
        rep = check_data.check_folder(self.dir, ROOT / "config" / "levels.json")
        self.assertEqual([e for e in rep.errors if "change_types" in str(e)], [])

    def test_check_data_rejects_unknown_type_and_unknown_pr(self):
        self.types["prs"].append({"number": 99, "type": "magic", "reason": ""})
        write(self.dir / "change_types.json", self.types)
        rep = check_data.check_folder(self.dir, ROOT / "config" / "levels.json")
        text = " ".join(str(e) for e in rep.errors)
        self.assertIn("такого PR нет", text)
        self.assertIn("magic", text)

    def test_dashboard_data_and_reports_carry_types(self):
        data = make_data.collect(self.dir, None)
        self.assertEqual(data["types"]["prs"][2]["type"], "feature")
        report = reports.employee_report(data, "ann")
        self.assertEqual(report["change_types"], {"bugfix": 2})
        self.assertEqual({p["type"] for p in report["prs"]}, {"bugfix"})
        team = reports.team_report(data)
        self.assertEqual(team["change_types"], {"bugfix": 2, "feature": 1})

    def test_without_types_file_nothing_breaks(self):
        (self.dir / "change_types.json").unlink()
        data = make_data.collect(self.dir, None)
        self.assertNotIn("types", data)
        self.assertEqual(reports.employee_report(data, "ann")["change_types"], {})


if __name__ == "__main__":
    unittest.main()
