"""Translate the stale and missing slots of a plugin's manuals and rebuild the pages.

A page is written only once every slot it needs is translated. Each translated
slot goes to the local cache the moment it arrives, so a run that stops halfway
(no network, a used-up limit, Ctrl+C) costs nothing to resume.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import check, inline, links
from .align import Alignment, Row, compare
from .cache import Cache, job_key
from .markdown import ANCHOR, EXPLICIT_ID, Doc, Slot, digest, escape, parse, squash
from .migrate import untranslated
from .plugin import Plugin, read
from .profiles import Profile
from .providers import Busy, Client, ProviderError, Stop, parse_json_reply

KIND_NAMES = {"meta": "frontmatter value", "h": "heading", "p": "paragraph", "li": "list item", "q": "quote",
              "row": "table row"}
BATCH_SLOTS = 40
PACKAGE = Path(__file__).resolve().parent


@dataclass
class Job:
    id: str
    key: str
    slot: Slot
    page: str
    masked: list[str | None]
    values: list[list[str]]
    section: str = ""
    before: str = ""
    previous_translation: str = ""
    previous_source: str = ""
    problem: str = ""
    result: list[str] | None = None
    cached: bool = False


@dataclass
class PagePlan:
    page: str
    english: Doc
    translated: Doc | None = None
    table: Alignment | None = None
    fill: dict[int, tuple[list[str], str]] = field(default_factory=dict)
    waiting: dict[int, Job] = field(default_factory=dict)
    note: str = ""
    edited: bool = False
    layout_changed: bool = False

    @property
    def needs_write(self) -> bool:
        return not self.note and (bool(self.waiting) or self.edited or self.layout_changed or self.translated is None)


@dataclass
class Summary:
    written: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    unfinished: list[tuple[str, int]] = field(default_factory=list)
    jobs: int = 0
    sent: int = 0
    cached: int = 0
    reused: int = 0
    failures: list[str] = field(default_factory=list)
    stopped: str = ""


def page_hash(doc: Doc) -> str:
    return digest(doc.src)[:12]


# -- planning -----------------------------------------------------------------

def _sections(doc: Doc) -> list[str]:
    stack: list[tuple[int, str]] = []
    out = []
    for slot in doc.slots:
        if slot.kind == "h":
            while stack and stack[-1][0] >= slot.level:
                stack.pop()
            out.append(" › ".join(t for _, t in stack))
            stack.append((slot.level, squash(slot.texts[0])))
        else:
            out.append(" › ".join(t for _, t in stack))
    return out


def _old_english(plugin: Plugin, table: Alignment, page: str) -> dict[str, str]:
    """English slot text by fingerprint, from the revision the translation was aligned with."""
    if not table.rev:
        return {}
    try:
        text = subprocess.run(["git", "-C", str(plugin.root), "show", f"{table.rev}:Docs/{page}"],
                              capture_output=True, check=True).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return {}
    return {s.key: " | ".join(s.texts) for s in parse(text).slots}


def plan(plugin: Plugin, pages: list[str], cache: Cache, model: str, reuse: bool = True,
         rebuild: bool = False) -> tuple[list[PagePlan], dict[str, Job]]:
    plans: list[PagePlan] = []
    pending: list[tuple[PagePlan, int, str]] = []   # (plan, slot index, previous translation index or "")

    def exists(relative: str) -> bool:
        return (plugin.root / "Docs" / relative).exists()

    for page in pages:
        english = parse(read(plugin.english(page)))
        p = PagePlan(page, english)
        plans.append(p)
        if page in plugin.manual:
            p.note = "manual page: translate it by hand"
            continue
        zh_path = plugin.translation(page)
        states = untranslated(english).states
        if zh_path.exists():
            table_path = plugin.alignment(page)
            if not table_path.exists():
                p.note = "has a translation but no alignment table: run dreamdocs migrate --write first"
                continue
            p.translated, p.table = parse(read(zh_path)), Alignment.read(table_path)
            comparison = compare(p.table, english, p.translated)
            if comparison.mismatch:
                p.note = f"translation no longer matches its table ({comparison.mismatch}); fix the page or mark it manual"
                continue
            states = comparison.states
            p.edited = bool(comparison.edited)
            p.layout_changed = rebuild or p.table.extra.get("page", "") != page_hash(english)
        old_english = None
        for s in states:
            slot = english.slots[s.index]
            if s.state == "skip":
                p.fill[s.index] = (list(slot.texts), "s")
            elif s.state == "current":
                # Kept text is stored with links as the English writes them; the page is re-pointed as a whole.
                kept = [links.to_source(t, page, plugin.target, exists) for t in p.translated.slots[s.old].texts]
                p.fill[s.index] = (kept, "h" if s.edited else p.table.rows[s.old].origin)
            else:
                previous = ""
                if s.old is not None and p.translated is not None:
                    previous = " | ".join(p.translated.slots[s.old].texts)
                    if old_english is None:
                        old_english = _old_english(plugin, p.table, page)
                    source = old_english.get(p.table.rows[s.old].known or "", "")
                    previous = json.dumps([previous, source], ensure_ascii=False)
                pending.append((p, s.index, previous))

    # Reuse: an English slot already translated somewhere else is copied, not sent.
    known: dict[str, list[str]] = {}
    if reuse:
        for p in plans:
            for index, (texts, origin) in p.fill.items():
                if origin in "mhc":
                    known.setdefault(p.english.slots[index].key, texts)

    jobs: dict[str, Job] = {}
    sections: dict[str, list[str]] = {}
    for p, index, previous in pending:
        slot = p.english.slots[index]
        if slot.key in known:
            p.fill[index] = (list(known[slot.key]), "c")
            continue
        masked, values = [], []
        for text in slot.texts:
            if inline.translatable(text):
                m, v = inline.mask(text, plugin.codes)
                masked.append(m)
                values.append(v)
            else:
                masked.append(None)
                values.append([])
        if all(m is None for m in masked):
            p.fill[index] = (list(slot.texts), "s")
            continue
        key = job_key(slot.key, masked)
        job = jobs.get(key)
        if job is None:
            job = Job(f"s{len(jobs) + 1}", key, slot, p.page, masked, values)
            if p.page not in sections:
                sections[p.page] = _sections(p.english)
            job.section = sections[p.page][index]
            if index > 0:
                job.before = squash(" | ".join(p.english.slots[index - 1].texts))[:300]
            if previous:
                job.previous_translation, job.previous_source = json.loads(previous)
            cached = cache.get(model, plugin.target, key)
            if cached is not None and len(cached) == len(slot.texts):
                job.result, job.cached = cached, True
            jobs[key] = job
        p.waiting[index] = job
    return plans, jobs


# -- one batch ------------------------------------------------------------------

def _glossary(plugin: Plugin) -> dict[str, str]:
    terms = json.loads((PACKAGE / "glossary" / f"base.{plugin.target}.json").read_text(encoding="utf-8")) \
        if (PACKAGE / "glossary" / f"base.{plugin.target}.json").exists() else {}
    own = plugin.mirror / ".dreamdocs" / "glossary.json"
    if own.exists():
        terms.update(json.loads(own.read_text(encoding="utf-8")))
    for word in plugin.config.get("keep", []):
        terms[word] = word
    return terms


def system_prompt(plugin: Plugin) -> str:
    text = (PACKAGE / "prompts" / f"translate.{plugin.target}.md").read_text(encoding="utf-8")
    own = plugin.mirror / ".dreamdocs" / "prompt.md"
    if own.exists():
        text += "\n\nProject notes:\n\n" + own.read_text(encoding="utf-8")
    return text


def request_body(jobs: list[Job], glossary: dict[str, str], title: str) -> str:
    text = " ".join(m for j in jobs for m in j.masked if m) + " " + " ".join(j.previous_source for j in jobs)
    hits = {k: v for k, v in glossary.items() if re.search(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", text, re.I)}
    slots = []
    for j in jobs:
        entry = {"id": j.id, "kind": KIND_NAMES[j.slot.kind]}
        if j.section:
            entry["section"] = j.section
        if j.before:
            entry["before"] = j.before
        if j.slot.kind == "row":
            entry["cells"] = j.masked
        else:
            entry["text"] = j.masked[0]
        if j.previous_translation:
            entry["previous_translation"] = j.previous_translation
            if j.previous_source:
                entry["previous_source"] = j.previous_source
        if j.problem:
            entry["problem"] = j.problem
        slots.append(entry)
    body = {"page": jobs[0].page, "title": title, "slots": slots}
    glossary_hits = {k: v for k, v in hits.items() if k != v}
    keep = sorted(k for k, v in hits.items() if k == v)
    if glossary_hits:
        body["GLOSSARY"] = glossary_hits
    if keep:
        body["KEEP"] = keep
    return json.dumps(body, ensure_ascii=False)


_MARKUP = re.compile(r"<[A-Za-z/!]|[{}]|`{3}")


def _repair(out: str, masked: str, values: list[str]) -> str:
    """Undo the two usual slips: backticks around a placeholder, and the frozen code written out."""
    out = re.sub(r"`+(⟦(\d+)⟧)`+", lambda m: f"⟦{m[2]}⟧" if int(m[2]) <= len(values) and values[int(m[2]) - 1].startswith("`") else m[0], out)
    want = inline.PLACEHOLDER.findall(masked)
    have = inline.PLACEHOLDER.findall(out)
    for n in want:
        if have.count(n) < want.count(n):
            value = values[int(n) - 1]
            if value in out:
                out = out.replace(value, f"⟦{n}⟧", 1)
                have.append(n)
    return out


def accept(job: Job, answer) -> tuple[list[str] | None, str]:
    """(translated texts, "") or (None, what is wrong)."""
    if job.slot.kind == "row":
        if not isinstance(answer, list) or len(answer) != len(job.masked):
            return None, f"answer with a list of {len(job.masked)} cells, null where cells has null"
        answers = answer
    else:
        if isinstance(answer, list) and len(answer) == 1:
            answer = answer[0]
        if not isinstance(answer, str):
            return None, "answer with one string"
        answers = [answer]
    texts = []
    for source, masked, values, out in zip(job.slot.texts, job.masked, job.values, answers):
        if masked is None:
            texts.append(source)
            continue
        if not isinstance(out, str) or not out.strip():
            return None, "the translation is empty"
        out = re.sub(r"\s*\n\s*", " ", out.strip())
        out = _repair(out, masked, values)
        problem = inline.check_placeholders(masked, out)
        if problem:
            return None, f"placeholders wrong: {problem}; keep each ⟦n⟧ exactly as often as in the English"
        added = len(_MARKUP.findall(inline.PLACEHOLDER.sub("", out))) - len(_MARKUP.findall(inline.PLACEHOLDER.sub("", masked)))
        if added > 0:
            return None, "the translation added HTML, braces or a code fence; write plain text and placeholders only"
        texts.append(inline.unmask(out, values))
    return texts, ""


class Run:
    def __init__(self, plugin: Plugin, profile: Profile, client: Client, cache: Cache, log=print):
        self.plugin, self.profile, self.client, self.cache, self.log = plugin, profile, client, cache, log
        self.stop = threading.Event()
        self.stop_reason = ""
        self.lock = threading.Lock()
        self.system = system_prompt(plugin)
        self.glossary = _glossary(plugin)
        self.run_id = uuid.uuid4().hex[:8]
        self.sent = 0
        self.failures: list[str] = []

    def ask(self, jobs: list[Job], title: str) -> list[Job]:
        """Send one batch; return the jobs still without a translation."""
        if self.stop.is_set():
            return jobs
        session = f"dreamdocs-{self.run_id}-{digest(jobs[0].page)[:8]}"
        body = request_body(jobs, self.glossary, title)
        for attempt in range(6):
            if self.stop.is_set():
                return jobs
            try:
                reply = self.client.complete(self.system, body, session)
                break
            except Stop as error:
                self._halt(str(error))
                return jobs
            except Busy as error:
                wait = error.wait if error.wait is not None else min(120, 3 * 2 ** attempt)
                if wait > 300 or attempt == 5:
                    self._halt(f"{error} (gave up waiting; translations so far are cached)")
                    return jobs
                self.log(f"  waiting {wait:.0f} s: {error}")
                time.sleep(wait)
            except ProviderError as error:
                for job in jobs:
                    job.problem = str(error)
                return jobs
        try:
            answers = parse_json_reply(reply.text).get("slots")
        except ProviderError as error:
            for job in jobs:
                job.problem = f"your last reply was unusable ({error}); reply with the JSON object only"
            return jobs
        if not isinstance(answers, dict):
            answers = {}
        left = []
        for job in jobs:
            if job.id not in answers:
                job.problem = "your last reply left this id out; translate it"
                left.append(job)
                continue
            texts, problem = accept(job, answers[job.id])
            if texts is None:
                job.problem = problem
                left.append(job)
                continue
            job.result, job.problem = texts, ""
            self.cache.put(self.profile.model, self.plugin.target, job.key, texts)
            with self.lock:
                self.sent += 1
        return left

    def _halt(self, reason: str) -> None:
        with self.lock:
            if not self.stop.is_set():
                self.stop_reason = reason
                self.stop.set()


def batches(jobs: list[Job], chunk_chars: int) -> list[list[Job]]:
    out, current, size = [], [], 0
    for job in jobs:
        weight = sum(len(m or "") for m in job.masked) + len(job.previous_translation) + len(job.previous_source)
        if current and (job.page != current[0].page or size + weight > chunk_chars or len(current) >= BATCH_SLOTS):
            out.append(current)
            current, size = [], 0
        current.append(job)
        size += weight
    if current:
        out.append(current)
    return out


# -- writing pages ----------------------------------------------------------------

def _anchor_slug(title: str) -> str:
    # The convention the documentation sites already read: lower case, punctuation dropped, spaces to dashes.
    return re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")


def heading_anchors(english: Doc) -> dict[int, str]:
    """An <a id> line before each top-level heading, so the translation keeps the English anchor."""
    inserts, counts = {}, {}
    src = english.src
    for slot in english.slots:
        if slot.kind != "h":
            continue
        start = slot.spans[0][0]
        line_start = src.rfind("\n", 0, start) + 1
        line_end = src.find("\n", start)
        line_end = len(src) if line_end < 0 else line_end
        if EXPLICIT_ID.search(src[slot.spans[0][1]:line_end]):
            continue
        prefix = src[line_start:start]
        if not (slot.setext and prefix == "" or re.fullmatch(r"#{1,6}[ \t]+", prefix)):
            continue  # Headings inside lists or quotes keep whatever the renderer gives them.
        before = src[:line_start].rstrip("\n").rsplit("\n", 1)[-1]
        if ANCHOR.match(before):
            continue
        slug = _anchor_slug(slot.texts[0])
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        inserts[line_start] = f'<a id="{slug if count == 0 else f"{slug}-{count}"}"></a>\n\n'
    return inserts


def assemble(p: PagePlan, plugin: Plugin, translated_pages: set[str]) -> tuple[str, list[str]]:
    """The translated page and the origin of each slot."""
    texts, origins = [], []
    for index, slot in enumerate(p.english.slots):
        if index in p.waiting:
            texts.append(p.waiting[index].result)
            origins.append("m")
        else:
            value, origin = p.fill[index]
            texts.append(value)
            origins.append(origin)
    values = {i: [escape(slot, t) for t in texts[i]] for i, slot in enumerate(p.english.slots)}
    out = p.english.render(values, heading_anchors(p.english))
    out = links.relocate(out, p.page, plugin.target, translated_pages)
    return out, origins


def write_page(p: PagePlan, plugin: Plugin, translated_pages: set[str], model: str) -> None:
    out, origins = assemble(p, plugin, translated_pages)
    zh = parse(out)
    if [s.signature for s in zh.slots] != [s.signature for s in p.english.slots]:
        raise ValueError(f"{p.page}: the rebuilt translation does not keep the English structure (a dreamdocs bug)")
    found = check.problems(zh)
    if found:
        raise ValueError(f"{p.page}: the rebuilt translation fails the self-check: {found[0]}")
    table = Alignment(f"Docs/{p.page}", plugin.revision()[:12])
    table.extra["page"] = page_hash(p.english)
    old_model = p.table.extra.get("model", "") if p.table else ""
    if p.waiting:
        table.extra["model"] = model
    elif old_model:
        table.extra["model"] = old_model
    table.rows = [Row(e.signature, e.key, z.key[:8], o) for e, z, o in zip(p.english.slots, zh.slots, origins)]
    path = plugin.translation(p.page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out, encoding="utf-8", newline="\n")
    table.write(plugin.alignment(p.page))


# -- the whole run ----------------------------------------------------------------

@dataclass
class Estimate:
    pages: int
    jobs: int
    cached: int
    reused: int
    requests: int
    chars: int
    input_tokens: int
    output_tokens: int
    cost: float | None


def estimate(plans: list[PagePlan], jobs: dict[str, Job], profile: Profile, client: Client | None,
             system: str) -> Estimate:
    todo = [j for j in jobs.values() if j.result is None]
    groups = batches(todo, profile.chunk_chars)
    chars = sum(len(m or "") for j in todo for m in j.masked)
    context = sum(len(j.previous_translation) + len(j.previous_source) + len(j.before) + len(j.section) for j in todo)
    input_tokens = int((chars + context) / 3.2 + len(groups) * (len(system) / 3.5 + 150))
    output_tokens = int(chars / 2.2)
    cost = None
    if client is not None and profile.type == "OpenCodeGo":
        try:
            price = client.endpoint().price
            if price:
                cost = (input_tokens * price[0] + output_tokens * price[1]) / 1e6
        except ProviderError:
            pass
    reused = sum(1 for p in plans for _, origin in p.fill.values() if origin == "c")
    return Estimate(sum(1 for p in plans if p.needs_write), len(jobs), sum(j.cached for j in jobs.values()), reused,
                    len(groups), chars, input_tokens, output_tokens, cost)


def run(plugin: Plugin, profile: Profile, plans: list[PagePlan], jobs: dict[str, Job], client: Client,
        cache: Cache, limit: int | None = None, log=print) -> Summary:
    summary = Summary(jobs=len(jobs), cached=sum(j.cached for j in jobs.values()))
    summary.reused = sum(1 for p in plans for _, origin in p.fill.values() if origin == "c")
    for p in plans:
        if p.note:
            summary.skipped.append((p.page, p.note))
    todo = [j for j in jobs.values() if j.result is None]
    if limit is not None:
        todo = todo[:limit]
    titles = {p.page: next((squash(s.texts[0]) for s in p.english.slots if s.kind in ("h", "meta")), p.page) for p in plans}
    runner = Run(plugin, profile, client, cache, log)
    rounds = [batches(todo, profile.chunk_chars)]
    for attempt in range(profile.retry + 1):
        groups = rounds[-1]
        if not groups or runner.stop.is_set():
            break
        log(f"{'sending' if attempt == 0 else 'retrying'} {sum(len(g) for g in groups)} slots in {len(groups)} requests")
        done = [0]

        def one(group: list[Job]) -> list[Job]:
            left = runner.ask(group, titles[group[0].page])
            with runner.lock:
                done[0] += 1
                log(f"  [{done[0]}/{len(groups)}] {group[0].page}: {len(group) - len(left)}/{len(group)} slots")
            return left

        with ThreadPoolExecutor(max_workers=max(1, profile.concurrency)) as pool:
            left = [job for part in pool.map(one, groups) for job in part]
        # A slot that failed is asked again on its own, with what was wrong.
        rounds.append([[job] for job in left])
    summary.sent = runner.sent
    summary.stopped = runner.stop_reason
    summary.failures = [f"{j.page} {j.id}: {j.problem}" for g in rounds[-1] for j in g if j.problem]

    finished = [p for p in plans if p.needs_write and all(j.result is not None for j in p.waiting.values())]
    translated_pages = {page for page in plugin.pages() if plugin.translation(page).exists()} | {p.page for p in finished}
    for p in plans:
        if not p.needs_write:
            continue
        if p in finished:
            write_page(p, plugin, translated_pages, profile.model)
            summary.written.append(p.page)
        else:
            summary.unfinished.append((p.page, sum(1 for j in p.waiting.values() if j.result is None)))
    return summary


def confirm(question: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        raise ValueError("not run from a terminal: pass --yes to send the requests, or --dry-run to only estimate")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
