"""score.py: что уходит модели, проверка ответа, ссылки на код, медиана, кэш и поведение при сбоях API."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PATCH, ROOT, make_pr, read, run_script, write

import fake_llm
import score


def answer(**scores):
    base = {"complexity": 3, "quality": 3, "risk": 3, "clarity": 3}
    base.update(scores)
    return {"summary": "s", "scores": {c: {"score": v, "reason": "r", "evidence": []} for c, v in base.items()}}


class AuthorIsHidden(unittest.TestCase):
    def test_login_removed_from_title_body_reviews_and_diff(self):
        pr = make_pr(author="octocat", title="Fix cache (by @octocat)",
                     body="octocat fixed it properly this time around.\nSigned-off-by: Octo Cat <o@example.com>",
                     files=[{"path": "src/cache.py", "patch": PATCH + "\n+# TODO(octocat): remove", "truncated": False}],
                     reviews=[{"state": "COMMENTED", "body": "thanks @OctoCat, looks good"}])
        text = score.pr_to_text(pr)
        self.assertNotIn("octocat", text.lower())
        self.assertNotIn("Signed-off-by", text)
        self.assertIn("TODO([author])", text)
        self.assertFalse(score.author_still_visible(text, "octocat"))

    def test_login_inside_a_longer_word_is_left_alone(self):
        text = score.pr_to_text(make_pr(author="max", title="Raise max_retries and maximum size"))
        self.assertIn("max_retries", text)
        self.assertIn("maximum", text)

    def test_short_login_that_is_also_a_code_word_does_not_garble_the_diff(self):
        code = "@@ -1,3 +1,5 @@\n+limit = max(a, b)\n+max = 3\n+# TODO(max): tidy up, thanks @max\n ctx"
        pr = make_pr(author="max", files=[{"path": "src/limits.py", "patch": code, "truncated": False}])
        text = score.pr_to_text(pr)
        self.assertIn("limit = max(a, b)", text)                 # код не тронут
        self.assertIn("+max = 3", text)
        self.assertIn("TODO([author]): tidy up, thanks [author]", text)
        self.assertTrue(score.author_still_visible(text, "max"))   # и это честно попадает в отчёт

    def test_login_in_a_path_is_kept_and_reported(self):
        pr = make_pr(author="octocat", files=[{"path": "docs/authors/octocat.md", "patch": PATCH, "truncated": False}])
        text = score.pr_to_text(pr)
        self.assertIn("docs/authors/octocat.md", text)          # путь нужен для ссылок, его не трогаем
        self.assertTrue(score.author_still_visible(text, "octocat"))

    def test_author_field_itself_is_never_printed(self):
        text = score.pr_to_text(make_pr(author="someone-unique-42"))
        self.assertNotIn("someone-unique-42", text)


class AnswerIsChecked(unittest.TestCase):
    def test_json_inside_markdown_fence_is_read(self):
        self.assertEqual(score.parse_json('Here:\n```json\n{"a": 1}\n```')["a"], 1)

    def test_text_without_json_is_rejected(self):
        with self.assertRaises(ValueError):
            score.parse_json("I cannot score this")

    def test_valid_answer_passes_and_whole_floats_become_integers(self):
        a = answer()
        a["scores"]["risk"]["score"] = 4.0
        self.assertEqual(score.validate(a)["scores"]["risk"]["score"], 4)

    def test_bad_answers_are_rejected(self):
        cases = {"score out of range": answer(risk=7), "zero score": answer(quality=0), "fraction": answer(clarity=2.5),
                 "boolean": answer(risk=True), "text score": answer(risk="high")}
        missing = answer()
        del missing["scores"]["clarity"]
        cases["missing criterion"] = missing
        no_reason = answer()
        no_reason["scores"]["risk"]["reason"] = "  "
        cases["empty reason"] = no_reason
        cases["no summary"] = dict(answer(), summary="")
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                score.validate(bad)


class EvidenceIsChecked(unittest.TestCase):
    def setUp(self):
        self.pr = make_pr(files=[{"path": "src/cache.py", "patch": PATCH, "truncated": False},       # строки 10-15
                                 {"path": "src/huge.py", "patch": "", "truncated": True}])           # diff не показан

    def check(self, evidence):
        a = answer()
        a["scores"]["risk"]["evidence"] = evidence
        a = score.validate(a)
        removed, cleared = score.check_evidence(a, self.pr)
        return a["scores"]["risk"]["evidence"], removed, cleared

    def test_hunk_ranges(self):
        self.assertEqual(score.hunk_ranges(PATCH), [(10, 15)])
        self.assertEqual(score.hunk_ranges("@@ -1 +1 @@\n-a\n+b\n@@ -40,2 +44,0 @@\n-x\n-y"), [(1, 1), (44, 44)])
        self.assertEqual(score.hunk_ranges("no headers here"), [])

    def test_parse_lines(self):
        self.assertEqual(score.parse_lines("10-18"), (10, 18))
        self.assertEqual(score.parse_lines("L10-L18"), (10, 18))
        self.assertEqual(score.parse_lines("12"), (12, 12))
        self.assertEqual(score.parse_lines("18–10"), (10, 18))
        self.assertIsNone(score.parse_lines("around the top"))
        self.assertIsNone(score.parse_lines(""))

    def test_good_reference_is_kept(self):
        self.assertEqual(self.check([{"path": "src/cache.py", "lines": "11-13"}]),
                         ([{"path": "src/cache.py", "lines": "11-13"}], 0, 0))

    def test_invented_file_is_removed(self):
        self.assertEqual(self.check([{"path": "src/ghost.py", "lines": "1-5"}]), ([], 1, 0))

    def test_file_whose_diff_was_not_shown_is_removed(self):
        self.assertEqual(self.check([{"path": "src/huge.py", "lines": "1-5"}]), ([], 1, 0))

    def test_lines_far_outside_the_diff_are_cleared_but_the_path_stays(self):
        self.assertEqual(self.check([{"path": "src/cache.py", "lines": "900-950"}]),
                         ([{"path": "src/cache.py", "lines": ""}], 0, 1))

    def test_a_couple_of_lines_off_is_tolerated(self):
        kept, removed, cleared = self.check([{"path": "src/cache.py", "lines": "16-18"}])
        self.assertEqual((kept[0]["lines"], removed, cleared), ("16-18", 0, 0))

    def test_unreadable_lines_are_cleared(self):
        self.assertEqual(self.check([{"path": "src/cache.py", "lines": "near the top"}]),
                         ([{"path": "src/cache.py", "lines": ""}], 0, 1))

    def test_duplicates_and_bare_paths_next_to_real_references_are_dropped(self):
        kept, _, _ = self.check([{"path": "src/cache.py", "lines": "11-13"}, {"path": "src/cache.py", "lines": "11-13"},
                                 {"path": "src/cache.py", "lines": "5000"}])
        self.assertEqual(kept, [{"path": "src/cache.py", "lines": "11-13"}])

    def test_applying_twice_changes_nothing(self):
        a = answer()
        a["scores"]["risk"]["evidence"] = [{"path": "src/ghost.py", "lines": "1"}, {"path": "src/cache.py", "lines": "900"}]
        a = score.validate(a)
        score.check_evidence(a, self.pr)
        snapshot = repr(a)
        self.assertEqual(score.check_evidence(a, self.pr), (0, 0))
        self.assertEqual(repr(a), snapshot)


class RunsAreCombined(unittest.TestCase):
    def test_median_and_reason_come_from_a_run_with_the_median_score(self):
        runs = [score.validate(answer(risk=2)), score.validate(answer(risk=4)), score.validate(answer(risk=4))]
        runs[0]["scores"]["risk"]["reason"] = "low"
        runs[1]["scores"]["risk"]["reason"] = "high"
        record, per_run = score.combine(7, runs)
        self.assertEqual(record["scores"]["risk"]["score"], 4)
        self.assertEqual(record["scores"]["risk"]["reason"], "high")
        self.assertEqual(per_run["risk"], [2, 4, 4])
        self.assertTrue(record["unstable"])           # 2 и 4 расходятся больше чем на 1
        self.assertEqual(record["runs"], 3)

    def test_spread_of_one_is_not_unstable(self):
        record, _ = score.combine(7, [score.validate(answer(risk=3)), score.validate(answer(risk=4))])
        self.assertFalse(record["unstable"])
        self.assertEqual(record["scores"]["risk"]["score"], 3)   # при чётном числе прогонов берётся нижняя медиана: балл остаётся целым


class BadRecordsAreNamed(unittest.TestCase):
    def test_record_problem(self):
        self.assertIsNone(score.record_problem(make_pr()))
        self.assertIn("number", score.record_problem(dict(make_pr(), number="12")))
        self.assertIn("path", score.record_problem(dict(make_pr(), files=[{"filename": "a.py"}])))
        self.assertIn("files", score.record_problem(dict(make_pr(), files="a.py")))
        self.assertIn("reviews", score.record_problem(dict(make_pr(), reviews=["ok"])))


class AgainstALocalModelStub(unittest.TestCase):
    """score.py запускается отдельным процессом и ходит в локальную заглушку вместо настоящего API."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="score-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.prs = [make_pr(i, author="octocat", title=f"Change number {i} by @octocat") for i in range(1, 7)]
        write(self.dir / "prs.json", self.prs)

    def start(self, **options):
        server, url, fake = fake_llm.start(**options)
        self.addCleanup(server.stop)
        self.env = {"SCORER_BASE_URL": url, "SCORER_MODEL": "fake-llm", "SCORER_API_KEY": "local"}
        return fake

    def score(self, *args, env=None):
        return run_script("score.py", ["--rubric", ROOT / "rubric.md", "--http-retries", "3"] + list(args),
                          cwd=self.dir, env=dict(self.env, **(env or {})))

    def test_scores_every_pr_and_second_launch_costs_nothing(self):
        fake = self.start()
        code, out = self.score()
        self.assertEqual(code, 0, out)
        scores = read(self.dir / "scores.json")
        self.assertEqual([r["number"] for r in scores], [1, 2, 3, 4, 5, 6])
        for record in scores:
            self.assertEqual(record["runs"], 3)
            for item in record["scores"].values():
                self.assertIn(item["score"], (1, 2, 3, 4, 5))
                self.assertTrue(item["reason"])
                for ev in item["evidence"]:
                    self.assertEqual(ev["path"], "src/cache.py")     # выдуманные файлы заглушки отсеяны
                    self.assertNotIn("9000", ev["lines"])            # и строки вне diff тоже
        hits = fake.hits
        code, out = self.score()
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.hits, hits, "второй запуск не должен делать запросов")
        self.assertEqual(read(self.dir / "scores.json"), scores)
        self.assertEqual(read(self.dir / "run_stats.json")["runs_from_cache"], 18)

    def test_author_login_never_leaves_the_machine(self):
        fake = self.start()
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(len(fake.requests), 6)
        for payload in fake.requests:
            self.assertNotIn("octocat", repr(payload).lower())
        self.assertEqual(read(self.dir / "run_stats.json")["author_login_still_visible"], [])

    def test_rejected_answers_are_asked_again(self):
        self.start(first_answer_bad=True)      # первый ответ на каждый PR содержит балл 7
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(read(self.dir / "run_stats.json")["answers_rejected_and_retried"], 6)
        for record in read(self.dir / "scores.json"):
            self.assertLessEqual(record["scores"]["risk"]["score"], 5)

    def test_model_that_never_answers_in_format_fails_cleanly(self):
        self.start(never_valid=True)
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("no valid answer after 3 attempts", out)
        self.assertFalse((self.dir / "scores.json").exists())

    def test_provider_without_strict_schema_steps_down_to_plain_json(self):
        self.start(reject_schema=True)
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("plain JSON mode", out)
        self.assertEqual(len(read(self.dir / "scores.json")), 6)

    def test_rate_limit_answers_are_survived(self):
        self.start(limit_every=3)
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("slowing to about", out)
        self.assertEqual(len(read(self.dir / "scores.json")), 6)

    def test_exhausted_quota_stops_the_run_instead_of_retrying_forever(self):
        self.start(always_429=True)
        code, out = self.score("--runs", "1", "--http-retries", "2")
        self.assertEqual(code, 2, out)
        self.assertIn("quota", out)
        self.assertFalse((self.dir / "scores.json").exists())

    def test_wrong_key_stops_before_the_fan_out(self):
        fake = self.start()
        code, out = self.score(env={"SCORER_API_KEY": "bad-key"})
        self.assertEqual(code, 2, out)
        self.assertIn("check the key", out)
        self.assertEqual(fake.hits, 0)
        self.assertFalse((self.dir / "scores.json").exists())

    def test_broken_record_is_skipped_and_the_rest_is_scored(self):
        self.start()
        write(self.dir / "prs.json", self.prs + [dict(make_pr(99), files=[{"filename": "a.py"}]), self.prs[0]])
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 1, out)                       # часть не оценена: код 1, но файл записан
        self.assertIn("SKIPPED #99", out)
        self.assertEqual([r["number"] for r in read(self.dir / "scores.json")], [1, 2, 3, 4, 5, 6])
        stats = read(self.dir / "run_stats.json")
        self.assertEqual(stats["failed"][0]["number"], 99)
        self.assertEqual(stats["duplicates_skipped"], 1)

    def test_damaged_cache_file_is_requested_again(self):
        fake = self.start(misbehave=False)
        self.assertEqual(self.score("--runs", "1")[0], 0)
        victim = sorted((self.dir / "cache").glob("3_*.json"))[0]
        victim.write_text('{"answer": {"summ', encoding="utf-8")
        hits = fake.hits
        code, out = self.score("--runs", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.hits, hits + 1)

    def test_changing_the_rubric_scores_everything_again(self):
        fake = self.start(misbehave=False)
        self.assertEqual(self.score("--runs", "1")[0], 0)
        hits = fake.hits
        rubric = self.dir / "rubric.md"
        rubric.write_text((ROOT / "rubric.md").read_text(encoding="utf-8") + "\nExtra rule.", encoding="utf-8")
        code, out = run_script("score.py", ["--rubric", rubric, "--runs", "1"], cwd=self.dir, env=self.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.hits, hits + 6)

    def test_empty_rubric_is_refused(self):
        self.start()
        (self.dir / "empty.md").write_text("<!-- only a comment -->", encoding="utf-8")
        code, out = run_script("score.py", ["--rubric", self.dir / "empty.md"], cwd=self.dir, env=self.env)
        self.assertEqual(code, 2, out)
        self.assertIn("is empty", out)

    def test_without_settings_and_without_a_terminal_it_explains_what_to_set(self):
        code, out = run_script("score.py", ["--rubric", ROOT / "rubric.md"], cwd=self.dir, env={})
        self.assertEqual(code, 2, out)
        self.assertIn("SCORER_API_KEY", out)


if __name__ == "__main__":
    unittest.main()
