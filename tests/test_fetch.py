"""fetch_prs.py против локальной заглушки GitHub: какие PR выбираются, что вычищается, кэш и сбои."""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import HAVE_REQUESTS, read, run_script

if not HAVE_REQUESTS:
    raise unittest.SkipTest("нужна библиотека requests: pip install requests")

import demo_repo
import fake_github
import fetch_prs

DATA = demo_repo.generate()
SERVICE = ("Release v", "Bump ")


def eligible():
    """Что должно попадать в выборку, посчитано независимо от fetch_prs.py."""
    return sorted((p for p in DATA["pulls"] if p["merged_at"] and p["user"]["type"] != "Bot"
                   and not p["title"].startswith(SERVICE)), key=lambda p: p["merged_at"], reverse=True)


class Helpers(unittest.TestCase):
    def test_noise(self):
        noise = ["package-lock.json", "web/yarn.lock", "dist/app.js", "pkg/vendor/x.go", "static/app.min.js",
                 "img/logo.PNG", "tests/__snapshots__/a.snap", "node_modules/x/index.js"]
        code = ["src/app.py", "docs/build.md", "src/distance.py", "lockfile.py", "vendor.py"]
        for path in noise:
            self.assertTrue(fetch_prs.is_noise({"filename": path}), path)
        for path in code:
            self.assertFalse(fetch_prs.is_noise({"filename": path}), path)

    def test_truncate_patch(self):
        long = "\n".join(f"+line {i}" for i in range(500))
        cut, truncated = fetch_prs.truncate_patch(long)
        self.assertTrue(truncated)
        self.assertEqual(len(cut.splitlines()), fetch_prs.MAX_PATCH_LINES)
        self.assertEqual(fetch_prs.truncate_patch("+a\n+b"), ("+a\n+b", False))

    def test_bots_and_deleted_accounts(self):
        self.assertTrue(fetch_prs.is_bot({"login": "dependabot[bot]", "type": "User"}))
        self.assertTrue(fetch_prs.is_bot({"login": "renovate", "type": "Bot"}))
        self.assertFalse(fetch_prs.is_bot(None))
        self.assertEqual(fetch_prs.login_of(None), "ghost")

    def test_before_merge(self):
        self.assertTrue(fetch_prs.before_merge("2026-05-01T09:00:00Z", "2026-05-01T10:00:00Z"))
        self.assertFalse(fetch_prs.before_merge("2026-05-02T09:00:00Z", "2026-05-01T10:00:00Z"))
        self.assertTrue(fetch_prs.before_merge(None, "2026-05-01T10:00:00Z"))


