"""dreamdocs ci translate: the plugin workflow that keeps translations up to date.

After English (or a page written in Chinese first) changes on the default branch,
GitHub Actions runs this in the plugin checkout. It translates what is stale, checks
the result, and puts it on one branch with one pull request, which a person reviews
and merges. Nothing is sent without DOCS_AI_MODEL and DOCS_AI_API_KEY; nothing is
pushed when the plugin check fails or when someone else has pushed to that branch.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898583+github-actions[bot]@users.noreply.github.com"
DEFAULT_MAX_SLOTS = 3000
BODY_LIMIT = 60000          # GitHub refuses pull request bodies over 65,536 characters.


def _one_line(texts) -> str:
    return " | ".join(t.strip() for t in texts if t is not None and t.strip())


def review(pages) -> list[str]:
    """For each written page, every paragraph sent: the source, the translation it had, the new one."""
    out = []
    for label, plan in pages:
        jobs = [j for j in plan.waiting.values() if j.result is not None]
        if not jobs:
            continue
        out.append(f"<details><summary><code>{plan.page}</code> ({label}): {len(jobs)} paragraphs</summary>\n")
        for job in jobs:
            out.append(f"- **Source:** {_one_line(job.slot.texts)}")
            if job.previous_translation:
                out.append(f"  **Before:** {job.previous_translation.strip()}")
            out.append(f"  **Now:** {_one_line(job.result)}")
        out.append("\n</details>")
    return out


def _run(args: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


@dataclass
class Outcome:
    state: str            # skipped / nothing / blocked / failed / pushed
    message: str = ""
    lines: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)


def notice(kind: str, text: str) -> None:
    """A GitHub Actions annotation, or a plain line elsewhere."""
    print(f"::{kind}::{text}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"{kind}: {text}", file=sys.stderr)


def foreign_commits(root: Path, branch: str, base: str, run=_run) -> list[str]:
    """Commits on origin/<branch> after the base that the workflow did not make."""
    if not run(["git", "ls-remote", "--heads", "origin", branch], root):
        return []
    run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], root)
    log = run(["git", "log", "--format=%h %ae %s", f"origin/{base}..origin/{branch}"], root)
    return [line for line in log.splitlines() if line.split(" ", 2)[1] != BOT_EMAIL]


def translate_pr(root: Path, branch: str = "dreamdocs/translate", base: str = "main", push: bool = True,
                 run=_run, log=lambda line: print(line, file=sys.stderr)) -> Outcome:
    from . import translate
    from .cache import Cache
    from .check import check_plugin
    from .plugin import Plugin
    from .profiles import env_profile
    from .providers import Client

    profile = env_profile()
    if profile is None or not profile.key():
        return Outcome("skipped", "no translator configured (set the DOCS_AI_API_KEY secret and the DOCS_AI_MODEL "
                                  "variable); translations were not updated")
    root = Path(root).resolve()
    if push:
        others = foreign_commits(root, branch, base, run)
        if others:
            return Outcome("blocked", f"{branch} has commits not made by this workflow ({others[0]}); merge or delete "
                                      f"that branch first, nothing was translated")
    plugin = Plugin.open(root)
    cache = Cache()
    limit = int(os.environ.get("DOCS_AI_MAX_SLOTS") or DEFAULT_MAX_SLOTS)
    work = translate.plan_all(plugin, cache, profile.model)
    client = Client(profile, cache)
    outcome = Outcome("nothing")
    stopped = ""
    written = []
    for w in work:
        todo = sum(1 for j in w.jobs.values() if j.result is None)
        if not todo and not any(p.needs_write for p in w.plans):
            continue
        summary = translate.run(plugin, profile, w.plans, w.jobs, client, cache, limit, log=log)
        limit = max(0, limit - summary.sent)
        written.extend((w.direction.label, p) for p in w.plans if p.page in summary.written)
        outcome.lines.append(f"- {w.direction.label}: {len(summary.written)} pages written, {summary.sent} paragraphs "
                             f"translated, {summary.cached} from the cache, {summary.reused} copied")
        for page, left in summary.unfinished:
            outcome.lines.append(f"  - not written yet: `{page}` ({left} paragraphs left; the next run continues)")
        for page, note in summary.skipped:
            outcome.lines.append(f"  - skipped `{page}`: {note}")
        outcome.lines.extend(f"  - failed: {line}" for line in summary.failures[:10])
        if summary.stopped:
            stopped = summary.stopped
            break
    if client.requests:
        outcome.lines.append(f"- {profile.describe()}: {client.requests} requests, {client.input_tokens:,} input / "
                             f"{client.output_tokens:,} output tokens")
    changed = run(["git", "status", "--porcelain", "--", "Docs"], root)
    if stopped and not changed:
        return Outcome("failed", f"the translator stopped: {stopped}", outcome.lines)
    if not changed:
        return outcome
    report = check_plugin(root)
    if report.errors:
        return Outcome("failed", "the translated pages fail dreamdocs check: " + "; ".join(report.errors[:5]),
                       outcome.lines)
    outcome.review = review(written)
    if not push:
        return Outcome("pushed", "translated (not pushed)", outcome.lines, outcome.review)
    head = run(["git", "rev-parse", "--short", "HEAD"], root)
    identity = ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    run(["git", "checkout", "-B", branch], root)
    run(["git", "add", "--all", "--", "Docs"], root)
    run(["git", *identity, "commit", "--quiet", "-m", f"Update translations for {head}"], root)
    run(["git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}"], root)
    shown, size = [], 0
    for line in outcome.review:
        size += len(line) + 1
        if size > BODY_LIMIT:
            shown.append("\n</details>\n\nThe rest is in the run summary and the diff.")
            break
        shown.append(line)
    body = "\n".join([f"Translations of the pages that changed up to {head}, made by `dreamdocs ci translate`.", "",
                      *outcome.lines, "", *shown, "",
                      "Review the wording, edit on this branch if needed (the workflow then leaves the branch alone "
                      "until it is merged), and merge with a merge commit, not a squash: the alignment tables record "
                      "the commit they were made from."]
                     + (["", f"The translator stopped early: {stopped}"] if stopped else []))
    number = run(["gh", "pr", "list", "--head", branch, "--base", base, "--state", "open", "--json", "number",
                  "--jq", ".[0].number"], root)
    if number:
        run(["gh", "pr", "edit", number, "--body", body], root)
        outcome.message = f"updated pull request #{number}"
    else:
        url = run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", "Update translations",
                   "--body", body], root)
        outcome.message = f"opened {url}"
    outcome.state = "pushed"
    if stopped:
        outcome.state, outcome.message = "failed", outcome.message + f"; the translator stopped: {stopped}"
    return outcome


def main(args) -> int:
    try:
        outcome = translate_pr(Path(args.plugin), args.branch, args.base, push=not args.no_push)
    except RuntimeError as error:
        notice("error", str(error))
        return 1
    for line in outcome.lines:
        print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Translations\n\n" + (outcome.message or outcome.state) + "\n\n" + "\n".join(outcome.lines) + "\n\n"
                    + "\n".join(outcome.review) + "\n")
    if outcome.state == "skipped":
        notice("warning", outcome.message)
        return 0
    if outcome.state == "nothing":
        print("every translation is up to date")
        return 0
    if outcome.state in ("blocked", "failed"):
        notice("error", outcome.message)
        return 1
    print(outcome.message)
    return 0
