"""dreamdocs ci translate against a throwaway git remote and the local stand-in model; gh is faked."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider import FakeProvider  # noqa: E402

from dreamdocs import ci  # noqa: E402
from dreamdocs.profiles import env_profile  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "needs git")
class CiTranslateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.server = FakeProvider()
        self.addCleanup(self.server.close)
        env = patch.dict(os.environ, {"DOCS_AI_MODEL": "fake-model", "DOCS_AI_BASE_URL": self.server.url,
                                      "DOCS_AI_API_KEY": "sk-ci", "DOCS_AI_TYPE": "OpenAI",
                                      "DREAMDOCS_CACHE": str(base / "cache"), "GITHUB_ACTIONS": "",
                                      "GIT_CONFIG_GLOBAL": str(base / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        (base / "gitconfig").write_text("[user]\n\tname = Tester\n\temail = tester@example.com\n[core]\n\tautocrlf = false\n"
                                        "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        self.remote = base / "remote.git"
        git(base, "init", "--quiet", "--bare", str(self.remote))
        seed = base / "seed"
        (seed / "Docs/zh-CN").mkdir(parents=True)
        (seed / "Docs/guide.md").write_text("# Guide\n\nEnglish paragraph.\n", encoding="utf-8")
        (seed / "Docs/zh-CN/share.md").write_text("# 分享\n\n中文原文。\n", encoding="utf-8")
        (seed / "Docs/dreamdocs.json").write_text(json.dumps({"version": 2, "chineseOriginal": ["share.md"]}),
                                                  encoding="utf-8")
        git(seed, "init", "--quiet")
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "seed")
        git(seed, "push", "--quiet", str(self.remote), "HEAD:refs/heads/main")
        self.base = base
        self.gh_calls = []
        self.prs = []

    def clone(self, name):
        path = self.base / name
        git(self.base, "clone", "--quiet", str(self.remote), str(path))
        return path

    def run_ci(self, root, **kw):
        def fake(args, cwd, check=True):
            if args[0] == "gh":
                self.gh_calls.append(args[1:3])
                if args[1:3] == ["pr", "list"]:
                    return str(self.prs[-1]) if self.prs else ""
                if args[1:3] == ["pr", "create"]:
                    self.prs.append(len(self.prs) + 1)
                    return f"https://example.invalid/pull/{self.prs[-1]}"
                return ""
            return ci._run(args, cwd, check)
        return ci.translate_pr(root, run=fake, log=lambda *_: None, **kw)

    def test_no_key_skips_without_sending(self):
        with patch.dict(os.environ, {"DOCS_AI_API_KEY": ""}):
            outcome = self.run_ci(self.clone("a"))
        self.assertEqual(outcome.state, "skipped")
        self.assertEqual(self.server.requests, [])

    def test_stale_pages_go_to_one_branch_and_pull_request(self):
        outcome = self.run_ci(self.clone("a"))
        self.assertEqual(outcome.state, "pushed", outcome.message)
        self.assertIn(["pr", "create"], self.gh_calls)
        files = git(self.remote, "ls-tree", "-r", "--name-only", "dreamdocs/translate").splitlines()
        self.assertIn("Docs/zh-CN/guide.md", files)
        self.assertIn("Docs/share.md", files)
        self.assertIn("Docs/.align/share.md.tsv", files)
        self.assertEqual(git(self.remote, "log", "-1", "--format=%ae", "dreamdocs/translate"), ci.BOT_EMAIL)
        # Run again on a fresh checkout of main: the branch is the bot's, so it is rebuilt and the PR updated.
        (self.base / "seed/Docs/guide.md").write_text("# Guide\n\nEnglish paragraph.\n\nMore.\n", encoding="utf-8")
        git(self.base / "seed", "commit", "--quiet", "-am", "more")
        git(self.base / "seed", "push", "--quiet", str(self.remote), "HEAD:refs/heads/main")
        outcome = self.run_ci(self.clone("b"))
        self.assertEqual((outcome.state, outcome.message), ("pushed", "updated pull request #1"))
        self.assertIn("译More.", git(self.remote, "show", "dreamdocs/translate:Docs/zh-CN/guide.md"))

    def test_nothing_stale_does_nothing(self):
        a = self.clone("a")
        self.run_ci(a)
        git(self.remote, "update-ref", "refs/heads/main", "refs/heads/dreamdocs/translate")
        self.gh_calls.clear()
        self.server.requests.clear()
        outcome = self.run_ci(self.clone("b"))
        self.assertEqual(outcome.state, "nothing")
        self.assertEqual((self.server.requests, self.gh_calls), ([], []))

    def test_someone_elses_commit_blocks_the_branch(self):
        a = self.clone("a")
        self.run_ci(a)
        (a / "Docs/zh-CN/guide.md").write_text("# 指南\n\n人工改过。\n", encoding="utf-8")
        git(a, "commit", "--quiet", "-am", "hand edit")
        git(a, "push", "--quiet", "origin", "dreamdocs/translate")
        self.server.requests.clear()
        outcome = self.run_ci(self.clone("b"))
        self.assertEqual(outcome.state, "blocked")
        self.assertIn("hand edit", outcome.message)
        self.assertEqual(self.server.requests, [])

    def test_failed_check_pushes_nothing(self):
        a = self.clone("a")
        with patch("dreamdocs.check.check_plugin") as check:
            check.return_value.errors = ["Docs/zh-CN/guide.md: broken"]
            outcome = self.run_ci(a)
        self.assertEqual(outcome.state, "failed")
        self.assertEqual(git(self.remote, "branch", "--list", "dreamdocs/*"), "")


class EnvProfileTests(unittest.TestCase):
    def test_type_picks_the_service(self):
        with patch.dict(os.environ, {"DOCS_AI_MODEL": "deepseek-v4.1-flash", "DOCS_AI_TYPE": "opencodego",
                                     "DOCS_AI_API_KEY": "k", "DOCS_AI_BASE_URL": "", "DOCS_AI_API_FORMAT": ""}):
            profile = env_profile()
            self.assertEqual((profile.type, profile.api, profile.base_url), ("OpenCodeGo", "", ""))
        with patch.dict(os.environ, {"DOCS_AI_MODEL": "m", "DOCS_AI_TYPE": "Anthropic", "DOCS_AI_API_FORMAT": ""}):
            self.assertEqual(env_profile().type, "Anthropic")
        with patch.dict(os.environ, {"DOCS_AI_MODEL": "m", "DOCS_AI_TYPE": "DeepL"}):
            with self.assertRaisesRegex(ValueError, "DOCS_AI_TYPE"):
                env_profile()


if __name__ == "__main__":
    unittest.main()