class AgainstALocalGitHubStub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="fetch-test-"))
        cls.log = []
        cls.server, url = fake_github.start(flaky=True, log=cls.log)
        cls.env = {"GITHUB_API_URL": url, "GITHUB_TOKEN": "test-token", "FETCH_CACHE_DIR": str(cls.dir / "raw"),
                   "FETCH_SEARCH_PAUSE": "0"}
        cls.args = ["--repo", demo_repo.REPO, "--limit", "40", "--out", "prs.json", "--all-prs", "all_prs.json",
                    "--history", "author_history.json"]
        cls.code, cls.out = run_script("fetch_prs.py", cls.args, cwd=cls.dir, env=cls.env)
        cls.prs = read(cls.dir / "prs.json") if cls.code == 0 else []
        cls.by_title = {p["title"]: p for p in cls.prs}

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.dir, True)

    def test_run_succeeds_despite_502_and_rate_limit(self):
        self.assertEqual(self.code, 0, self.out)
        self.assertIn("Лимит исчерпан", self.out)      # заглушка один раз отказала по лимиту
        self.assertEqual(len(self.prs), 40)

    def test_exactly_the_last_merged_prs_are_taken(self):
        expected = [p["number"] for p in eligible()[:40]]
        self.assertEqual([p["number"] for p in self.prs], expected)

    def test_bots_service_and_unmerged_prs_are_left_out(self):
        titles = " | ".join(p["title"] for p in self.prs)
        self.assertNotIn("Release v2.4.0", titles)
        self.assertNotIn("Bump requests", titles)
        self.assertNotIn("dependabot[bot]", {p["author"] for p in self.prs})
        merged_numbers = {p["number"] for p in DATA["pulls"] if p["merged_at"]}
        self.assertTrue({p["number"] for p in self.prs} <= merged_numbers)

    def test_old_pr_with_a_fresh_comment_is_not_mistaken_for_recent(self):
        self.assertNotIn("Initial payment gateway", self.by_title)

    def test_nothing_written_after_the_merge_reaches_the_file(self):
        self.assertNotIn("LEAK-AFTER-MERGE", (self.dir / "prs.json").read_text(encoding="utf-8"))
        leaks = sum("LEAK-AFTER-MERGE" in r["body"] for n in DATA["reviews"] for r in DATA["reviews"][n])
        self.assertGreater(leaks, 0, "в синтетическом репозитории должны быть поздние комментарии, иначе тест пуст")

    def test_bot_reviews_are_dropped_and_human_ones_kept(self):
        text = (self.dir / "prs.json").read_text(encoding="utf-8")
        self.assertNotIn("Automated coverage report", text)
        self.assertIn("Please add a test for the empty case", text)
        self.assertIn("This branch is never covered", text)        # комментарий к строке кода

    def test_noise_binary_oversized_and_renamed_files(self):
        pr = self.by_title["Import the new catalog format"]
        files = {f["path"]: f for f in pr["files"]}
        self.assertEqual(sorted(pr["noise_removed"]), ["assets/logo.png", "package-lock.json"])
        self.assertNotIn("package-lock.json", files)
        self.assertEqual((files["src/catalog_data.py"]["patch"], files["src/catalog_data.py"]["truncated"]), ("", True))
        self.assertEqual((files["src/catalog_old.py"]["patch"], files["src/catalog_old.py"]["truncated"]), ("", False))
        self.assertTrue(files["src/catalog_import.py"]["truncated"])
        self.assertEqual(len(files["src/catalog_import.py"]["patch"].splitlines()), 300)

    def test_ci_result_is_mapped(self):
        for pr in self.prs:
            self.assertEqual(pr["ci"], DATA["checks"][pr["number"]], pr["number"])

    def test_closed_pr_list_covers_the_whole_scored_period(self):
        all_prs = read(self.dir / "all_prs.json")
        period_start = min(p["merged_at"] for p in self.prs)
        self.assertLessEqual(min(p["updated_at"] for p in all_prs), period_start)
        numbers = {p["number"] for p in all_prs}
        later = {p["number"] for p in DATA["pulls"] if p["updated_at"] >= period_start}
        self.assertTrue(later <= numbers, "в списке должны быть все PR, обновлённые с начала периода")
        self.assertEqual(len(numbers), len(all_prs), "без дублей")

    def test_author_history(self):
        history = read(self.dir / "author_history.json")
        self.assertEqual(history["repo"], demo_repo.REPO)
        for author in {p["author"] for p in self.prs}:
            self.assertEqual(history["merged_before"][author], demo_repo.AUTHORS[author])

    def test_second_launch_makes_no_requests(self):
        before = len(self.log)
        code, out = run_script("fetch_prs.py", self.args, cwd=self.dir, env=self.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.log), before)

    def test_min_age_keeps_only_prs_old_enough_to_have_an_outcome(self):
        work = self.dir / "aged"
        work.mkdir(exist_ok=True)
        code, out = run_script("fetch_prs.py", ["--repo", demo_repo.REPO, "--limit", "30", "--min-age-days", "30",
                                                "--out", "prs.json"], cwd=work, env=self.env)
        self.assertEqual(code, 0, out)
        prs = read(work / "prs.json")
        snapshot = max(p["updated_at"] for p in DATA["pulls"])
        cutoff = (datetime.strptime(snapshot, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                  - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(all(p["merged_at"] <= cutoff for p in prs))
        expected = [p["number"] for p in eligible() if p["merged_at"] <= cutoff][:30]
        self.assertEqual([p["number"] for p in prs], expected)

    def test_page_limit_is_reported_not_hidden(self):
        work = self.dir / "capped"
        work.mkdir(exist_ok=True)
        code, out = run_script("fetch_prs.py", ["--repo", demo_repo.REPO, "--limit", "140", "--all-pages", "1",
                                                "--out", "prs.json"], cwd=work, env=self.env)
        self.assertEqual(code, 0, out)
        self.assertIn("выборка может быть неполной", out)

    def test_unknown_repository_gives_a_clear_error(self):
        code, out = run_script("fetch_prs.py", ["--repo", "nobody/nothing", "--out", "x.json"], cwd=self.dir, env=self.env)
        self.assertEqual(code, 2, out)
        self.assertIn("404", out)
        self.assertNotIn("Traceback", out)
        self.assertFalse((self.dir / "x.json").exists())

    def test_malformed_repo_name_is_refused(self):
        code, out = run_script("fetch_prs.py", ["--repo", "https://github.com/a/b"], cwd=self.dir, env=self.env)
        self.assertEqual(code, 2, out)
        self.assertIn("owner/name", out)

    def test_damaged_cache_file_is_fetched_again(self):
        work = self.dir / "damaged"
        work.mkdir(exist_ok=True)
        env = dict(self.env, FETCH_CACHE_DIR=str(work / "raw"))
        args = ["--repo", demo_repo.REPO, "--limit", "3", "--out", "prs.json"]
        self.assertEqual(run_script("fetch_prs.py", args, cwd=work, env=env)[0], 0)
        first = read(work / "prs.json")
        for path in (work / "raw").glob("*.json"):
            path.write_text('[{"number": 1', encoding="utf-8")       # оборванная запись
        code, out = run_script("fetch_prs.py", args, cwd=work, env=env)
        self.assertEqual(code, 0, out)
        self.assertEqual(read(work / "prs.json"), first)


class BotHeavyRepository(unittest.TestCase):
    """Репозиторий, где большинство PR открывают боты: в первых страницах списка людей мало."""

    def test_selection_keeps_paging_until_it_has_the_true_last_merged_prs(self):
        data = demo_repo.generate()
        number = max(p["number"] for p in data["pulls"])
        for k in range(260):
            number += 1
            when = demo_repo.stamp(demo_repo.NOW - timedelta(hours=6 * k))
            data["pulls"].append({"number": number, "title": f"Bump lib{k} from 1.{k} to 1.{k + 1}", "body": "",
                                  "user": {"login": "dependabot[bot]", "type": "Bot"}, "created_at": when,
                                  "merged_at": when, "closed_at": when, "updated_at": when, "head": {"sha": f"{number:040x}"}})
        server, url = fake_github.start(data=data)
        self.addCleanup(server.stop)
        work = Path(tempfile.mkdtemp(prefix="fetch-bots-"))
        self.addCleanup(shutil.rmtree, work, True)
        env = {"GITHUB_API_URL": url, "GITHUB_TOKEN": "t", "FETCH_CACHE_DIR": str(work / "raw")}
        code, out = run_script("fetch_prs.py", ["--repo", demo_repo.REPO, "--limit", "60", "--out", "prs.json"], cwd=work, env=env)
        self.assertEqual(code, 0, out)
        expected = [p["number"] for p in eligible()[:60]]
        self.assertEqual([p["number"] for p in read(work / "prs.json")], expected)
        self.assertNotIn("неполной", out)


if __name__ == "__main__":
    unittest.main()
