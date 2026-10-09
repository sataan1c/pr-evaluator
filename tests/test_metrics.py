"""metrics.py: множитель, нормы уровней, защита от дробления PR, флаги."""
import copy
import unittest

from tests.helpers import ROOT, make_pr, make_score

import metrics
from common import DataError

CFG = metrics.load_config(ROOT / "config" / "levels.json")


def person(author, count, start=1, lines=60, **scores):
    prs = [make_pr(start + i, author=author, additions=lines, deletions=0) for i in range(count)]
    return prs, [make_score(start + i, **scores) for i in range(count)]


def compute(prs, scores, history=None, cfg=CFG):
    records, info = metrics.compute(prs, scores, {"merged_before": history or {}}, cfg)
    return {r["author"]: r for r in records}, info


class Levels(unittest.TestCase):
    def test_boundaries_follow_the_agreed_rule(self):
        for count, level in [(0, "junior"), (9, "junior"), (10, "middle"), (50, "middle"), (51, "senior"), (500, "senior")]:
            self.assertEqual(metrics.level_of("a", {"a": count}, CFG), (level, "history"), count)

    def test_override_wins_and_unknown_author_gets_the_default(self):
        cfg = dict(CFG, level_overrides={"a": "senior"})
        self.assertEqual(metrics.level_of("a", {"a": 0}, cfg), ("senior", "override"))
        self.assertEqual(metrics.level_of("b", {}, cfg), ("middle", "unknown"))


class Multiplier(unittest.TestCase):
    def test_formula_and_limits(self):
        self.assertEqual(metrics.multiplier(3.2, 3.2, CFG), 1.0)
        self.assertEqual(metrics.multiplier(3.52, 3.2, CFG), 1.05)       # на 10% выше нормы -> +5%
        self.assertEqual(metrics.multiplier(5.0, 2.6, CFG), 1.15)        # потолок
        self.assertEqual(metrics.multiplier(1.0, 3.8, CFG), 0.9)         # пол

    def test_record_matches_the_agreed_format(self):
        prs, scores = person("mid", 4, complexity=4, quality=4, risk=5, clarity=2)
        got, _ = compute(prs, scores, {"mid": 20})
        r = got["mid"]
        self.assertEqual((r["level"], r["pr_count"], r["medians"]), ("middle", 4, {"complexity": 4, "quality": 4, "risk": 5, "clarity": 2}))
        self.assertEqual(r["composite"], 3.6)                             # 0.4*4 + 0.4*4 + 0.2*2
        self.assertEqual((r["norm"], r["multiplier"], r["flags"]), (3.2, 1.06, []))

    def test_risk_does_not_move_the_multiplier(self):
        prs, low = person("a", 4, risk=1)
        _, high = person("a", 4, risk=5)
        self.assertEqual(compute(prs, low, {"a": 20})[0]["a"]["multiplier"], compute(prs, high, {"a": 20})[0]["a"]["multiplier"])

    def test_too_few_prs_gives_no_multiplier(self):
        prs, scores = person("new", 2)
        r = compute(prs, scores, {"new": 0})[0]["new"]
        self.assertIsNone(r["multiplier"])
        self.assertIn("insufficient_data", r["flags"])


class Gaming(unittest.TestCase):
    def test_splitting_work_into_small_prs_does_not_raise_the_multiplier(self):
        prs, scores = person("honest", 4, complexity=3, quality=4, clarity=3)
        honest = compute(prs, scores, {"honest": 20})[0]["honest"]
        # тот же человек добавляет двадцать однострочных PR
        more_prs, more_scores = person("honest", 20, start=100, lines=2, complexity=1, quality=4, clarity=3)
        padded = compute(prs + more_prs, scores + more_scores, {"honest": 20})[0]["honest"]
        self.assertLessEqual(padded["multiplier"], honest["multiplier"])
        self.assertIn("many_small_prs", padded["flags"])
        self.assertNotIn("many_small_prs", honest["flags"])

    def test_unstable_scores_are_flagged(self):
        prs, scores = person("a", 4)
        for s in scores[:2]:
            s["unstable"] = True
        self.assertIn("unstable_scores", compute(prs, scores, {"a": 20})[0]["a"]["flags"])


class Norms(unittest.TestCase):
    def build(self, composites):
        prs, scores = [], []
        for i, value in enumerate(composites):
            p, s = person(f"dev{i}", 3, start=100 * (i + 1), complexity=value, quality=value, clarity=value)
            prs, scores = prs + p, scores + s
        return prs, scores, {f"dev{i}": 20 for i in range(len(composites))}

    def test_with_few_people_the_norm_comes_from_the_config(self):
        got, info = compute(*self.build([2, 3, 4]))
        self.assertEqual(info["norm_source"]["middle"], "config")
        self.assertEqual(got["dev0"]["norm"], 3.2)

    def test_with_enough_people_the_norm_is_their_median(self):
        got, info = compute(*self.build([2, 3, 3, 4, 5]))
        self.assertEqual((info["norm_source"]["middle"], info["norms"]["middle"]), ("calibrated", 3))
        self.assertEqual(got["dev1"]["multiplier"], 1.0)
        self.assertGreater(got["dev4"]["multiplier"], 1.0)
        self.assertLess(got["dev0"]["multiplier"], 1.0)

    def test_people_without_enough_prs_do_not_shift_the_norm(self):
        prs, scores, history = self.build([2, 3, 3, 4, 5])
        extra_prs, extra_scores = person("drive-by", 1, start=9000, complexity=5, quality=5, clarity=5)
        history["drive-by"] = 20
        _, info = compute(prs + extra_prs, scores + extra_scores, history)
        self.assertEqual(info["norms"]["middle"], 3)


class Inputs(unittest.TestCase):
    def test_scores_without_a_pr_are_reported_not_counted(self):
        prs, scores = person("a", 3)
        got, info = compute(prs, scores + [make_score(777)], {"a": 20})
        self.assertEqual(info["scores_without_pr"], [777])
        self.assertEqual(got["a"]["pr_count"], 3)

    def test_period_filter(self):
        prs = [make_pr(1, author="a", merged_at="2026-01-15T00:00:00Z"), make_pr(2, author="a", merged_at="2026-04-15T00:00:00Z")]
        records, _ = metrics.compute(prs, [make_score(1), make_score(2)], {}, CFG, since="2026-04-01", until="2026-06-30")
        self.assertEqual(records[0]["pr_count"], 1)

    def test_broken_config_is_explained(self):
        cases = {"weights do not sum to 1": lambda c: c["weights"]["junior"].update(quality=0.9),
                 "missing norm": lambda c: c["norms"].pop("senior"),
                 "limits upside down": lambda c: c["multiplier"].update(min=1.2),
                 "unknown level in overrides": lambda c: c.update(level_overrides={"a": "principal"})}
        import json, tempfile, os
        for name, damage in cases.items():
            cfg = copy.deepcopy(CFG)
            damage(cfg)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump(cfg, f)
            try:
                with self.subTest(name), self.assertRaises(DataError):
                    metrics.load_config(f.name)
            finally:
                os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
