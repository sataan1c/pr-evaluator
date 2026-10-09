"""outcomes.py: что считается откатом и исправлением, окно наблюдения."""
import unittest

from tests.helpers import make_pr

import outcomes
from common import DataError


def closed(number, title, body="", merged="2026-05-03T10:00:00Z", **extra):
    return dict({"number": number, "title": title, "body": body, "author": "someone", "merged_at": merged,
                 "closed_at": merged or "2026-05-03T10:00:00Z", "updated_at": merged or "2026-05-03T10:00:00Z"}, **extra)


SNAPSHOT = closed(900, "Latest change in the repo", merged="2026-07-01T00:00:00Z")


def label(prs, later, window=14):
    records, info = outcomes.label(prs, later + [SNAPSHOT], window)
    return {r["number"]: r for r in records}, info


class Reverts(unittest.TestCase):
    def setUp(self):
        self.prs = [make_pr(1, title="Fix cache invalidation"), make_pr(2, title="Add bulk export")]

    def test_github_revert_button(self):
        got, _ = label(self.prs, [closed(50, 'Revert "Fix cache invalidation"', "Reverts acme/shop#1")])
        self.assertEqual((got[1]["reverted"], got[1]["reverted_by"], got[1]["problem"], got[1]["problem_strict"]), (True, [50], True, True))
        self.assertFalse(got[2]["problem"])

    def test_number_inside_the_quoted_title(self):
        got, _ = label(self.prs, [closed(50, 'Revert "Add bulk export (#2)"')])
        self.assertEqual(got[2]["reverted_by"], [50])
        self.assertFalse(got[1]["reverted"])

    def test_only_a_quoted_title(self):
        got, _ = label(self.prs, [closed(50, 'Revert "Add bulk export"')])
        self.assertEqual(got[2]["reverted_by"], [50])

    def test_quoted_title_shared_by_two_prs_points_at_the_latest_one_before_the_revert(self):
        prs = [make_pr(1, title="Tune timeouts", merged_at="2026-04-01T10:00:00Z"),
               make_pr(2, title="Tune timeouts", merged_at="2026-05-01T10:00:00Z"),
               make_pr(3, title="Tune timeouts", merged_at="2026-05-20T10:00:00Z")]      # принят уже после отката
        got, _ = label(prs, [closed(50, 'Revert "Tune timeouts"', merged="2026-05-03T10:00:00Z")])
        self.assertEqual([n for n in got if got[n]["reverted"]], [2])

    def test_other_wordings(self):
        for title, body in [("Partially revert #1", ""), ("chore: revert #1", ""), ("Restore the old cache", "Reverts #1, it broke prod"),
                            ("[backport] Revert \"Fix cache invalidation\"", "")]:
            with self.subTest(title):
                got, _ = label(self.prs, [closed(50, title, body)])
                self.assertEqual(got[1]["reverted_by"], [50])

    def test_revert_closed_without_merge_reverted_nothing(self):
        got, _ = label(self.prs, [closed(50, 'Revert "Fix cache invalidation"', "Reverts #1", merged=None)])
        self.assertFalse(got[1]["problem"])

    def test_revert_merged_before_the_pr_cannot_be_its_revert(self):
        got, _ = label(self.prs, [closed(50, 'Revert "Fix cache invalidation"', "Reverts #1", merged="2026-04-01T10:00:00Z")])
        self.assertFalse(got[1]["problem"])

    def test_reference_to_another_repository_is_ignored(self):
        got, _ = label(self.prs, [closed(50, "Revert upstream change", "This undoes other/project#1 in our fork")])
        self.assertFalse(got[1]["problem"])

    def test_a_fix_that_mentions_the_word_revert_is_a_fix(self):
        got, _ = label(self.prs, [closed(50, "Fix revert button", "Broken since #1")])
        self.assertEqual((got[1]["reverted"], got[1]["fixed_by"]), (False, [50]))

    def test_bot_prs_do_not_count(self):
        got, _ = label(self.prs, [closed(50, 'Revert "Fix cache invalidation"', "Reverts #1", is_bot=True)])
        self.assertFalse(got[1]["problem"])

    def test_revert_of_a_revert(self):
        prs = self.prs + [make_pr(50, title='Revert "Fix cache invalidation"', body="Reverts #1", merged_at="2026-05-03T10:00:00Z")]
        got, _ = label(prs, [closed(50, 'Revert "Fix cache invalidation"', "Reverts #1"),
                             closed(60, 'Revert "Revert "Fix cache invalidation""', "Reverts #50", merged="2026-05-05T10:00:00Z")])
        self.assertEqual(got[1]["reverted_by"], [50])
        self.assertEqual(got[50]["reverted_by"], [60])

    def test_revert_of_a_revert_does_not_count_against_the_original_again(self):
        # GitHub при squash дописывает номер в заголовок: он оказывается внутри вложенной цитаты
        first = 'Revert "Fix cache invalidation (#1)"'
        prs = self.prs + [make_pr(50, title=first, body="", merged_at="2026-05-03T10:00:00Z")]
        for body in ("Reverts acme/shop#50", ""):               # с явной ссылкой и только с цитатой заголовка
            with self.subTest(body=body):
                got, _ = label(prs, [closed(50, first), closed(60, 'Revert "' + first + '"', body, merged="2026-05-05T10:00:00Z")])
                self.assertEqual(got[1]["reverted_by"], [50])
                self.assertEqual(got[50]["reverted_by"], [60])


