"""stats_util.py: сверка с известными значениями."""
import unittest

from tests.helpers import ROOT  # noqa: F401  (настраивает пути)

import stats_util as st


class Stats(unittest.TestCase):
    def test_wilson_matches_published_values(self):
        low, high = st.wilson(0, 10)
        self.assertAlmostEqual(low, 0.0, places=4)
        self.assertAlmostEqual(high, 0.2775, places=3)
        low, high = st.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)
        self.assertIsNone(st.wilson(0, 0))

    def test_fisher_matches_textbook_examples(self):
        # «Дама, пробующая чай»: 3 из 4 угаданы, односторонний p = 17/70
        self.assertAlmostEqual(st.fisher_greater(3, 1, 1, 3), 17 / 70, places=6)
        self.assertAlmostEqual(st.fisher_greater(4, 0, 0, 4), 1 / 70, places=6)
        self.assertEqual(st.fisher_greater(0, 5, 0, 5), 1.0)
        self.assertEqual(st.fisher_greater(0, 0, 0, 0), 1.0)

    def test_auc(self):
        self.assertEqual(st.auc([5, 4], [1, 2, 3]), 1.0)
        self.assertEqual(st.auc([1], [2, 3]), 0.0)
        self.assertEqual(st.auc([3, 3], [3, 3]), 0.5)
        self.assertAlmostEqual(st.auc([4, 2], [3, 1]), 0.75)
        self.assertIsNone(st.auc([], [1]))

    def test_interval_is_reproducible_and_contains_the_estimate(self):
        pos, neg = [5, 4, 4, 3, 5, 2], [1, 2, 3, 2, 1, 3, 4, 2, 1, 2]
        first, second = st.auc_interval(pos, neg), st.auc_interval(pos, neg)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], st.auc(pos, neg))
        self.assertGreaterEqual(first[1], st.auc(pos, neg))


if __name__ == "__main__":
    unittest.main()
