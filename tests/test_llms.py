"""llms.txt, llms-full.txt and published plugin files (dreamdocs/llms.py), written by site sync."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dreamdocs import site


class LlmsTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GITHUB_ACTIONS": "", "DOCS_TRANSLATION_PREVIEW": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = base / "site"
        self.plugin = base / "plugin"
        self.sha = "b" * 40
        git = patch.object(site, "git", side_effect=lambda root, *args: "" if args[0] == "status" else self.sha)
        git.start()
        self.addCleanup(git.stop)
        self.cfg = {
            "version": 1, "repository": "example/plugin", "branch": "main", "siteUrl": "https://docs.example.com",
            "stripLeadingTitle": True,
            "pages": [
                {"id": "cheatsheet", "format": "md", "title": "Cheat sheet", "sources": [{"path": "Docs/cheatsheet.md"}]},
                {"id": "language/index", "format": "md", "title": "Language", "sources": [{"path": "Docs/language.md"}]},
                {"id": "diagnostics/e1xxx", "format": "md", "title": "E1xxx", "sources": [{"path": "Docs/diagnostics/E1xxx.md"}]},
            ],
            "llms": {
                "title": "Example",
                "summary": "Example turns text into assets.",
                "details": ["Read the cheat sheet first."],
                "sections": [
                    {"title": "Start here", "links": [
                        {"page": "cheatsheet", "note": "the language on one page"},
                        {"file": "diagnostics.json", "title": "Diagnostics as JSON"},
                    ]},
                    {"title": "Optional", "links": [{"page": "language/index"}]},
                ],
                "full": ["cheatsheet", {"source": ".skill/reference/ref.md", "title": "Reference"}],
                "publish": {"diagnostics.json": "Docs/diagnostics/codes.json"},
            },
        }
        self.save()
        site.write(self.plugin / "Docs/cheatsheet.md",
                   "# Cheat sheet\n\nSee [the language](language.md) and [E1001](diagnostics/E1xxx.md#e1001).\n\n"
                   "```cpp\n[not a link](language.md)\n```\n")
        site.write(self.plugin / "Docs/language.md", "# Language\n\nEverything.\n")
        site.write(self.plugin / "Docs/diagnostics/E1xxx.md", "# E1xxx\n\n## E1001\n\nA code.\n")
        site.write(self.plugin / ".skill/reference/ref.md", "# Ref\n\nThe [cheat sheet](../../Docs/cheatsheet.md).\n")
        self.codes({"codes": [{"code": "E1001", "doc": "https://docs.example.com/en/docs/diagnostics/e1xxx/#e1001"}]})

    def save(self):
        site.write(self.root / "docs-sync/config.json", site.json_text(self.cfg))

    def codes(self, value):
        site.write(self.plugin / "Docs/diagnostics/codes.json", json.dumps(value, indent=2) + "\n")

    def sync(self, **kw):
        return site.sync(self.root, self.plugin, **kw)

    def test_index_follows_the_llmstxt_shape(self):
        self.sync()
        text = site.read(self.root / "public/llms.txt")
        self.assertTrue(text.startswith("# Example\n\n> Example turns text into assets.\n\nRead the cheat sheet first.\n"))
        self.assertIn("## Start here\n\n- [Cheat sheet](https://docs.example.com/en/docs/cheatsheet/): the language on one page\n", text)
        self.assertIn("- [Diagnostics as JSON](https://docs.example.com/diagnostics.json)\n", text)
        self.assertIn("## Optional\n\n- [Language](https://docs.example.com/en/docs/language/)\n", text)

    def test_full_text_has_absolute_links_and_leaves_code_alone(self):
        self.sync()
        full = site.read(self.root / "public/llms-full.txt")
        self.assertIn("# Cheat sheet\n\nSource: https://docs.example.com/en/docs/cheatsheet/\n", full)
        self.assertIn("[the language](https://docs.example.com/en/docs/language)", full)
        self.assertIn("[E1001](https://docs.example.com/en/docs/diagnostics/e1xxx#e1001)", full)
        self.assertIn("```cpp\n[not a link](language.md)\n```", full)
        # A plugin file that is not a page: its links still lead to the site.
        self.assertIn("# Reference\n\nSource: https://github.com/example/plugin/blob/" + self.sha + "/.skill/reference/ref.md", full)
        self.assertIn("The [cheat sheet](https://docs.example.com/en/docs/cheatsheet).", full)
        self.assertNotIn("# Language", full)  # Optional pages are not in the full text unless listed

    def test_published_file_is_copied_and_locked(self):
        self.sync()
        self.assertEqual((self.root / "public/diagnostics.json").read_bytes(),
                         (self.plugin / "Docs/diagnostics/codes.json").read_bytes())
        lock = site.load(self.root / "docs-sync/source-lock.json")
        self.assertEqual(sorted(lock["published"]), ["public/diagnostics.json", "public/llms-full.txt", "public/llms.txt"])
        self.assertEqual(site.validate(self.root), 3)

    def test_check_writes_nothing_and_sync_is_idempotent(self):
        changes = self.sync(check=True)
        self.assertIn(str(Path("public/llms.txt")), changes)
        self.assertFalse((self.root / "public").exists())
        self.sync()
        self.assertEqual(self.sync(check=True), [])

    def test_validate_catches_an_edited_published_file(self):
        self.sync()
        (self.root / "public/llms.txt").write_text("# edited\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs from lock: public/llms.txt"):
            site.validate(self.root)

    def test_a_doc_link_to_a_route_the_site_lacks_is_refused(self):
        self.codes({"codes": [{"code": "E1001", "doc": "https://docs.example.com/en/docs/plugin/diagnostics/e1xxx/#e1001"}]})
        with self.assertRaisesRegex(ValueError, "not a page of this site"):
            self.sync()

    def test_unknown_page_and_missing_site_url_are_errors(self):
        self.cfg["llms"]["sections"][0]["links"].append({"page": "nope"})
        self.save()
        with self.assertRaisesRegex(ValueError, "no page 'nope'"):
            self.sync()
        self.cfg["llms"]["sections"][0]["links"].pop()
        del self.cfg["siteUrl"]
        self.save()
        with self.assertRaisesRegex(ValueError, "siteUrl"):
            self.sync()

    def test_dropping_llms_removes_its_files(self):
        self.sync()
        del self.cfg["llms"]
        self.save()
        self.sync()
        self.assertFalse((self.root / "public/llms.txt").exists())
        self.assertFalse((self.root / "public/diagnostics.json").exists())
        self.assertNotIn("published", site.load(self.root / "docs-sync/source-lock.json"))

    def test_a_site_without_llms_is_unchanged(self):
        del self.cfg["llms"]
        self.save()
        self.sync()
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
