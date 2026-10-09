"""dashboard/make_data.py: что попадает в data.js и что страница одним файлом собирается целой."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PATCH, ROOT, make_pr, make_score, run_script, write


def read_data(path):
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("window.DASHBOARD_DATA = ") and text.rstrip().endswith(";")
    return json.loads(text[len("window.DASHBOARD_DATA = "):].rstrip().rstrip(";"))


class MakeData(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dash-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        files = [{"path": "src/cache.py", "patch": PATCH, "truncated": False}, {"path": "src/other.py", "patch": PATCH, "truncated": False}]
        self.prs = [make_pr(1, title='</script><script>alert(1)</script>', files=files), make_pr(2, body="x" * 9000)]
        write(self.dir / "prs.json", self.prs)
        write(self.dir / "scores.json", [make_score(1, evidence=[{"path": "src/cache.py", "lines": "10-12"}]), make_score(2)])
        self.out = self.dir / "data.js"

    def build(self, *args):
        return run_script("dashboard/make_data.py", ["--dir", self.dir, "--out", self.out] + list(args))

    def test_bundle_has_prs_scores_and_the_default_config(self):
        code, out = self.build()
        self.assertEqual(code, 0, out)
        data = read_data(self.out)
        self.assertEqual([p["number"] for p in data["prs"]], [1, 2])
        self.assertEqual(len(data["scores"]), 2)
        self.assertIn("weights", data["config"])
        self.assertNotIn("metrics", data)
        self.assertIn("нет metrics.json", out)

    def test_only_cited_diffs_are_kept_and_long_text_is_cut(self):
        self.build()
        data = read_data(self.out)
        files = {f["path"]: f["patch"] for f in data["prs"][0]["files"]}
        self.assertEqual(files, {"src/cache.py": PATCH, "src/other.py": ""})
        self.assertEqual(len(data["prs"][1]["body"]), 4000)

    def test_optional_files_are_picked_up(self):
        write(self.dir / "metrics.json", [{"author": "octocat", "multiplier": 1.0}])
        write(self.dir / "author_history.json", {"repo": "acme/shop", "merged_before": {"octocat": 12}})
        write(self.dir / "run_stats.json", {"model": "m", "per_pr": {"1": {"run_scores": {"risk": [3, 3, 4]}}}, "tokens": {"in": 1}})
        self.build()
        data = read_data(self.out)
        self.assertEqual(data["repo"], "acme/shop")
        self.assertEqual(data["metrics"][0]["author"], "octocat")
        self.assertEqual(data["run_stats"]["per_pr"]["1"]["run_scores"]["risk"], [3, 3, 4])
        self.assertNotIn("tokens", data["run_stats"])

    def test_text_from_a_pr_cannot_close_the_script_tag(self):
        single = self.dir / "one.html"
        code, out = self.build("--single", single)
        self.assertEqual(code, 0, out)
        self.assertNotIn("</script><script>alert(1)", self.out.read_text(encoding="utf-8"))
        html = single.read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert(1)", html)
        self.assertEqual(html.count("<script>"), html.count("</script>"))
        for leftover in ('href="styles.css"', 'src="data.js"', 'src="app.js"', 'src="i18n.js"'):
            self.assertNotIn(leftover, html)
        self.assertEqual(read_data(self.out)["prs"][0]["title"], '</script><script>alert(1)</script>')   # сам текст не потерян

    def test_without_scores_the_page_still_gets_the_prs(self):
        """Оценок ещё нет: дашборд получает PR и исходы и показывает экран «оценок пока нет»."""
        (self.dir / "scores.json").unlink()
        code, out = self.build()
        self.assertEqual(code, 0, out)
        data = read_data(self.out)
        self.assertEqual(data["scores"], [])
        self.assertEqual(len(data["prs"]), 2)
        self.assertTrue(all(f["patch"] == "" for pr in data["prs"] for f in pr["files"]))   # без оценок diff показывать негде

    def test_missing_prs_is_a_clear_error(self):
        (self.dir / "prs.json").unlink()
        code, out = self.build()
        self.assertNotEqual(code, 0)
        self.assertIn("нет prs.json", out)
        self.assertFalse(self.out.exists())


def language_tables():
    """Разбирает i18n.js без браузера: {язык: {ключ: текст или первая форма}}."""
    import re
    text = (ROOT / "dashboard" / "i18n.js").read_text(encoding="utf-8")
    starts = [(m.group(1), m.start()) for m in re.finditer(r"^  (ru|az|en): \{$", text, re.M)]
    pair = re.compile(r'(?:"([\w.]+)"|\b([A-Za-z_]\w*))\s*:\s*("(?:[^"\\]|\\.)*"|\[[^\]]*\])')
    tables = {}
    for i, (lang, start) in enumerate(starts):
        block = text[start:starts[i + 1][1] if i + 1 < len(starts) else len(text)]
        tables[lang] = {a or b: value for a, b, value in pair.findall(block)}
    return tables


class Languages(unittest.TestCase):
    def setUp(self):
        self.tables = language_tables()

    def test_three_languages_with_the_same_keys(self):
        self.assertEqual(sorted(self.tables), ["az", "en", "ru"])
        self.assertGreater(len(self.tables["ru"]), 250)
        for lang in ("az", "en"):
            self.assertEqual(sorted(self.tables[lang]), sorted(self.tables["ru"]), lang)

    def test_placeholders_and_markup_match_in_every_language(self):
        import re
        shape = lambda value: (sorted(re.findall(r"\{\w+\}", value)), value.count("**"), value.count("`"))
        for lang in ("az", "en"):
            for key, value in self.tables["ru"].items():
                if key in ("_months", "_locale", "_decimal", "_name", "_short") or value.startswith("["):
                    continue
                with self.subTest(lang=lang, key=key):
                    self.assertEqual(shape(self.tables[lang][key]), shape(value))

    def test_every_key_used_by_the_page_exists(self):
        import re
        source = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8") + (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        key = r"([A-Za-z0-9_.]+)"                                   # только латиница: в комментариях встречается «ключ»
        used = set(re.findall(r'\b(?:tr|tn|T)\(\s*"' + key + '"', source)) | set(re.findall('data-i18n="' + key + '"', source))
        used |= set(re.findall(r'data-i18n-attr="[\w-]+:' + key + '"', source))
        used = {k for k in used if not k.endswith(".")}            # «crit.» + имя собираются на лету
        self.assertGreater(len(used), 120)
        self.assertEqual(sorted(used - set(self.tables["ru"])), [])

    def test_azerbaijani_uses_its_own_letters_and_no_cyrillic(self):
        import re
        text = " ".join(self.tables["az"].values())
        self.assertIsNone(re.search(r"[а-яё]", text, re.I))
        for letter in "əıöüçşğ":
            self.assertIn(letter, text)
        self.assertIsNone(re.search(r"[а-яё]", " ".join(self.tables["en"].values()), re.I))


class ShippedFiles(unittest.TestCase):
    def test_page_references_only_local_files(self):
        html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "dashboard" / "styles.css").read_text(encoding="utf-8")
        js = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        texts = (ROOT / "dashboard" / "i18n.js").read_text(encoding="utf-8")
        for text in (html, css, texts):
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)        # ни шрифтов, ни библиотек из сети: демо работает офлайн
        self.assertNotIn("innerHTML", js)             # данные из файлов вставляются только как текст
        self.assertEqual(js.count("https://"), 1)     # единственный внешний адрес: ссылка на PR в github.com


if __name__ == "__main__":
    unittest.main()
