"""validate.py, style_test.py и check_data.py."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, make_pr, make_score, read, run_script, write

import check_data
import metrics
import style_test
import validate
from common import DataError


def outcome(number, problem=False, observable=True, strict=None):
    return {"number": number, "merged_at": "2026-05-01T10:00:00Z", "reverted": problem, "reverted_by": [900] if problem else [],
            "fixed_by": [], "fixed_by_explicit": [], "problem": problem,
            "problem_strict": problem if strict is None else strict, "observed_days": 30.0, "observable": observable}


class RiskAgainstOutcomes(unittest.TestCase):
    def build(self, rows):
        """rows: (балл риска, проблемный?) -> оценки, PR и исходы."""
        scores = [make_score(i, risk=risk) for i, (risk, _) in enumerate(rows, 1)]
        prs = [make_pr(i) for i in range(1, len(rows) + 1)]
        return scores, prs, [outcome(i, problem) for i, (_, problem) in enumerate(rows, 1)]

    def test_clear_relationship_is_reported_as_supported(self):
        rows = [(5, True)] * 6 + [(4, True)] * 2 + [(4, False)] * 4 + [(3, False)] * 8 + [(2, False)] * 10 + [(1, False)] * 10
        r = validate.risk_validation(*self.build(rows))
        self.assertEqual(r["verdict"], "supported")
        self.assertEqual([(g["risk"], g["n"], g["problems"]) for g in r["groups"]], [("1–2", 20, 0), ("3", 8, 0), ("4–5", 12, 8)])
        self.assertGreater(r["auc_risk"]["value"], 0.9)
        self.assertLess(r["high_vs_low_p"], 0.001)
        self.assertEqual(r["problems"], 8)

    def test_no_relationship_is_not_called_a_confirmation(self):
        rows = [(risk, i % 5 == 0) for i, risk in enumerate([1, 2, 3, 4, 5] * 10)]   # проблемы поровну на всех баллах
        r = validate.risk_validation(*self.build(rows))
        self.assertEqual(r["verdict"], "not_shown")
        self.assertLessEqual(r["auc_risk"]["ci95"][0], 0.5)

    def test_handful_of_problems_gives_no_verdict_at_all(self):
        rows = [(5, True)] * 3 + [(1, False)] * 30
        self.assertEqual(validate.risk_validation(*self.build(rows))["verdict"], "too_few_problems")

    def test_prs_whose_window_has_not_passed_are_left_out(self):
        scores, prs, outs = self.build([(5, True)] * 6 + [(1, False)] * 10)
        for o in outs[:3]:
            o["observable"] = False
        r = validate.risk_validation(scores, prs, outs)
        self.assertEqual((r["prs_used"], r["excluded_window_not_passed"], r["problems"]), (13, 3, 3))

    def test_strict_rule_uses_its_own_flag(self):
        scores, prs, outs = self.build([(5, True)] * 6 + [(1, False)] * 10)
        outs[0]["problem_strict"] = False
        self.assertEqual(validate.risk_validation(scores, prs, outs, "problem_strict")["problems"], 5)


class HumanAgainstModel(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="validate-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def csv(self, text):
        path = self.dir / "human.csv"
        path.write_text(text, encoding="utf-8")
        return validate.read_human(path)

    def test_semicolon_and_comma_files_read_the_same(self):
        a = self.csv("number;title;complexity;quality;risk;clarity\n1;Fix;3;4;;2\n#2;Add;5;5;5;5\n")
        b = self.csv("﻿number,complexity,quality,risk,clarity\n1,3,4,,2\n2,5,5,5,5\n")
        self.assertEqual(a, b)
        self.assertEqual(a[0], {"number": 1, "complexity": 3, "quality": 4, "clarity": 2})   # пустая ячейка = не оценивал

    def test_bad_cells_are_explained_with_the_line_number(self):
        with self.assertRaises(DataError) as ctx:
            self.csv("number,risk\n1,3\n2,9\n")
        self.assertIn("строка 3", str(ctx.exception))
        with self.assertRaises(DataError):
            self.csv("pr,risk\n1,3\n")

    def test_agreement_numbers(self):
        scores = [make_score(1, complexity=3, quality=3, risk=3, clarity=3), make_score(2, complexity=5, quality=5, risk=5, clarity=5)]
        human = [{"number": 1, "complexity": 3, "quality": 4, "risk": 5, "clarity": 3},      # 0, -1, -2, 0
                 {"number": 2, "complexity": 5, "risk": 5}, {"number": 77, "risk": 1}]       # 0, 0; PR 77 не оценён моделью
        r = validate.human_agreement(scores, human)
        self.assertEqual((r["prs"], r["ratings"], r["not_in_scores"]), (2, 6, [77]))
        self.assertEqual(r["within_1"], round(5 / 6, 3))
        self.assertEqual(r["exact"], round(4 / 6, 3))
        self.assertEqual(r["criteria"]["risk"], {"n": 2, "exact": 0.5, "within_1": 0.5, "mean_model_minus_human": -1.0})

    def test_template_is_blind_and_spread_over_the_list(self):
        prs = [make_pr(i, title=f"Change; number {i}") for i in range(1, 41)]
        text = validate.human_template(prs, 10, "acme/shop")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "number;title;url;complexity;quality;risk;clarity")
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[1], "1;Change, number 1;https://github.com/acme/shop/pull/1;;;;")
        self.assertTrue(lines[-1].startswith("37;"))


class StyleTest(unittest.TestCase):
    def test_degrade_is_deterministic_short_and_keeps_no_markup(self):
        body = "## What\nInvalidate the cache when the price changes. Second sentence here.\n```py\ncode()\n```\nSee https://example.com"
        first = style_test.degrade("Fix cache invalidation for the pricing page", body)
        self.assertEqual(first, style_test.degrade("Fix cache invalidation for the pricing page", body))
        title, text = first
        self.assertEqual(len(title.split()), 4)
        self.assertLessEqual(len(text.split()), 10)
        self.assertEqual(text, text.lower())
        for junk in ("#", "`", "http", "."):
            self.assertNotIn(junk, text)
        self.assertNotEqual(text, "invalidate the cache when the price changes")   # опечатки внесены

    def test_prepare_changes_only_title_and_body(self):
        prs = [make_pr(i) for i in range(1, 11)] + [make_pr(99, body="x")]
        prs[-1]["body"] = "fix"                                    # слишком короткое описание: портить нечего
        out = style_test.prepare(prs, 5)
        self.assertEqual(len(out), 5)
        self.assertNotIn(99, [p["number"] for p in out])
        original = {p["number"]: p for p in prs}
        for p in out:
            src = original[p["number"]]
            self.assertTrue(p["style_degraded"])
            self.assertNotEqual((p["title"], p["body"]), (src["title"], src["body"]))
            for key in ("files", "reviews", "ci", "author", "additions", "deletions", "merged_at"):
                self.assertEqual(p[key], src[key])

    def test_compare(self):
        base = [make_score(1, complexity=3, quality=3, risk=3, clarity=5), make_score(2, complexity=3, quality=3, risk=3, clarity=4)]
        style = [make_score(1, complexity=3, quality=3, risk=3, clarity=2), make_score(2, complexity=3, quality=3, risk=4, clarity=2)]
        r = style_test.compare(base, style)
        self.assertEqual((r["n"], r["clarity_dropped"], r["others_stable"]), (2, True, False))
        self.assertEqual(r["criteria"]["clarity"]["mean_shift"], -2.5)
        self.assertEqual(r["criteria"]["risk"], {"mean_shift": 0.5, "mean_abs_shift": 0.5, "unchanged_share": 0.5, "moved_2_or_more": 0})
        self.assertEqual(r["worst_other"], "risk")
        with self.assertRaises(DataError):
            style_test.compare(base, [make_score(50)])


class CheckData(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="check-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.prs = [make_pr(i, author="a" if i < 4 else "b") for i in range(1, 7)]
        self.scores = [make_score(i, evidence=[{"path": "src/cache.py", "lines": "10-12"}]) for i in range(1, 7)]
        self.history = {"merged_before": {"a": 20, "b": 3}}
        cfg = metrics.load_config(ROOT / "config" / "levels.json")
        self.metrics, _ = metrics.compute(self.prs, self.scores, self.history, cfg)
        self.outcomes = [{"number": i, "merged_at": "2026-05-01T10:00:00Z", "reverted": False, "reverted_by": [], "fixed_by": [],
                          "fixed_by_explicit": [], "problem": False, "problem_strict": False, "observed_days": 30.0,
                          "observable": True} for i in range(1, 7)]

    def check(self, **changed):
        files = {"prs": self.prs, "scores": self.scores, "metrics": self.metrics, "outcomes": self.outcomes,
                 "author_history": self.history}
        files.update(changed)
        for name, data in files.items():
            if data is not None:
                write(self.dir / f"{name}.json", data)
        return check_data.check_folder(self.dir, ROOT / "config" / "levels.json")

    def test_consistent_files_pass(self):
        rep = self.check()
        self.assertEqual((rep.errors, rep.warnings), ([], []))

    def assertError(self, rep, fragment):
        self.assertTrue(any(fragment in e for e in rep.errors), f"нет ошибки «{fragment}» среди: {rep.errors}")

    def test_prs_problems(self):
        prs = copy.deepcopy(self.prs)
        prs[0]["ci"] = "passed"
        prs[1]["merged_at"] = "yesterday"
        prs[2]["files"][0].pop("truncated")
        prs[3]["number"] = prs[4]["number"]
        rep = self.check(prs=prs, metrics=None, scores=None, outcomes=None)
        for fragment in ("ci должен быть", "merged_at должен быть датой", "truncated", "номер встречается дважды"):
            self.assertError(rep, fragment)

    def test_scores_problems(self):
        scores = copy.deepcopy(self.scores)
        scores[0]["scores"]["risk"]["score"] = 6
        scores[1]["scores"]["quality"]["reason"] = ""
        scores[2]["scores"]["complexity"]["evidence"] = [{"path": "src/ghost.py", "lines": "1-2"}]
        del scores[3]["scores"]["clarity"]
        scores[4]["number"] = 404
        rep = self.check(scores=scores, metrics=None)
        for fragment in ("risk.score должен быть", "quality.reason пуст", "src/ghost.py", "ровно критерии", "такого PR нет в prs.json"):
            self.assertError(rep, fragment)

    def test_unscored_prs_are_a_warning(self):
        rep = self.check(scores=self.scores[:4], metrics=None)
        self.assertEqual(rep.errors, [])
        self.assertTrue(any("не оценено 2 PR" in w for w in rep.warnings), rep.warnings)

    def test_stale_metrics_are_caught(self):
        scores = copy.deepcopy(self.scores)
        for s in scores[:3]:
            s["scores"]["quality"]["score"] = 5              # оценки пересчитали, а metrics.json остался старым
        self.assertError(self.check(scores=scores), "файл устарел")

    def test_metrics_problems(self):
        bad = copy.deepcopy(self.metrics)
        bad[0]["multiplier"] = 1.4
        bad[1]["level"] = "principal"
        rep = self.check(metrics=bad)
        self.assertError(rep, "multiplier должен лежать между 0.9 и 1.15")
        self.assertError(rep, "level должен быть одним из")

    def test_outcomes_problems(self):
        bad = copy.deepcopy(self.outcomes)
        bad[0]["reverted_by"] = [50]                          # список есть, а флаг не выставлен
        bad[1]["fixed_by_explicit"] = [60]
        self.assertError(self.check(outcomes=bad), "reverted не согласован")
        self.assertError(self.check(outcomes=bad), "fixed_by_explicit должен быть частью fixed_by")

    def test_command_line_exit_codes(self):
        self.check()
        code, out = run_script("check_data.py", ["--dir", self.dir])
        self.assertEqual(code, 0, out)
        self.assertIn("Проверка пройдена", out)
        bad = copy.deepcopy(self.scores)
        bad[0]["scores"]["risk"]["score"] = 0
        write(self.dir / "scores.json", bad)
        code, out = run_script("check_data.py", ["--dir", self.dir])
        self.assertEqual(code, 1, out)
        self.assertEqual(run_script("check_data.py", ["--dir", self.dir, "--only-prs"])[0], 0)
        (self.dir / "prs.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(run_script("check_data.py", ["--dir", self.dir])[0], 1)


class ValidateCommand(unittest.TestCase):
    def test_report_without_optional_inputs_says_what_is_missing(self):
        work = Path(tempfile.mkdtemp(prefix="validate-cli-"))
        self.addCleanup(shutil.rmtree, work, True)
        write(work / "prs.json", [make_pr(1), make_pr(2)])
        write(work / "scores.json", [make_score(1, runs=1), make_score(2, runs=1)])
        code, out = run_script("validate.py", cwd=work)
        self.assertEqual(code, 0, out)
        report = read(work / "validation.json")
        self.assertEqual((report["risk"], report["human"], report["style"], report["synthetic"]), (None, None, None, False))
        text = (work / "validation.md").read_text(encoding="utf-8")
        for fragment in ("нет outcomes.json", "нет human_scores.csv", "нет scores_style.json", "стабильность не проверялась"):
            self.assertIn(fragment, text)
        self.assertNotIn("заглушке", text)

    def test_missing_scores_is_a_clear_error(self):
        work = Path(tempfile.mkdtemp(prefix="validate-cli-"))
        self.addCleanup(shutil.rmtree, work, True)
        write(work / "prs.json", [make_pr(1)])
        code, out = run_script("validate.py", cwd=work)
        self.assertEqual(code, 2, out)
        self.assertIn("не найден файл оценок", out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