class Fixes(unittest.TestCase):
    def setUp(self):
        self.prs = [make_pr(1, title="Add bulk export")]

    def test_fix_with_a_causal_reference_counts_in_both_rules(self):
        got, _ = label(self.prs, [closed(50, "Fix crash in export", "Regression introduced in #1")])
        self.assertEqual((got[1]["fixed_by"], got[1]["fixed_by_explicit"], got[1]["problem"], got[1]["problem_strict"]),
                         ([50], [50], True, True))

    def test_fix_that_only_mentions_the_pr_counts_in_the_loose_rule_only(self):
        got, _ = label(self.prs, [closed(50, "Hotfix export headers", "Small correction, see #1")])
        self.assertEqual((got[1]["fixed_by"], got[1]["fixed_by_explicit"], got[1]["problem"], got[1]["problem_strict"]),
                         ([50], [], True, False))

    def test_fix_outside_the_window_does_not_count(self):
        got, _ = label(self.prs, [closed(50, "Fix crash in export", "Regression from #1", merged="2026-05-20T10:00:00Z")])
        self.assertFalse(got[1]["problem"])
        got, _ = label(self.prs, [closed(50, "Fix crash in export", "Regression from #1", merged="2026-05-20T10:00:00Z")], window=30)
        self.assertTrue(got[1]["problem"])

    def test_a_pr_without_fix_words_is_not_a_fix(self):
        got, _ = label(self.prs, [closed(50, "Add CSV to export", "Builds on #1")])
        self.assertFalse(got[1]["problem"])

    def test_words_that_only_contain_fix_do_not_count(self):
        got, _ = label(self.prs, [closed(50, "Add prefix and suffix options", "Builds on #1")])
        self.assertFalse(got[1]["problem"])

    def test_link_form_of_reference(self):
        got, _ = label(self.prs, [closed(50, "Fix export", "Broken by https://github.com/acme/shop/pull/1")])
        self.assertEqual(got[1]["fixed_by"], [50])


class ObservationWindow(unittest.TestCase):
    def test_recent_pr_is_marked_not_observable(self):
        prs = [make_pr(1, merged_at="2026-05-01T10:00:00Z"), make_pr(2, merged_at="2026-06-25T00:00:00Z")]
        got, info = label(prs, [])
        self.assertTrue(got[1]["observable"])
        self.assertFalse(got[2]["observable"])            # до снимка 1 июля прошло 6 дней из 14
        self.assertEqual(got[2]["observed_days"], 6.0)
        self.assertEqual(info["snapshot"], "2026-07-01T00:00:00Z")

    def test_history_shorter_than_the_period_is_flagged(self):
        prs = [make_pr(1, merged_at="2026-03-01T10:00:00Z")]
        _, info = outcomes.label(prs, [closed(50, "Later change", merged="2026-06-01T00:00:00Z")])
        self.assertFalse(info["history_covers_period"])
        _, info = outcomes.label(prs, [closed(50, "Later change", merged="2026-06-01T00:00:00Z"),
                                      closed(40, "Earlier change", merged="2026-02-01T00:00:00Z")])
        self.assertTrue(info["history_covers_period"])

    def test_bad_input_is_explained(self):
        with self.assertRaises(DataError):
            outcomes.label([{"number": 1}], [SNAPSHOT])
        with self.assertRaises(DataError):
            outcomes.label([], [SNAPSHOT])


if __name__ == "__main__":
    unittest.main()
