"""Self-checks of the splitter, run over whole plugins."""
from __future__ import annotations

import re
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
