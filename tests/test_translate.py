import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider import FakeProvider  # noqa: E402

from dreamdocs import translate  # noqa: E402
from dreamdocs.align import Alignment  # noqa: E402
from dreamdocs.cache import Cache  # noqa: E402
from dreamdocs.plugin import Plugin  # noqa: E402
from dreamdocs.profiles import Profile, env_profile, parse_profiles  # noqa: E402
from dreamdocs.providers import Client  # noqa: E402
from dreamdocs.status import page_status  # noqa: E402

PAGE_A = """# Types

Use `float3` for colour. See the [guide](guide/b.md#setup).

## Vectors

```hlsl
float3 c = 1;
```

| Name | Use |
|---|---|
| `float3` | colour |
| `a` | `b` |

Shared paragraph about DreamShader.
"""

PAGE_B = """# Guide

## Setup

Read [types](../types.md) and [the other page](../other.md). ![logo](../img.png)

Shared paragraph about DreamShader.
"""


def quiet(*_):
    pass


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Plugin"
        (self.root / "Docs/guide").mkdir(parents=True)
        (self.root / "Docs/types.md").write_text(PAGE_A, encoding="utf-8")
        (self.root / "Docs/guide/b.md").write_text(PAGE_B, encoding="utf-8")
        (self.root / "Docs/other.md").write_text("# Other\n\nNot translated here.\n", encoding="utf-8")
        self.server = FakeProvider()
        self.cache = Cache(Path(self.tmp.name) / "cache")
        self.profile = Profile("fake", "OpenAI", self.server.url, "fake-model", api_key="sk-test-123", concurrency=1)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def go(self, pages=("types.md", "guide/b.md"), profile=None, **kw):
        profile = profile or self.profile
        plugin = Plugin.open(self.root)
        plans, jobs = translate.plan(plugin, list(pages), self.cache, profile.model)
        client = Client(profile, self.cache, models_url=self.server.url + "/models")
        return translate.run(plugin, profile, plans, jobs, client, self.cache, log=quiet, **kw), plugin

    def zh(self, page):
        return (self.root / "Docs/zh-CN" / page).read_text(encoding="utf-8")

    def test_translates_writes_page_and_table_and_status_is_current(self):
        summary, plugin = self.go()
        self.assertEqual(sorted(summary.written), ["guide/b.md", "types.md"])
        a, b = self.zh("types.md"), self.zh("guide/b.md")
        self.assertIn('<a id="types"></a>\n\n# 译Types', a)
        self.assertIn("译Use `float3` for colour. See the [guide](guide/b.md#setup).", a)
        self.assertIn("```hlsl\nfloat3 c = 1;\n```", a)
        self.assertIn("| `float3` | 译colour |", a)
        self.assertIn("| `a` | `b` |", a)  # Nothing to translate in that row: copied as is.
        self.assertIn("[types](../types.md)", b)                 # Translated in this run: the Chinese copy.
        self.assertIn("[the other page](../../other.md)", b)     # Not translated: back to the English.
        self.assertIn("![logo](../../img.png)", b)
        for page in ("types.md", "guide/b.md"):
            self.assertEqual(page_status(plugin, page).state, "current")
        headers = self.server.requests[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-test-123")
        self.assertTrue(headers["User-Agent"].startswith("dreamdocs/"))

    def test_identical_english_is_sent_once(self):
        self.go()
        texts = [s.get("text") for s in self.server.sent_slots()]
        self.assertEqual(texts.count("Shared paragraph about DreamShader."), 1)

    def test_only_changed_slots_are_sent_and_hand_edits_survive(self):
        self.go()
        zh = self.zh("types.md").replace("译colour", "颜色（人工）")
        (self.root / "Docs/zh-CN/types.md").write_text(zh, encoding="utf-8")
        english = PAGE_A.replace("## Vectors", "## Vector types")
        (self.root / "Docs/types.md").write_text(english, encoding="utf-8")
        self.server.requests.clear()
        summary, plugin = self.go(pages=["types.md"])
        sent = self.server.sent_slots()
        self.assertEqual([s["text"] for s in sent], ["Vector types"])
        self.assertEqual(sent[0]["previous_translation"], "译Vectors")
        zh = self.zh("types.md")
        self.assertIn("## 译Vector types", zh)
        self.assertIn("颜色（人工）", zh)
        rows = Alignment.read(plugin.alignment("types.md")).rows
        self.assertIn("h", [r.origin for r in rows])
        self.assertEqual(page_status(plugin, "types.md").state, "current")

    def test_layout_change_rebuilds_without_sending(self):
        self.go()
        (self.root / "Docs/types.md").write_text(PAGE_A.replace("float3 c = 1;", "float3 c = 2;"), encoding="utf-8")
        plugin = Plugin.open(self.root)
        self.assertEqual(page_status(plugin, "types.md").state, "outdated")
        self.server.requests.clear()
        self.go(pages=["types.md"])
        self.assertEqual(self.server.requests, [])
        self.assertIn("float3 c = 2;", self.zh("types.md"))

    def test_structure_changed_by_hand_is_never_overwritten(self):
        self.go()
        path = self.root / "Docs/zh-CN/types.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n多写的一段。\n", encoding="utf-8")
        (self.root / "Docs/types.md").write_text(PAGE_A + "\nNew English paragraph.\n", encoding="utf-8")
        summary, _ = self.go(pages=["types.md"])
        self.assertEqual(summary.written, [])
        self.assertTrue(summary.skipped and "no longer matches" in summary.skipped[0][1])
        self.assertIn("多写的一段。", path.read_text(encoding="utf-8"))

    def test_broken_placeholder_is_asked_again_alone(self):
        self.server.faults = [("drop",)]
        summary, _ = self.go(pages=["types.md"])
        self.assertEqual(summary.written, ["types.md"])
        retried = [s for s in self.server.sent_slots() if s.get("problem")]
        self.assertTrue(retried and "placeholders wrong" in retried[0]["problem"])
        self.assertIn("`float3`", self.zh("types.md"))

    def test_chatter_around_the_json_is_tolerated(self):
        self.server.faults = [("chatter",)]
        summary, _ = self.go(pages=["types.md"])
        self.assertEqual(summary.written, ["types.md"])

    def test_busy_waits_and_truncation_retries(self):
        self.server.faults = [("status", 429, {"Retry-After": "0"}), ("truncate",)]
        summary, _ = self.go(pages=["types.md"])
        self.assertEqual(summary.written, ["types.md"])
        self.assertGreaterEqual(len(self.server.requests), 3)

    def test_refusal_stops_the_run_and_the_cache_resumes_it(self):
        self.profile.chunk_chars = 30   # One slot per request.
        self.server.faults = [("ok",), ("ok",), ("status", 402)]
        summary, _ = self.go(pages=["types.md"])
        self.assertIn("402", summary.stopped)
        self.assertEqual(summary.written, [])
        self.assertFalse((self.root / "Docs/zh-CN/types.md").exists())
        before = len(self.server.requests)
        summary, _ = self.go(pages=["types.md"])
        self.assertEqual(summary.written, ["types.md"])
        self.assertEqual(summary.cached, 2)
        resent = [s["text"] for s in self.server.sent_slots()[before - 1:] if "text" in s]
        self.assertNotIn("Types", resent)

    def test_secrets_never_show_in_errors(self):
        self.server.faults = [("status", 401)]
        summary, _ = self.go(pages=["types.md"])
        self.assertNotIn("sk-test-123", summary.stopped)

    def test_responses_and_messages_formats(self):
        for profile, path in ((Profile("r", "OpenAI", self.server.url, "m1", api_key="k", api="responses"), "/responses"),
                              (Profile("a", "Anthropic", self.server.url, "m2", api_key="k"), "/messages")):
            self.server.requests.clear()
            for page in ("types.md",):
                (self.root / "Docs/zh-CN" / page).unlink(missing_ok=True)
                (self.root / "Docs/zh-CN/.align" / (page + ".tsv")).unlink(missing_ok=True)
            summary, _ = self.go(pages=["types.md"], profile=profile)
            self.assertEqual(summary.written, ["types.md"], profile.name)
            self.assertTrue(self.server.requests[0]["path"].endswith(path))
        self.assertEqual(self.server.requests[0]["headers"]["X-Api-Key"], "k")

    def test_opencode_go_routes_by_model_list_and_sends_its_headers(self):
        self.server.models = [
            {"id": "chatty", "api": {"chat-completions": {"base_url": self.server.url + "/inference/go/openai/v1"}},
             "cost": {"text": {"input": {"price": 0.15}, "output": {"price": 0.6}}}},
            {"id": "claude-ish", "api": {"messages": {"base_url": self.server.url + "/inference/go/anthropic/v1"}}},
        ]
        profile = Profile("ocgo", "OpenCodeGo", model="claude-ish", api_key="go-key")
        summary, _ = self.go(pages=["types.md"], profile=profile)
        self.assertEqual(summary.written, ["types.md"])
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/inference/go/anthropic/v1/messages")
        self.assertTrue(request["headers"]["X-Opencode-Session"].startswith("dreamdocs-"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer go-key")
        client = Client(Profile("ocgo", "OpenCodeGo", model="chatty", api_key="k"), Cache(Path(self.tmp.name) / "c2"),
                        models_url=self.server.url + "/models")
        self.assertEqual((client.endpoint().api, client.endpoint().price), ("chat", (0.15, 0.6)))

    def test_export_and_answers_translate_without_a_model(self):
        plugin = Plugin.open(self.root)
        plans, jobs = translate.plan(plugin, ["guide/b.md"], self.cache, "")
        path = Path(self.tmp.name) / "pending.json"
        count = translate.export_jobs(jobs, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(count, len(data["slots"]))
        for entry in data["slots"]:
            entry["answer"] = "答" + entry["text"]
        broken = next(e for e in data["slots"] if "⟦1⟧" in e["text"])   # One answer loses its placeholders.
        broken["answer"] = "没有占位符"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        accepted, problems = translate.apply_answers(jobs, path)
        self.assertEqual((accepted, len(problems)), (count - 1, 1))
        self.assertIn("placeholders wrong", problems[0])
        broken["answer"] = "答" + broken["text"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        plans, jobs = translate.plan(plugin, ["guide/b.md"], self.cache, "")
        translate.apply_answers(jobs, path)
        summary = translate.run(plugin, None, plans, jobs, None, self.cache, log=quiet)
        self.assertEqual(summary.written, ["guide/b.md"])
        self.assertEqual(self.server.requests, [])
        self.assertEqual({r.origin for r in Alignment.read(plugin.alignment("guide/b.md")).rows} - {"s"}, {"h"})

    def test_offline_run_writes_only_complete_pages(self):
        plugin = Plugin.open(self.root)
        plans, jobs = translate.plan(plugin, ["types.md"], self.cache, "")
        summary = translate.run(plugin, None, plans, jobs, None, self.cache, log=quiet)
        self.assertEqual((summary.written, summary.unfinished[0][0]), ([], "types.md"))
        self.assertEqual(self.server.requests, [])

    def test_plugin_check(self):
        from dreamdocs import check
        self.go()
        report = check.check_plugin(self.root)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("other.md: missing" in w for w in report.warnings))
        (self.root / "Docs/zh-CN/orphan.md").write_text("# 孤儿\n", encoding="utf-8")
        (self.root / "Docs/zh-CN/guide/b.md").write_text(self.zh("guide/b.md") + "\n多的一段。\n", encoding="utf-8")
        (self.root / "Docs/types.md").write_text(PAGE_A.replace("for colour", "for colours"), encoding="utf-8")
        report = check.check_plugin(self.root, strict=True)
        joined = "\n".join(report.errors)
        self.assertIn("orphan.md: a translation of no English page", joined)
        self.assertIn("guide/b.md: no longer matches its alignment table", joined)
        self.assertIn("types.md: outdated, 1 paragraphs to translate", joined)

    def test_cut_off_replies_are_retried_and_extra_body_cannot_replace_the_model(self):
        import http.client
        from dreamdocs.providers import Busy, Stop

        class Cut:
            def open(self, request, timeout):
                raise http.client.IncompleteRead(b"")
        client = Client(self.profile, self.cache, opener=Cut())
        with self.assertRaises(Busy):
            client.complete("s", "u")
        self.profile.extra_body = {"model": "other"}
        with self.assertRaisesRegex(Stop, "not replace model"):
            Client(self.profile, self.cache).complete("s", "u")

    def test_estimate_sends_nothing(self):
        plugin = Plugin.open(self.root)
        plans, jobs = translate.plan(plugin, ["types.md", "guide/b.md"], self.cache, "fake-model")
        est = translate.estimate(plans, jobs, self.profile, None, translate.system_prompt(plugin))
        self.assertEqual((est.pages, est.reused), (2, 0))
        self.assertGreater(est.jobs, 5)
        self.assertEqual(self.server.requests, [])


class ProfileTests(unittest.TestCase):
    def test_dreamloc_file_with_dreamdocs_keys(self):
        text = ("; personal\n[Profile ocgo]\nType=OpenCodeGo\nModel=deepseek-v4.1-flash ; cheap\nApiKeyEnv=OCGO\n"
                "Concurrency=3\nExtraBody={\"thinking\": {\"type\": \"disabled\"}}\n\n[Profile deepl]\nType=DeepL\n"
                "ApiKeyEnv=X\nRegion=eu\n[Profile bad]\nTokenField=nope\nWhatever=1\n")
        profiles, warnings = parse_profiles(text)
        ocgo = profiles["ocgo"]
        self.assertEqual((ocgo.type, ocgo.model, ocgo.concurrency), ("OpenCodeGo", "deepseek-v4.1-flash", 3))
        self.assertEqual(ocgo.extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(profiles["deepl"].type, "DeepL")
        self.assertEqual(len(warnings), 2)

    def test_env_profile_for_ci(self):
        saved = dict(os.environ)
        try:
            os.environ.update({"DOCS_AI_MODEL": "m", "DOCS_AI_BASE_URL": "https://x/v1/", "DOCS_AI_API_FORMAT": "responses",
                               "DOCS_AI_API_KEY": "k"})
            profile = env_profile()
            self.assertEqual((profile.base_url, profile.api, profile.key()), ("https://x/v1", "responses", "k"))
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
