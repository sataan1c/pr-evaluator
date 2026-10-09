"""app.py: одно приложение. Сервер дашборда, свежие данные, заметки в файле, подсказки без данных."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.helpers import HAVE_REQUESTS, ROOT, make_pr, make_score, read, run_script, write


def data_from(script_text):
    body = script_text.split("window.DASHBOARD_API", 1)[0].strip()
    return json.loads(body[len("window.DASHBOARD_DATA = "):].rstrip(";"))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="app-test-"))
        write(cls.dir / "prs.json", [make_pr(1, title="First change"), make_pr(2, title="Second change")])
        write(cls.dir / "scores.json", [make_score(1), make_score(2)])
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "serve", "--dir", str(cls.dir), "--port", "0", "--no-browser"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(__import__("os").environ, PYTHONUTF8="1"))
        first = cls.proc.stdout.readline().decode("utf-8", "replace")
        match = re.search(r"http://127\.0\.0\.1:(\d+)", first)
        assert match, f"сервер не напечатал адрес: {first!r}"
        cls.url, cls.port = match.group(0), int(match.group(1))
        cls.token = re.search(r'"token": "([^"]+)"', cls.get("/data.js")[1]).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        shutil.rmtree(cls.dir, True)

    @classmethod
    def get(cls, path, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(cls.url + path, headers=headers or {}), timeout=10) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), dict(e.headers)

    def post(self, body, token=None, headers=None, raw=None):
        head = {"Content-Type": "application/json"}
        if token:
            head["X-Dashboard-Token"] = token
        head.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + "/api/note", data=data, headers=head, method="POST"), timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_page_and_its_files_are_served(self):
        status, html, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('<script src="app.js"></script>', html)
        for path, marker in (("/styles.css", "--surface"), ("/app.js", "DASHBOARD_DATA"), ("/i18n.js", "window.I18N")):
            status, text, headers = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(marker, text)
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_nothing_outside_the_dashboard_is_served(self):
        for path in ("/app.py", "/../app.py", "/dashboard/app.js", "/prs.json", "/notes.json", "/.env", "/%2e%2e/score.py"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_data_is_read_from_disk_on_every_request(self):
        data = data_from(self.get("/data.js")[1])
        self.assertEqual([p["title"] for p in data["prs"]], ["First change", "Second change"])
        write(self.dir / "prs.json", [make_pr(1, title="Renamed"), make_pr(2, title="Second change")])
        try:
            self.assertEqual(data_from(self.get("/data.js")[1])["prs"][0]["title"], "Renamed")
        finally:
            write(self.dir / "prs.json", [make_pr(1, title="First change"), make_pr(2, title="Second change")])

    def test_note_is_saved_to_a_file_and_comes_back_with_the_data(self):
        self.assertEqual(self.post({"number": 2, "text": "три дня искали причину"}, self.token), 200)
        self.assertEqual(read(self.dir / "notes.json")["2"]["text"], "три дня искали причину")
        self.assertEqual(data_from(self.get("/data.js")[1])["notes"], {"2": "три дня искали причину"})
        self.assertEqual(self.post({"number": 2, "text": "  "}, self.token), 200)          # пустой текст убирает заметку
        self.assertEqual(read(self.dir / "notes.json"), {})

    def test_note_requests_without_the_page_token_are_refused(self):
        before = (self.dir / "notes.json").read_text(encoding="utf-8") if (self.dir / "notes.json").exists() else None
        self.assertEqual(self.post({"number": 1, "text": "x"}), 403)
        self.assertEqual(self.post({"number": 1, "text": "x"}, "wrong-token"), 403)
        after = (self.dir / "notes.json").read_text(encoding="utf-8") if (self.dir / "notes.json").exists() else None
        self.assertEqual(before, after)

    def test_bad_note_requests_are_refused(self):
        self.assertEqual(self.post({"number": "1", "text": "x"}, self.token), 400)
        self.assertEqual(self.post({"text": "x"}, self.token), 400)
        self.assertEqual(self.post(None, self.token, raw=b"{not json"), 400)
        self.assertEqual(self.post({"number": 1, "text": "я" * 4001}, self.token), 413)
        self.assertEqual(self.post({"number": 1, "text": "x" * 200000}, self.token), 413)

    def test_requests_for_another_host_name_are_refused(self):
        self.assertEqual(self.get("/", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.get("/data.js", {"Host": f"evil.example:{self.port}"})[0], 403)
        self.assertEqual(self.post({"number": 1, "text": "x"}, self.token, {"Host": "evil.example"}), 403)
        self.assertEqual(self.get("/", {"Host": f"localhost:{self.port}"})[0], 200)

    def test_damaged_notes_file_does_not_break_the_page(self):
        (self.dir / "notes.json").write_text("{broken", encoding="utf-8")
        try:
            self.assertEqual(data_from(self.get("/data.js")[1])["notes"], {})
            self.assertEqual(self.post({"number": 1, "text": "новая"}, self.token), 200)
            self.assertEqual(read(self.dir / "notes.json")["1"]["text"], "новая")
        finally:
            (self.dir / "notes.json").unlink()


class Commands(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="app-cmd-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_without_data_it_says_how_to_start(self):
        code, out = run_script("app.py", ["--no-browser"], cwd=self.dir)
        self.assertEqual(code, 2, out)
        for fragment in ("python app.py demo", "python app.py run --repo", "python app.py serve --dir"):
            self.assertIn(fragment, out)

    def test_empty_folder_can_still_be_served_and_the_page_asks_for_files(self):
        proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "serve", "--dir", str(self.dir), "--port", "0", "--no-browser"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            url = re.search(r"http://127\.0\.0\.1:\d+", proc.stdout.readline().decode("utf-8", "replace")).group(0)
            with urllib.request.urlopen(url + "/data.js", timeout=10) as r:
                self.assertEqual(data_from(r.read().decode("utf-8")), {"notes": {}})
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            proc.stdout.close()

    def test_missing_requests_library_is_explained_without_a_traceback(self):
        blocker = self.dir / "requests.py"                      # подменяем библиотеку пустышкой, которая не импортируется
        blocker.write_text("raise ImportError('simulated: requests is not installed')", encoding="utf-8")
        code, out = run_script("fetch_prs.py", ["--repo", "a/b"], cwd=self.dir, env={"PYTHONPATH": str(self.dir)})
        self.assertNotEqual(code, 0)
        self.assertIn("pip install requests", out)
        self.assertNotIn("Traceback", out)

    @unittest.skipUnless(HAVE_REQUESTS, "нужна библиотека requests: pip install requests")
    def test_demo_runs_the_whole_pipeline_without_network(self):
        code, out = run_script("app.py", ["demo", "--no-serve", "--limit", "30", "--runs", "1"], cwd=self.dir, timeout=300)
        self.assertEqual(code, 0, out[-2500:])
        for name in ("prs.json", "scores.json", "metrics.json", "outcomes.json", "validation.json"):
            self.assertTrue((self.dir / "demo_out" / name).is_file(), name)
        self.assertEqual(run_script("app.py", ["check", "--dir", self.dir / "demo_out"])[0], 0)

    def test_check_reports_broken_data(self):
        (self.dir / "prs.json").write_text('[{"number": "one"}]', encoding="utf-8")
        code, out = run_script("app.py", ["check", "--dir", self.dir])
        self.assertEqual(code, 1, out)
        self.assertIn("Проверка НЕ пройдена", out)

    def test_run_without_repo_and_without_data_explains_itself(self):
        code, out = run_script("app.py", ["run", "--no-serve", "--workdir", self.dir])
        self.assertEqual(code, 2, out)
        self.assertIn("нет prs.json", out)


if __name__ == "__main__":
    unittest.main()
