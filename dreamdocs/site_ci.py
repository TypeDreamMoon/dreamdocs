"""GitHub Actions coordination for a documentation site; never runs locally.

event          validate a plugin's dispatch and say which plugin revision to check out
commit-source  commit only the generated bilingual pages and records, and push them
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .site import config, git, load, validate, within

SECRET_ENV = ("GH_TOKEN", "DOCS_SYNC_TOKEN", "DOCS_SOURCE_TOKEN")


def run(root, *args):
    result = subprocess.run(list(args), cwd=root, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        # Never echo subprocess input or auth-bearing errors; whitespace errors name only file:line.
        detail = ""
        if args[1:4] == ("diff", "--staged", "--check"):
            detail = ": " + "; ".join(line for line in result.stdout.splitlines() if not line.startswith("+"))[:500]
        raise ValueError(f"{args[0]} {args[1]} failed (exit {result.returncode}){detail}")
    return result.stdout.rstrip("\r\n")  # Porcelain status keeps its significant leading spaces.


def output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def main(command: str, root: Path) -> int:
    if not os.environ.get("GITHUB_ACTIONS"):
        raise ValueError("This coordinator runs only inside GitHub Actions")
    root = Path(root).resolve()
    cfg = config(root)
    event = load(os.environ["GITHUB_EVENT_PATH"])
    default_branch = event["repository"]["default_branch"]
    if os.environ.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise ValueError("Docs automation must run from the website default branch")
    if command == "event":
        payload = event.get("client_payload", {})
        if payload and payload.get("repository") != cfg["repository"]:
            raise ValueError("Dispatch source does not match the configured plugin")
        sha = payload.get("sha") or os.environ.get("DOCS_SOURCE_SHA", "")
        if sha and not re.fullmatch(r"[a-f0-9]{40,64}", sha):
            raise ValueError("Invalid plugin SHA")
        output("repository", cfg["repository"])
        output("plugin_ref", sha or cfg["branch"])
        output("plugin_branch", cfg["branch"])
        output("site_branch", default_branch)
        return 0
    if command != "commit-source":
        raise ValueError(f"unknown site CI command {command!r}")
    validate(root)
    lock = load(root / "docs-sync/source-lock.json")
    if lock.get("workingTree"):
        raise ValueError("A dirty plugin snapshot cannot be committed to the website")
    tracked = set(run(root, "git", "ls-files").splitlines())
    # Both locales are generated from merged plugin documents. Notes stay site-owned;
    # sync verified the generated body before keeping them.
    allowed = {"docs-sync/source-lock.json"}
    allowed.update(name for name in tracked if name.startswith("docs-sync/sources/"))
    snapshots = root / "docs-sync/sources"
    if snapshots.exists():
        allowed.update(path.relative_to(root).as_posix() for path in snapshots.rglob("*") if path.is_file())
    for page in lock["pages"].values():
        if page["locale"] == "en":
            allowed.add(page["output"])
            allowed.add(page["output"].rsplit(".", 1)[0] + (".mdx" if page["format"] == "md" else ".md"))
        translation = page.get("translation")
        if translation and translation.get("output"):
            name = translation["output"]
            allowed.update((name, name.rsplit(".", 1)[0] + (".mdx" if name.endswith(".md") else ".md"), translation["record"]))
    paths = sorted(p for p in allowed if within(root, p).exists() or p in tracked)
    if paths:
        run(root, "git", "add", "--", *paths)
    run(root, "git", "diff", "--staged", "--check")
    staged = set(run(root, "git", "diff", "--staged", "--name-only").splitlines())
    if staged - allowed:
        raise ValueError("Sync staging contains paths outside the generated documentation allowlist")
    if staged:
        run(root, "git", "-c", "user.name=docs-sync[bot]", "-c", "user.email=docs-sync[bot]@users.noreply.github.com",
            "commit", "-m", f"docs: sync plugin {lock['revision'][:12]}")
        # A non-fast-forward failure is intended: rerun from a fresh main, never force-push.
        run(root, "git", "push", "origin", f"HEAD:refs/heads/{default_branch}")
    output("sha", git(root, "rev-parse", "HEAD"))
    return 0
