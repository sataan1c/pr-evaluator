"""Сквозной прогон: заглушка GitHub -> выгрузка -> оценка -> исходы -> множитель -> стиль -> валидация -> проверка."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import HAVE_REQUESTS, read, run_script

import check_data
import demo_repo
import fake_github
import fake_llm
from tests.helpers import ROOT


@unittest.skipUnless(HAVE_REQUESTS, "нужна библиотека requests: pip install requests")
class WholePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="pipeline-test-"))
        cls.github, github_url = fake_github.start()
        cls.llm, llm_url, cls.fake = fake_llm.start()
        cls.env = {"GITHUB_API_URL": github_url, "GITHUB_TOKEN": "t", "FETCH_CACHE_DIR": str(cls.dir / "raw"),
                   "FETCH_SEARCH_PAUSE": "0", "SCORER_BASE_URL": llm_url, "SCORER_MODEL": "fake-llm", "SCORER_API_KEY": "local"}
        cls.code, cls.out = run_script("run_all.py", ["--no-app", "--workdir", cls.dir, "--repo", demo_repo.REPO, "--limit", "50",
                                                      "--min-age-days", "7", "--runs", "2", "--style-count", "10"],
                                       env=cls.env, timeout=300)

    @classmethod
    def tearDownClass(cls):
        cls.github.stop()
        cls.llm.stop()
        shutil.rmtree(cls.dir, True)

    def test_every_step_ran_and_the_run_is_clean(self):
        self.assertEqual(self.code, 0, self.out[-3000:])
        for name in ("prs.json", "all_prs.json", "author_history.json", "scores.json", "run_stats.json", "outcomes.json",
                     "metrics.json", "prs_style.json", "scores_style.json", "style_report.json", "validation.json", "validation.md"):
            self.assertTrue((self.dir / name).is_file(), name)
        self.assertNotIn("Traceback", self.out)

    def test_files_agree_with_each_other(self):
        rep = check_data.check_folder(self.dir, ROOT / "config" / "levels.json")
        self.assertEqual(rep.errors, [])
        prs, scores = read(self.dir / "prs.json"), read(self.dir / "scores.json")
        self.assertEqual(len(prs), 50)
        self.assertEqual({p["number"] for p in prs}, {s["number"] for s in scores})
        self.assertEqual({o["number"] for o in read(self.dir / "outcomes.json")}, {p["number"] for p in prs})
        self.assertEqual({m["author"] for m in read(self.dir / "metrics.json")}, {p["author"] for p in prs})
        self.assertEqual(sum(m["pr_count"] for m in read(self.dir / "metrics.json")), 50)

    def test_known_outcomes_of_the_synthetic_repository_are_found(self):
        data = demo_repo.generate()
        scored = {p["number"] for p in read(self.dir / "prs.json")}
        expected = set()
        for p in data["pulls"]:                       # откаты с явным «Reverts demo/shop#N» и мержем
            if p["merged_at"] and p["body"].startswith("Reverts demo/shop#"):
                target = int(p["body"].split("#")[1].split()[0])
                if target in scored:
                    expected.add(target)
        self.assertTrue(expected, "в выборке должен быть хотя бы один откат, иначе тест пуст")
        reverted = {o["number"] for o in read(self.dir / "outcomes.json") if o["reverted"]}
        self.assertTrue(expected <= reverted, (expected, reverted))
        unmerged_target = next(int(p["body"].split("#")[1]) for p in data["pulls"]
                               if not p["merged_at"] and p["title"].startswith("Revert"))
        by_number = {o["number"]: o for o in read(self.dir / "outcomes.json")}
        if unmerged_target in by_number:
            closed_revert = next(p["number"] for p in data["pulls"] if not p["merged_at"] and p["title"].startswith("Revert"))
            self.assertNotIn(closed_revert, by_number[unmerged_target]["reverted_by"])

    def test_report_is_marked_as_a_stub_run(self):
        self.assertTrue(read(self.dir / "validation.json")["synthetic"])
        self.assertIn("прогон на заглушке модели", (self.dir / "validation.md").read_text(encoding="utf-8"))

    def test_model_never_saw_an_author_login_outside_a_path(self):
        logins = set(demo_repo.AUTHORS)
        prs = {p["number"]: p for p in read(self.dir / "prs.json")}
        allowed = read(self.dir / "run_stats.json")["author_login_still_visible"]
        seen = 0
        for payload in self.fake.requests:
            for message in payload["messages"]:
                text = message["content"]
                if not text.startswith("PULL REQUEST DATA"):
                    continue
                seen += 1
                # какой это PR, по заголовку не узнать (он мог быть испорчен тестом стиля), поэтому проверяются все логины
                leaked = [login for login in logins if f"({login})" in text or f"@{login}" in text]
                self.assertEqual(leaked, [], text[:200])
        self.assertGreater(seen, 100)
        self.assertEqual([prs[n]["title"] for n in allowed], ["Add a sale discount"])    # логин в пути docs/authors/alina.md

    def test_second_launch_reuses_everything(self):
        hits = self.fake.hits
        code, out = run_script("run_all.py", ["--no-app", "--workdir", self.dir, "--runs", "2", "--style-count", "10"], env=self.env, timeout=300)
        self.assertEqual(code, 0, out[-3000:])
        self.assertEqual(self.fake.hits, hits, "повторный запуск не должен обращаться к модели")
        self.assertIn("Выгрузка пропущена", out)


class RunAllErrors(unittest.TestCase):
    def test_without_repo_and_without_prs_it_says_what_to_do(self):
        work = Path(tempfile.mkdtemp(prefix="pipeline-empty-"))
        self.addCleanup(shutil.rmtree, work, True)
        code, out = run_script("run_all.py", ["--no-app", "--workdir", work])
        self.assertEqual(code, 2, out)
        self.assertIn("нет prs.json", out)

    def test_bad_prs_file_stops_before_any_money_is_spent(self):
        work = Path(tempfile.mkdtemp(prefix="pipeline-bad-"))
        self.addCleanup(shutil.rmtree, work, True)
        (work / "prs.json").write_text('[{"number": "one"}]', encoding="utf-8")
        server, url, fake = fake_llm.start()
        self.addCleanup(server.stop)
        code, out = run_script("run_all.py", ["--no-app", "--workdir", work],
                               env={"SCORER_BASE_URL": url, "SCORER_MODEL": "fake-llm", "SCORER_API_KEY": "local"})
        self.assertEqual(code, 1, out)
        self.assertIn("Конвейер остановлен", out)
        self.assertEqual(fake.hits, 0)


if __name__ == "__main__":
    unittest.main()
