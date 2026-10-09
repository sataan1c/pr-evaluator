"""Раскладка репозитория: скрипты в корне, данные в data/. И работа без ключа модели.

Проверяется на копии образца sample/: настоящие данные проекта тесты не трогают.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from tests.helpers import ROOT, read, run_script

import app
import fake_llm


def project(with_scores: bool) -> Path:
    """Временная папка с данными в data/, как в репозитории проекта."""
    root = Path(tempfile.mkdtemp(prefix="layout-test-"))
    shutil.copytree(ROOT / "sample", root / "data")
    if not with_scores:
        for name in ("scores.json", "run_stats.json", "scores_style.json"):
            (root / "data" / name).unlink()
    return root


class ScriptsFindTheDataFolder(unittest.TestCase):
    def setUp(self):
        self.root = project(with_scores=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_scripts_launched_from_the_root_work_inside_data(self):
        for script, args, made in (("outcomes.py", [], "outcomes.json"), ("metrics.py", [], "metrics.json"),
                                   ("style_test.py", ["compare"], "style_report.json"), ("validate.py", [], "validation.json")):
            code, out = run_script(script, args, cwd=self.root)
            self.assertEqual(code, 0, f"{script}: {out[-1500:]}")
            self.assertTrue((self.root / "data" / made).is_file(), made)
            self.assertFalse((self.root / made).exists(), f"{made} не должен попасть в корень")
        self.assertTrue((self.root / "data" / "validation.md").is_file())
        code, out = run_script("check_data.py", cwd=self.root)
        self.assertEqual(code, 0, out[-1500:])
        self.assertIn("Проверка пройдена", out)

    def test_an_explicit_path_is_left_alone(self):
        code, out = run_script("outcomes.py", ["--prs", "data/prs.json", "--all-prs", "data/all_prs.json", "--out", "elsewhere.json"],
                               cwd=self.root)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "elsewhere.json").is_file())

    def test_a_folder_with_prs_beside_the_scripts_is_not_redirected(self):
        (self.root / "data" / "data").mkdir()                     # вложенная data/ не должна сбить с толку
        code, out = run_script("outcomes.py", cwd=self.root / "data")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "data" / "outcomes.json").is_file())

    def test_mock_scores_never_land_in_the_data_folder(self):
        real = (self.root / "data" / "scores.json").read_bytes()
        code, out = run_script("mock_scores.py", cwd=self.root)
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / "data" / "scores.json").read_bytes(), real)
        self.assertEqual(read(self.root / "mock_out" / "run_stats.json")["model"], "fake-mock")
        self.assertEqual(len(read(self.root / "mock_out" / "scores.json")), len(read(self.root / "data" / "prs.json")))
        self.assertTrue((self.root / "mock_out" / "all_prs.json").is_file())

    def test_mock_scores_do_not_fill_an_empty_data_folder_either(self):
        root = project(with_scores=False)
        self.addCleanup(shutil.rmtree, root, True)
        code, out = run_script("mock_scores.py", cwd=root)
        self.assertEqual(code, 0, out)
        self.assertFalse((root / "data" / "scores.json").exists())


class RunAllWithoutAKey(unittest.TestCase):
    def test_no_scores_and_no_key_does_what_it_can_and_says_what_is_missing(self):
        root = project(with_scores=False)
        self.addCleanup(shutil.rmtree, root, True)
        code, out = run_script("run_all.py", ["--no-app", "--workdir", root / "data"], timeout=120)
        self.assertEqual(code, 1, out[-2000:])
        self.assertIn("ключ модели не настроен", out)
        self.assertIn("python score.py", out)
        self.assertTrue((root / "data" / "outcomes.json").is_file())
        for name in ("scores.json", "metrics.json", "validation.json"):
            self.assertFalse((root / "data" / name).exists(), name)
        self.assertNotIn("Traceback", out)

    def test_ready_scores_need_neither_key_nor_model(self):
        """Путь организаторов: оценки уже лежат в папке, ключа нет, расчёты проходят целиком."""
        root = project(with_scores=True)
        self.addCleanup(shutil.rmtree, root, True)
        code, out = run_script("run_all.py", ["--no-app", "--workdir", root / "data"], timeout=180)
        self.assertEqual(code, 0, out[-2500:])
        self.assertIn("оценки уже есть для всех 70 PR", out)
        for name in ("outcomes.json", "metrics.json", "style_report.json", "validation.json", "validation.md"):
            self.assertTrue((root / "data" / name).is_file(), name)

    def test_a_key_in_env_file_is_noticed_and_only_missing_prs_are_scored(self):
        root = project(with_scores=True)
        self.addCleanup(shutil.rmtree, root, True)
        data = root / "data"
        scores = read(data / "scores.json")
        (data / "scores.json").write_text(json.dumps(scores[:60]), encoding="utf-8")       # десять PR не оценены
        server, url, fake = fake_llm.start()
        self.addCleanup(server.stop)
        (data / ".env").write_text(f"SCORER_BASE_URL={url}\nSCORER_MODEL=fake-llm\nSCORER_API_KEY=local\n", encoding="utf-8")
        code, out = run_script("run_all.py", ["--no-app", "--no-style", "--runs", "1", "--workdir", data], timeout=180)
        self.assertIn(code, (0, 1), out[-2500:])
        self.assertGreater(fake.hits, 0, "при настроенном ключе неоценённые PR должны уйти в модель")
        self.assertEqual(len(read(data / "scores.json")), 70)


class DashboardBeforeScoring(unittest.TestCase):
    def test_server_gives_prs_and_outcomes_without_scores(self):
        root = project(with_scores=False)
        self.addCleanup(shutil.rmtree, root, True)
        self.assertEqual(run_script("outcomes.py", cwd=root)[0], 0)
        server = app.start_server(root / "data", 0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/data.js", timeout=10) as r:
            text = r.read().decode("utf-8")
        data = json.loads(text.split("window.DASHBOARD_DATA = ", 1)[1].split(";\nwindow.DASHBOARD_API", 1)[0])
        self.assertEqual(len(data["prs"]), 70)
        self.assertEqual(data["scores"], [])
        self.assertEqual(len(data["outcomes"]), 70)

    def test_default_command_finds_the_data_folder(self):
        root = project(with_scores=False)
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(root)
        self.assertEqual(app.data_folder().resolve(), (root / "data").resolve())


if __name__ == "__main__":
    unittest.main()
