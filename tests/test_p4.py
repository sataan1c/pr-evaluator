"""Инструменты участника 4: пробные оценки, график и запуск всех шагов одной командой."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_pr, make_score, read, run_script, write

import check_data
import mock_scores
import validate
from common import DataError

try:
    import matplotlib  # noqa: F401
    HAVE_MATPLOTLIB = True
except ImportError:
    HAVE_MATPLOTLIB = False


def outcome(number, problem=False):
    return {"number": number, "merged_at": "2026-05-01T10:00:00Z", "reverted": problem, "reverted_by": [900] if problem else [],
            "fixed_by": [], "fixed_by_explicit": [], "problem": problem, "problem_strict": problem,
            "observed_days": 30.0, "observable": True}


def folder(test, with_history=True):
    work = Path(tempfile.mkdtemp(prefix="p4-test-"))
    test.addCleanup(shutil.rmtree, work, True)
    prs = [make_pr(i, author=f"dev{i % 3}", merged_at=f"2026-05-{i:02d}T10:00:00Z") for i in range(1, 13)]
    write(work / "prs.json", prs)
    if with_history:
        later = [{"number": 50, "title": 'Revert "Fix cache invalidation"', "body": "Reverts #12", "author": "x",
                  "merged_at": "2026-05-14T10:00:00Z", "closed_at": "2026-05-14T10:00:00Z", "updated_at": "2026-05-14T10:00:00Z"},
                 {"number": 60, "title": "Much later change", "body": "", "author": "x", "merged_at": "2026-07-01T10:00:00Z",
                  "closed_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:00:00Z"},
                 {"number": 40, "title": "Earlier change", "body": "", "author": "x", "merged_at": "2026-04-01T10:00:00Z",
                  "closed_at": "2026-04-01T10:00:00Z", "updated_at": "2026-04-01T10:00:00Z"}]
        write(work / "all_prs.json", later)
        write(work / "author_history.json", {"merged_before": {"dev0": 3, "dev1": 20, "dev2": 80}})
    return work, prs


class MockScores(unittest.TestCase):
    def test_format_is_the_one_score_py_writes(self):
        prs = [make_pr(i) for i in range(1, 30)]
        records = mock_scores.mock(prs)
        rep = check_data.Report()
        check_data.check_scores(records, {p["number"]: p for p in prs}, rep)
        self.assertEqual((rep.errors, rep.warnings), ([], []))
        self.assertEqual(records[0]["scores"]["risk"]["evidence"], [{"path": "src/cache.py", "lines": "10-15"}])

    def test_same_seed_same_scores_and_scores_vary_between_prs(self):
        prs = [make_pr(i) for i in range(1, 40)]
        self.assertEqual(mock_scores.mock(prs, seed=3), mock_scores.mock(prs, seed=3))
        self.assertNotEqual(mock_scores.mock(prs, seed=3), mock_scores.mock(prs, seed=4))
        self.assertGreater(len({r["scores"]["risk"]["score"] for r in mock_scores.mock(prs)}), 2)

    def test_bad_record_is_explained(self):
        with self.assertRaises(DataError):
            mock_scores.mock([{"title": "no number"}])

    def test_report_built_on_mock_scores_is_marked_as_a_stub(self):
        work, _ = folder(self)
        code, out = run_script("mock_scores.py", cwd=work)
        self.assertEqual(code, 0, out)
        self.assertEqual(read(work / "run_stats.json")["model"], "fake-mock")
        self.assertEqual(run_script("validate.py", cwd=work)[0], 0)
        self.assertTrue(read(work / "validation.json")["synthetic"])

    def test_real_scores_are_never_overwritten_silently(self):
        work, prs = folder(self)
        real = [make_score(p["number"]) for p in prs]
        write(work / "scores.json", real)
        write(work / "run_stats.json", {"model": "some-real-model", "prs_scored": 12})
        code, out = run_script("mock_scores.py", cwd=work)
        self.assertEqual(code, 2, out)
        self.assertIn("не затереть", out)
        self.assertEqual(read(work / "scores.json"), real)
        (work / "run_stats.json").unlink()                       # статистики нет вовсе: тоже считаем настоящим
        self.assertEqual(run_script("mock_scores.py", cwd=work)[0], 2)
        self.assertEqual(read(work / "scores.json"), real)
        self.assertEqual(run_script("mock_scores.py", ["--force"], cwd=work)[0], 0)
        self.assertNotEqual(read(work / "scores.json"), real)
        self.assertEqual(run_script("mock_scores.py", ["--seed", "2"], cwd=work)[0], 0, "пробный файл перезаписывается свободно")


@unittest.skipUnless(HAVE_MATPLOTLIB, "нужен matplotlib")
class Chart(unittest.TestCase):
    def report(self, rows, model="some-real-model"):
        scores = [make_score(i, risk=risk) for i, (risk, _) in enumerate(rows, 1)]
        prs = [make_pr(i) for i in range(1, len(rows) + 1)]
        outs = [outcome(i, problem) for i, (_, problem) in enumerate(rows, 1)]
        return validate.build_report(scores, prs, outcomes=outs, run_stats={"model": model})

    def draw(self, report, **options):
        import chart
        work = Path(tempfile.mkdtemp(prefix="chart-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        shown = chart.draw(report, work / "c.png", **options)
        return shown, work / "c.png"

    def test_png_has_slide_size_and_says_what_the_data_says(self):
        rows = [(5, True)] * 6 + [(4, False)] * 6 + [(3, False)] * 8 + [(1, False)] * 20
        shown, path = self.draw(self.report(rows))
        from PIL import Image
        with Image.open(path) as image:
            self.assertEqual(image.size, (1600, 900))
        self.assertEqual(shown["title"], "PR с высоким баллом риска чаще оказывались проблемными")
        self.assertEqual([(b["risk"], b["label"], b["share"]) for b in shown["bars"]],
                         [("1–2", "0 из 20", 0.0), ("3", "0 из 8", 0.0), ("4–5", "6 из 12", 0.5)])
        self.assertFalse(shown["synthetic"])

    def test_title_does_not_claim_what_was_not_shown(self):
        flat = [(risk, i % 5 == 0) for i, risk in enumerate([1, 2, 3, 4, 5] * 10)]
        self.assertIn("не доказана", self.draw(self.report(flat))[0]["title"])
        few = [(5, True)] * 2 + [(1, False)] * 20
        self.assertIn("слишком мало", self.draw(self.report(few))[0]["title"])

    def test_empty_group_dark_theme_and_strict_rule_do_not_break_it(self):
        rows = [(5, True)] * 6 + [(1, False)] * 20          # в группе «3» нет ни одного PR
        shown, path = self.draw(self.report(rows), dark=True, strict=True)
        self.assertEqual(shown["bars"][1]["label"], "0 из 0")
        self.assertGreater(path.stat().st_size, 10000)

    def test_stub_run_is_stamped(self):
        rows = [(5, True)] * 6 + [(1, False)] * 20
        self.assertTrue(self.draw(self.report(rows, model="fake-mock"))[0]["synthetic"])

    def test_without_outcomes_it_explains_instead_of_drawing(self):
        import chart
        with self.assertRaises(DataError):
            chart.draw({"risk": None, "risk_strict": None}, "unused.png")


class OneCommand(unittest.TestCase):
    def test_missing_required_file_is_named_with_its_owner(self):
        work, _ = folder(self)
        code, out = run_script("p4.py", ["--dir", work])
        self.assertEqual(code, 2, out)
        self.assertIn("НЕТ  scores.json", out)
        self.assertIn("участник 2", out)

    def test_full_run_on_trial_scores(self):
        work, _ = folder(self)
        self.assertEqual(run_script("mock_scores.py", cwd=work)[0], 0)
        code, out = run_script("p4.py", ["--dir", work])
        self.assertEqual(code, 0 if HAVE_MATPLOTLIB else 1, out)     # без matplotlib всё считается, но графика нет
        self.assertEqual([o["number"] for o in read(work / "outcomes.json") if o["reverted"]], [12])
        self.assertEqual({m["author"]: m["level"] for m in read(work / "metrics.json")},
                         {"dev0": "junior", "dev1": "middle", "dev2": "senior"})
        self.assertTrue((work / "validation.md").is_file())
        self.assertEqual((work / "risk_chart.png").is_file(), HAVE_MATPLOTLIB)
        self.assertIn("Проверка пройдена", out)

    def test_without_history_it_still_gives_multipliers_and_says_what_is_missing(self):
        work, _ = folder(self, with_history=False)
        self.assertEqual(run_script("mock_scores.py", cwd=work)[0], 0)
        code, out = run_script("p4.py", ["--dir", work])
        self.assertEqual(code, 1, out)                              # отработало, но с предупреждением
        self.assertIn("нет all_prs.json", out)
        self.assertEqual(len(read(work / "metrics.json")), 3)
        self.assertFalse((work / "risk_chart.png").exists())
        self.assertTrue(all("level_unknown" in m["flags"] for m in read(work / "metrics.json")))


if __name__ == "__main__":
    unittest.main()
