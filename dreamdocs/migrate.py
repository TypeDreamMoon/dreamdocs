"""Turn the old per-page translation records into alignment tables.

The old records (Docs/<lang>/.translations/<page>.json) hold the whole English
page each translation was made from. Splitting that English and the translation
the same way and lining the two slot sequences up gives, for every translated
slot, the English it came from. Comparing that English with today's English then
says which slots are current, stale or missing -- slot by slot instead of page
by page.

Slots are lined up by structure (kind, heading level, cell count) plus the code
spans they contain, which a translation keeps verbatim. Where the structure
differs (a merged paragraph, a hand-written page), only the runs that still
match are kept; the rest is translated again.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import inline
from .align import Alignment, Comparison, Row, SlotState, compare, translatable
from .markdown import Doc, digest, parse, squash
from .plugin import Plugin, read


def _structure(doc: Doc) -> list[str]:
    return [f"{s.signature}|{inline.anchor_tokens(' '.join(s.texts))}" for s in doc.slots]


def line_up(source: Doc, translated: Doc) -> tuple[dict[int, int], int]:
    """Map translated slot index -> source slot index; also return how many pairs were rejected."""
    a, b = _structure(source), _structure(translated)
    if a == b:
        pairs = {i: i for i in range(len(a))}
    else:
        pairs = {}
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                pairs.update({j1 + k: i1 + k for k in range(i2 - i1)})
    rejected = 0
    for j, i in list(pairs.items()):
        # Prose with nothing to translate must come through unchanged; if it did not, the pair is wrong.
        if not translatable(source.slots[i]) and [squash(t) for t in source.slots[i].texts] != \
                [squash(t) for t in translated.slots[j].texts]:
            del pairs[j]
            rejected += 1
    return pairs, rejected


@dataclass
class PageResult:
    page: str
    state: str                      # missing | tracked | unverified
    lined: str = ""                 # exact | partial
    slots: int = 0                  # translated slots
    unaligned: int = 0              # translated slots with no known English
    rejected: int = 0
    added_meta: int = 0             # frontmatter slots the translation has and its English never had
    table: Alignment | None = None
    comparison: Comparison | None = None
    english: Doc | None = None


@dataclass
class PluginResult:
    name: str
    revision: str
    pages: list[PageResult] = field(default_factory=list)


def migrate_page(plugin: Plugin, page: str, adopt: bool = False) -> PageResult:
    """Line a translation up with the English it was made from and build its table.

    adopt: the translation was written by hand for today's English (no record says
    otherwise); its slots that line up count as current, made by a person.
    """
    english = parse(read(plugin.english(page)))
    zh_path = plugin.translation(page)
    if not zh_path.exists():
        return PageResult(page, "missing", english=english, comparison=untranslated(english))
    translated = parse(read(zh_path))
    record_path = plugin.legacy_record(page)
    record = json.loads(read(record_path)) if record_path.exists() else {}
    if adopt:
        source, verified, rev = english, True, plugin.revision()
        record = {}
    elif record.get("source") and record.get("sourceHash"):
        source, verified, rev = parse(record["source"]), True, record.get("sourceRevision", "")
    else:
        # Imported without the English it was made from: assume today's English, never trust it as current.
        source, verified, rev = english, False, ""
    pairs, rejected = line_up(source, translated)
    origin = "m" if record.get("origin") == "ai" else "h"
    table = Alignment(f"Docs/{page}", rev[:12])
    if record.get("model"):
        table.extra["model"] = record["model"]
    if verified:
        table.extra["page"] = digest(source.src)[:12]  # The English page this translation was laid out from.
    for j, slot in enumerate(translated.slots):
        i = pairs.get(j)
        if i is None:
            table.rows.append(Row(slot.signature, "-", slot.text_key, origin))
            continue
        en = source.slots[i].key if verified else "~" + source.slots[i].key
        table.rows.append(Row(slot.signature, en, slot.text_key, "s" if not translatable(source.slots[i]) else origin))
    # The old pipeline gave every translation a title and description, also when the English had no
    # frontmatter. Those extra slots have no English to line up with and are not lost structure.
    added = 0 if any(s.kind == "meta" for s in source.slots) else \
        sum(1 for j, s in enumerate(translated.slots) if s.kind == "meta" and j not in pairs)
    unaligned = len(translated.slots) - len(pairs) - added
    exact = unaligned == 0 and len(pairs) == len(source.slots)
    result = PageResult(page, "tracked" if verified else "unverified", lined="exact" if exact else "partial",
                        slots=len(translated.slots), unaligned=unaligned, rejected=rejected, added_meta=added,
                        table=table, english=english)
    result.comparison = compare(table, english, translated)
    return result


def untranslated(english: Doc) -> Comparison:
    return Comparison([SlotState(n, "missing" if translatable(s) else "skip") for n, s in enumerate(english.slots)])


def migrate(plugin: Plugin) -> PluginResult:
    result = PluginResult(plugin.root.name, plugin.revision()[:12])
    for page in plugin.pages():
        result.pages.append(migrate_page(plugin, page))
    return result


def write_tables(result: PluginResult, out: Path) -> int:
    written = 0
    for page in result.pages:
        if page.table:
            page.table.write(out / (page.page + ".tsv"))
            written += 1
    return written


# -- reporting ------------------------------------------------------------------

def _chars(doc: Doc, states, reusable: set[str]) -> tuple[int, int]:
    """(slots to send, characters to send) for the stale and missing slots not covered by reuse."""
    slots = chars = 0
    for s in states:
        slot = doc.slots[s.index]
        if s.state in ("stale", "missing") and slot.key not in reusable:
            slots += 1
            chars += len(squash(" ".join(slot.texts)))
    return slots, chars


def summarize(results: list[PluginResult]) -> tuple[str, dict]:
    # English slots that already have a current translation somewhere: copying beats translating again.
    reusable = {p.english.slots[s.index].key for r in results for p in r.pages if p.comparison
                for s in p.comparison.states if s.state == "current"}
    lines = ["# dreamdocs migration report", ""]
    lines += ["| Plugin | Pages | Translated | Lined up exactly | Partly | Structure lost | Slots current | Stale | Missing | Nothing to translate | Reusable | To send (slots / chars) |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    data = {}
    for r in results:
        counts = {k: 0 for k in ("current", "stale", "missing", "skip")}
        reuse = send_slots = send_chars = 0
        for p in r.pages:
            for s in p.comparison.states:
                counts[s.state] += 1
                if s.state in ("stale", "missing") and p.english.slots[s.index].key in reusable:
                    reuse += 1
            a, b = _chars(p.english, p.comparison.states, reusable)
            send_slots += a
            send_chars += b
        translated = [p for p in r.pages if p.state != "missing"]
        exact = sum(p.lined == "exact" for p in translated)
        lost = sum(p.unaligned for p in translated)
        lines.append(f"| {r.name} `{r.revision}` | {len(r.pages)} | {len(translated)} | {exact} | {len(translated) - exact} "
                     f"| {lost} slots | {counts['current']} | {counts['stale']} | {counts['missing']} | {counts['skip']} "
                     f"| {reuse} | {send_slots} / {send_chars:,} |")
        data[r.name] = {"revision": r.revision, "pages": len(r.pages), "translated": len(translated), "exact": exact,
                        "unalignedSlots": lost, **counts, "reusable": reuse, "sendSlots": send_slots, "sendChars": send_chars,
                        "perPage": [{"page": p.page, "state": p.state, "lined": p.lined, "slots": p.slots,
                                     "unaligned": p.unaligned, "rejected": p.rejected,
                                     **{k: p.comparison.count(k) for k in counts}} for p in r.pages]}
    lines += ["", "Stale = the English changed since the translation was made (or the translation was imported without its English);",
              "missing = no translation; nothing to translate = code, numbers or identifiers only; reusable = an identical English slot",
              "is already translated elsewhere, so it is copied, not sent.", ""]
    for r in results:
        partial = sorted((p for p in r.pages if p.state != "missing" and p.lined == "partial"),
                         key=lambda p: -p.unaligned)
        if partial:
            lines += [f"## {r.name}: pages whose structure no longer matches their translation", "",
                      "| Page | Translated slots | Not lined up | Rejected pairs | Imported |", "| --- | ---: | ---: | ---: | --- |"]
            lines += [f"| `{p.page}` | {p.slots} | {p.unaligned} | {p.rejected} | {'yes' if p.state == 'unverified' else ''} |"
                      for p in partial[:40]]
            if len(partial) > 40:
                lines.append(f"| ... {len(partial) - 40} more | | | | |")
            lines.append("")
    return "\n".join(lines), data
