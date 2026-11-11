"""Self-checks of the splitter, run over whole plugins."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import markdown
from .markdown import Doc, escape, parse, squash

_BLOCK_START = re.compile(r"^(?:#{1,6} |> |[-*+] |\d+[.)] |`{3}|~{3}|\|)")


def problems(doc: Doc) -> list[str]:
    """Everything wrong with how a page was split; empty when the split is sound."""
    found = []
    previous = 0
    for n, slot in enumerate(doc.slots, 1):
        for start, end in slot.spans:
            if not (previous <= start <= end <= len(doc.src)):
                found.append(f"slot {n}: span {start}-{end} overlaps or leaves the page")
            previous = end
        if (slot.kind in markdown.PROSE or slot.setext) and _BLOCK_START.match(slot.texts[0]):
            found.append(f"slot {n}: {slot.kind} starts like a block: {slot.texts[0][:60]!r}")
    if doc.render() != doc.src:
        found.append("rendering the unchanged slots does not give back the page")
    # Filling every slot with one line of prose -- what a translation does -- must keep the structure.
    values = {i: [escape(slot, t) for t in slot.texts] for i, slot in enumerate(doc.slots)}
    again = parse(doc.render(values))
    # Frontmatter values come back unquoted and unescaped; everything else comes back as written.
    before = [(s.signature, [squash(t) for t in (s.texts if s.kind == "meta" else values[i])])
              for i, s in enumerate(doc.slots)]
    after = [(s.signature, [squash(t) for t in s.texts]) for s in again.slots]
    if before != after:
        first = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
        a = before[first] if first < len(before) else None
        b = after[first] if first < len(after) else None
        found.append(f"slot {first + 1}: refilled page parses differently: {a!r:.160} -> {b!r:.160}")
    return found


@dataclass
class Report:
    files: int = 0
    slots: int = 0
    failures: dict[str, list[str]] = field(default_factory=dict)


_LINK = re.compile(r"\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)")


def _broken_links(path: Path, text: str) -> list[str]:
    """Relative links in a page that point at no file (fragments and queries are not checked)."""
    from .links import _outside_code
    found = []

    def scan(segment: str) -> str:
        for m in _LINK.finditer(segment):
            url = m[1]
            if re.match(r"^(?:[a-zA-Z][\w+.-]*:|//|/|#)", url):
                continue
            target = (path.parent / urllib.parse.unquote(url.split("#", 1)[0].split("?", 1)[0])).resolve()
            if not target.exists():
                found.append(url)
        return segment

    _outside_code(text, scan)
    return found


@dataclass
class PluginReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages: int = 0


def check_plugin(root: Path, strict: bool = False) -> PluginReport:
    """What CI asks of a plugin's manuals and their translations."""
    from .plugin import Plugin
    from .status import page_status
    plugin = Plugin.open(root)
    report = PluginReport()
    tree = check_tree(plugin.root / "Docs")
    for name, found in tree.failures.items():
        report.errors.append(f"Docs/{name}: {found[0]}")
    for page in plugin.chinese_original:
        if not (plugin.mirror / page).exists():
            report.errors.append(f"Docs/{plugin.target}/{page}: listed in chineseOriginal but there is no such Chinese page")
    for direction in plugin.directions():
        pages = direction.pages()
        report.pages += len(pages)
        for page in pages:
            status = page_status(plugin, page, direction=direction)
            where = direction.translation_display(page)
            if status.state == "untracked":
                report.errors.append(f"{where}: a translation without an alignment table (dreamdocs adopt or migrate --write)")
            elif status.state == "mismatch":
                report.errors.append(f"{where}: no longer matches its alignment table ({status.detail})")
            elif status.state in ("outdated", "missing"):
                stale = status.comparison.count("stale") + status.comparison.count("missing") if status.comparison else 0
                note = f"{where}: {status.state}" + (f", {stale} paragraphs to translate" if stale else "") + \
                    (f" ({status.detail})" if status.detail else "")
                (report.errors if strict and status.state == "outdated" else report.warnings).append(note)
    english = set(plugin.pages())
    originals = set(plugin.chinese_original)
    for path in sorted(plugin.mirror.rglob("*.md")):
        relative = path.relative_to(plugin.mirror).as_posix()
        if relative.startswith("."):
            continue
        if relative not in originals and not (plugin.root / "Docs" / relative).exists():
            report.errors.append(f"Docs/{plugin.target}/{relative}: a translation of no English page "
                                 f"(a page written in Chinese first goes into chineseOriginal)")
        for url in _broken_links(path, path.read_text(encoding="utf-8")):
            (report.errors if strict else report.warnings).append(f"Docs/{plugin.target}/{relative}: broken link {url}")
    for tables, known, where in ((plugin.mirror / ".align", english, f"Docs/{plugin.target}/.align"),
                                 (plugin.root / "Docs" / ".align", originals, "Docs/.align")):
        if tables.exists():
            for path in sorted(tables.rglob("*.tsv")):
                page = path.relative_to(tables).as_posix()[:-4]
                if page not in known:
                    report.errors.append(f"{where}/{page}.tsv: a table for no original page")
    return report


def check_tree(root: Path) -> Report:
    report = Report()
    for path in sorted(root.rglob("*.md")):
        doc = parse(path.read_text(encoding="utf-8"))
        report.files += 1
        report.slots += len(doc.slots)
        found = problems(doc)
        if found:
            report.failures[path.relative_to(root).as_posix()] = found
    return report
