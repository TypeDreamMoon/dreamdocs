"""Generating documentation-site pages from a plugin checkout (dreamdocs site sync).

Ported from the sites' own scripts/test_docs_sync.py and test_docs_plugin_i18n.py
when the sync moved into dreamdocs; the AI-translation half of those tests lives
in test_translate.py now. Chinese pages here are written the way dreamdocs
translate writes them, with a local stand-in for the model. No network, no git.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dreamdocs import site, translate
from dreamdocs.cache import Cache
from dreamdocs.plugin import Plugin

WORDS = {"English paragraph.": "中文段落。", "Guide": "指南", "Second paragraph.": "第二段中文。",
         "Changed paragraph.": "更新后的中文。", "Install": "安装", "Install instructions.": "安装说明。",
         "Diagnostics": "诊断", "Topic": "主题", "Details": "细节", "Home": "首页", "Next": "下一页",
         "Back": "返回", "Second": "第二篇"}


def chinese(text):
    return WORDS.get(text, "译" + text)


def translate_plugin(root, pages=None):
    """dreamdocs translate with a stand-in model: every slot gets its WORDS entry or 译 + the English."""
    plugin = Plugin.open(root)
    pages = list(pages or plugin.pages())
    plans, jobs = translate.plan(plugin, pages, Cache(enabled=False), "stand-in")
    for job in jobs.values():
        job.result = [t if m is None else chinese(t) for t, m in zip(job.slot.texts, job.masked)]
    done = {p for p in plugin.pages() if plugin.translation(p).exists()} | set(pages)
    for p in plans:
        if p.needs_write:
            translate.write_page(p, plugin, done, "stand-in")


class SiteSyncTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GITHUB_ACTIONS": "", "DOCS_TRANSLATION_PREVIEW": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()   # Windows CI hands out an 8.3 short temp path.
        self.root = base / "site"
        self.plugin = base / "plugin"
        self.sha = "a" * 40
        self.dirty = ""
        git = patch.object(site, "git", side_effect=lambda root, *args: self.dirty if args[0] == "status" else self.sha)
        git.start()
        self.addCleanup(git.stop)
        self.cfg = {"version": 1, "repository": "example/plugin", "branch": "main",
                    "pages": [{"id": "guide", "format": "md", "title": "Website title", "sources": [{"path": "Docs/guide.md"}]}]}
        self.save()
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\nEnglish paragraph.\n\n```cpp\nvoid DoThing();\n```\n")
        site.write(self.root / "content/docs/guide.mdx", "中文原有内容\n")

    def save(self):
        site.write(self.root / "docs-sync/config.json", site.json_text(self.cfg))

    def sync(self, **kw):
        return site.sync(self.root, self.plugin, **kw)

    def first_import(self):
        self.sync()
        translate_plugin(self.plugin)
        return self.sync(adopt_existing=True)

    def status(self, page="guide"):
        return {r["id"]: r["status"] for r in site.statuses(self.root)}[page]

    def lock(self):
        return site.load(self.root / "docs-sync/source-lock.json")

    def zh(self, page="guide"):
        return site.read(site.translation_path(self.root, page))

    # -- generating the English ---------------------------------------------------
    def test_sync_without_plugin_translation_keeps_legacy_chinese(self):
        self.sync()
        self.assertEqual(site.read(self.root / "content/docs/guide.mdx"), "中文原有内容\n")
        self.assertIn("English paragraph.", site.read(self.root / "content/docs/guide.en.md"))
        self.assertEqual(self.status(), "missing")

    def test_check_is_read_only_and_sync_is_idempotent(self):
        self.assertTrue(self.sync(check=True))
        self.assertFalse((self.root / "docs-sync/source-lock.json").exists())
        self.sync()
        self.assertEqual(self.sync(), [])

    def test_local_generated_edits_are_not_overwritten(self):
        self.sync()
        site.write(self.root / "content/docs/guide.en.md", "Hand edited\n")
        with self.assertRaisesRegex(ValueError, "edited locally"):
            self.sync()
        self.assertEqual(site.read(self.root / "content/docs/guide.en.md"), "Hand edited\n")

    def test_missing_source_and_case_mismatch_fail_before_any_output(self):
        self.cfg["pages"].append({"id": "missing", "sources": [{"path": "Docs/missing.md"}]})
        self.save()
        with self.assertRaises(FileNotFoundError):
            self.sync()
        self.assertFalse((self.root / "content/docs/guide.en.md").exists())
        self.cfg["pages"] = [{"id": "guide", "format": "md", "sources": [{"path": "Docs/Guide.md"}]}]
        self.save()
        with self.assertRaisesRegex(ValueError, "case mismatch"):
            self.sync()

    def test_multiple_whole_sources_share_the_public_route(self):
        site.write(self.plugin / "Docs/history.md", "# Historical diagnostic\n\nDetails.\n")
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\n[Details](history.md#historical-diagnostic)\n")
        self.cfg["pages"][0]["sources"].append({"path": "Docs/history.md"})
        self.save()
        self.sync()
        self.assertIn("[Details](/en/docs/guide#historical-diagnostic)", site.read(self.root / "content/docs/guide.en.md"))

    def test_squash_merge_with_identical_sources_can_advance_revision(self):
        self.sync()
        lock = self.lock()
        lock["revision"] = "b" * 40
        site.write(self.root / "docs-sync/source-lock.json", site.json_text(lock))
        with patch.object(site.subprocess, "run") as run:
            run.return_value.returncode = 1
            self.sync()
        self.assertEqual(self.lock()["revision"], self.sha)

    def test_unrelated_revision_with_changed_sources_is_rejected(self):
        self.sync()
        lock = self.lock()
        lock["revision"] = "b" * 40
        site.write(self.root / "docs-sync/source-lock.json", site.json_text(lock))
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\nOlder or unrelated text.\n")
        with patch.object(site.subprocess, "run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, "different documentation"):
                self.sync()

    def test_paths_routes_and_sections(self):
        for path in ["../outside.md", "/outside.md", "C:/outside.md", "..\\outside.md"]:
            with self.assertRaises(ValueError):
                site.within(self.root, path)
        site.check_route("custom-pass/index")
        for page_id in ("plugin/runtime/index", "Runtime", "a/vendor/b"):
            with self.assertRaisesRegex(ValueError, "blocks"):
                site.check_route(page_id)
        text = "# Intro\n\n## Part\n```md\n## Fake\n```\ntext\n## Next\nend\n"
        self.assertEqual(site.excerpt(text, "Part"), "## Part\n```md\n## Fake\n```\ntext\n")

    def test_new_doc_is_discovered_and_the_chinese_tree_never_is(self):
        self.first_import()
        site.write(self.plugin / "Docs/new.md", "# New\n\nNew text\n")
        self.sync()
        self.assertTrue((self.root / "content/docs/plugin/new.en.md").exists())
        self.assertFalse(any("zh-cn" in p.as_posix().lower() for p in (self.root / "content/docs").rglob("*")))

    def test_removed_source_blocks_instead_of_deleting_translation(self):
        self.sync()
        (self.plugin / "Docs/guide.md").unlink()
        with self.assertRaises(FileNotFoundError):
            self.sync()
        self.assertTrue((self.root / "content/docs/guide.mdx").exists())

    # -- importing the Chinese ------------------------------------------------------
    def test_translated_page_is_imported_current_and_titled_from_its_heading(self):
        self.first_import()
        zh = self.zh()
        self.assertEqual(self.status(), "current")
        self.assertIn('title: "指南"', zh)            # No frontmatter in the plugin: the Chinese heading.
        self.assertIn("中文段落。", zh)
        self.assertIn("# 指南 [#guide]", zh)          # The plugin's <a id> becomes the heading's ID.
        self.assertFalse((self.root / "content/docs/guide.mdx").exists())
        self.assertEqual(self.lock()["pages"]["guide"]["translation"]["status"], "current")

    def test_plugin_frontmatter_titles_the_chinese_page(self):
        site.write(self.plugin / "Docs/guide.md", "---\ntitle: Guide\ndescription: How it works\n---\n\n# Guide\n\nEnglish paragraph.\n")
        self.first_import()
        self.assertIn('title: "指南"', self.zh())
        self.assertIn('description: "译How it works"', self.zh())

    def test_changed_english_is_outdated_with_a_paragraph_count(self):
        self.first_import()
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\nChanged paragraph.\n\nAnother new one.\n")
        self.sync()
        self.assertEqual(self.status(), "outdated")
        self.assertEqual(self.lock()["pages"]["guide"]["translation"]["pending"], 2)
        self.assertIn("中文段落。", self.zh())       # The old Chinese stays up, flagged, until translated.

    def test_site_sync_never_writes_plugin_files(self):
        self.first_import()
        before = {p: p.read_bytes() for p in self.plugin.rglob("*") if p.is_file()}
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\nChanged paragraph.\n")
        before[self.plugin / "Docs/guide.md"] = (self.plugin / "Docs/guide.md").read_bytes()
        self.sync()
        self.assertEqual({p: p.read_bytes() for p in self.plugin.rglob("*") if p.is_file()}, before)

    def test_chinese_without_a_table_is_unreviewed(self):
        self.sync()
        site.write(self.plugin / "Docs/zh-CN/guide.md", "# 指南\n\n导入的旧中文。\n")
        self.sync(adopt_existing=True)
        self.assertEqual(self.status(), "unreviewed")
        self.assertIn("导入的旧中文。", self.zh())

    def test_page_written_in_chinese_first_publishes_both_and_is_current(self):
        site.write(self.plugin / "Docs/dreamdocs.json", '{"version": 2, "chineseOriginal": ["guide.md"]}')
        site.write(self.plugin / "Docs/zh-CN/guide.md", "# 指南\n\n中文原文。\n")
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\nEnglish made from the Chinese.\n")
        self.sync(adopt_existing=True)
        self.assertEqual(self.status(), "current")
        self.assertIn("中文原文。", self.zh())
        self.assertIn("English made from the Chinese.", site.read(self.root / "content/docs/guide.en.md"))

    def test_existing_site_chinese_needs_explicit_adoption_and_never_in_ci(self):
        self.sync()
        translate_plugin(self.plugin)
        with self.assertRaisesRegex(ValueError, "explicit migration"):
            self.sync()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), self.assertRaisesRegex(ValueError, "cannot adopt"):
            self.sync(adopt_existing=True)

    def test_missing_translation_without_legacy_page_does_not_publish_english_as_chinese(self):
        (self.root / "content/docs/guide.mdx").unlink()
        self.sync()
        self.assertFalse(site.translation_path(self.root, "guide").exists())

    def test_notes_survive_and_prose_edits_are_refused(self):
        notes = "\n## 我的网站补充\n\n保持这个说明。\n"
        site.write(self.root / "content/docs/guide.mdx", "正文\n" + site.NOTES + notes)
        self.first_import()
        self.assertTrue(self.zh().endswith(site.NOTES + notes))
        path = site.translation_path(self.root, "guide")
        path.write_text(self.zh().replace("中文段落。", "网站上改的。"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "edited locally"):
            self.sync()
        path.write_text(self.zh().replace("网站上改的。", "中文段落。").replace("保持这个说明。", "说明改了。"), encoding="utf-8")
        self.sync()
        self.assertIn("说明改了。", self.zh())

    def test_links_between_translated_pages_use_chinese_routes(self):
        self.cfg["pages"] += [{"id": "topic/index", "format": "md", "sources": [{"path": "Docs/topic.md"}]},
                              {"id": "index", "format": "md", "sources": [{"path": "Docs/index.md"}]}]
        self.save()
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\n[Next](topic.md?view=full#details)\n\n[Home](index.md)\n")
        site.write(self.plugin / "Docs/topic.md", "# Topic\n\n## Details\n\n[Back](guide.md#guide)\n")
        site.write(self.plugin / "Docs/index.md", "# Home\n\n[Guide](guide.md)\n")
        self.sync()
        english = site.read(self.root / "content/docs/guide.en.md")
        translate_plugin(self.plugin)
        self.sync(adopt_existing=True)
        guide = self.zh()
        self.assertIn("译[Next](/docs/topic?view=full#details)", guide)
        self.assertIn("译[Home](/docs/)", guide)
        self.assertIn("译[Back](/docs/guide#guide)", self.zh("topic/index"))
        self.assertEqual(site.read(self.root / "content/docs/guide.en.md"), english)
        record = site.load(self.root / "docs-sync/translations/guide.json")
        self.assertEqual(record["translationHash"], site.digest(site.read(self.root / record["translationFile"])))

    def test_shared_source_is_translated_once_and_keeps_both_routes(self):
        self.cfg["pages"].append({"id": "also", "format": "md", "sources": [{"path": "Docs/guide.md"}]})
        self.save()
        self.first_import()
        self.assertIn("中文段落。", self.zh("also"))
        self.assertEqual(self.status("also"), "current")

    def test_multisource_page_waits_for_every_part(self):
        site.write(self.plugin / "Docs/second.md", "# Second\n\nSecond paragraph.\n")
        self.cfg["pages"][0]["sources"].append({"path": "Docs/second.md"})
        self.save()
        self.sync()
        translate_plugin(self.plugin, ["guide.md"])
        self.sync(adopt_existing=True)
        self.assertEqual(self.zh(), "中文原有内容\n")        # Half a page never replaces a whole one.
        translate_plugin(self.plugin, ["second.md"])
        self.sync(adopt_existing=True)
        self.assertIn("第二段中文。", self.zh())
        self.assertEqual(self.status(), "current")

    def test_heading_excerpts_select_by_anchor_not_translated_title(self):
        site.write(self.plugin / "Docs/guide.md", "# Guide\n\n## Install\n\nInstall instructions.\n\n## Other\n\nElse.\n")
        self.cfg["pages"] = [{"id": "install", "format": "md", "title": "Install",
                              "sources": [{"path": "Docs/guide.md", "heading": "Install"}]}]
        self.save()
        (self.root / "content/docs/guide.mdx").unlink()
        self.first_import()
        zh = self.zh("install")
        self.assertIn("安装说明。", zh)
        self.assertNotIn("译Else.", zh)

    def test_diagnostic_groups_select_translated_codes(self):
        site.write(self.plugin / "Docs/codes.md", "# Codes\n\n| Code | Name | Meaning |\n|---|---|---|\n"
                                                  "| DUI1001 | `First` | Only the first group. |\n"
                                                  "| DUI2001 | `Second` | Only the second group. |\n")
        self.cfg["pages"] = [{"id": "diagnostics/dui1xxx", "format": "md", "title": "DUI1xxx",
                              "sources": [{"path": "Docs/codes.md", "diagnosticPrefix": "DUI1"}]}]
        self.save()
        (self.root / "content/docs/guide.mdx").unlink()
        self.first_import()
        zh = self.zh("diagnostics/dui1xxx")
        self.assertIn("## DUI1001", zh)
        self.assertIn("译Only the first group.", zh)
        self.assertNotIn("DUI2001", zh)

    def test_late_ancestor_cannot_roll_chinese_back(self):
        self.first_import()
        lock = self.lock()
        lock["revision"] = "c" * 40
        site.write(self.root / "docs-sync/source-lock.json", site.json_text(lock))
        with patch.object(site.subprocess, "run") as run:
            run.return_value.returncode = 0   # The requested commit is an ancestor of the published one.
            run.side_effect = lambda args, **kw: type("R", (), {"returncode": 1 if args[-2] == "c" * 40 else 0})()
            with self.assertRaisesRegex(ValueError, "older plugin revision"):
                self.sync()

    def test_ci_rejects_dirty_plugin_documents(self):
        self.sync()
        self.dirty = " M Docs/guide.md"
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), self.assertRaisesRegex(ValueError, "committed"):
            self.sync()

    def test_localized_links_check_pages_and_anchors(self):
        site.write(self.root / "content/docs/existing/index.mdx", "# 中文页面\n\n## 中文标题 [#stable]\n")
        guide = self.root / "content/docs/guide.md"
        planned = {guide: "[A](/en/docs/existing#stable) [B](/en/docs/existing#absent) [C](/en/docs/missing)\n"}
        site.localize_translation_links(self.root, planned, [])
        self.assertEqual(planned[guide], "[A](/docs/existing#stable) [B](/en/docs/existing#absent) [C](/en/docs/missing)\n")

    def test_site_options_strip_the_leading_title_and_prefer_plugin_titles(self):
        site.write(self.plugin / "Docs/guide.md", "---\ntitle: Plugin title\n---\n\n# Guide\n\nEnglish paragraph.\n")
        self.sync()
        english = site.read(self.root / "content/docs/guide.en.md")
        self.assertIn('title: "Website title"', english)
        self.assertIn("# Guide", english)
        self.cfg.update(stripLeadingTitle=True, titleSource="plugin")
        self.save()
        self.sync()
        english = site.read(self.root / "content/docs/guide.en.md")
        self.assertIn('title: "Plugin title"', english)
        self.assertNotIn("# Guide", english)

    def test_links_to_a_stripped_title_go_to_the_page(self):
        self.cfg["pages"].append({"id": "other", "format": "md", "title": "Other", "sources": [{"path": "Docs/other.md"}]})
        self.cfg.update(stripLeadingTitle=True)
        self.save()
        site.write(self.plugin / "Docs/guide.md", "# Guide — `.dsg`\n\nEnglish paragraph.\n\n## Setup\n\nText.\n")
        site.write(self.plugin / "Docs/other.md", "# Other\n\nSee [the guide](guide.md#guide--dsg) and "
                                                  "[setup](guide.md#setup).\n")
        self.sync()
        other = site.read(self.root / "content/docs/other.en.md")
        self.assertIn("[the guide](/en/docs/guide)", other)
        self.assertIn("[setup](/en/docs/guide#setup)", other)
        translate_plugin(self.plugin)
        self.sync(adopt_existing=True)
        self.assertNotIn("# 译Guide", self.zh())
        self.assertTrue(self.zh().split("---\n", 2)[2].lstrip().startswith("中文段落。"))
        other = self.zh("other")
        self.assertIn("](/docs/guide)", other)
        self.assertIn("](/docs/guide#setup)", other)

    def test_validate_catches_edited_output(self):
        self.first_import()
        self.assertEqual(site.validate(self.root), 1)
        site.write(self.root / "content/docs/guide.en.md", "changed\n")
        with self.assertRaisesRegex(ValueError, "differs from lock"):
            site.validate(self.root)


if __name__ == "__main__":
    unittest.main()
