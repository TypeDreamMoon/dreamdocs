"""The alignment table: which English prose each slot of a translated page came from.

The translated Markdown file holds the text; this table only holds fingerprints,
one row per slot of the translated page, in page order:

    #dreamdocs-align 1	source=Docs/language/types.md	rev=d804ba9be050
    h2	3f9a1c2e7b40	a91c03d2	m

Columns: slot signature, English fingerprint, translation fingerprint, origin.
The English fingerprint is "-" when unknown and "~<key>" when assumed but never
checked (pages imported without their original English).
Origins: m machine, h human, c copied from an identical English slot, s source
text copied verbatim because nothing in it needs translating.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from . import inline
from .markdown import Doc, Slot

HEADER = "#dreamdocs-align 1"
ORIGINS = set("mhcs")


@dataclass
class Row:
    signature: str
    en: str
    zh: str
    origin: str

    @property
    def known(self) -> str | None:
        return None if self.en == "-" else self.en.lstrip("~")

    @property
    def verified(self) -> bool:
        return not self.en.startswith("~")


@dataclass
class Alignment:
    source: str
    rev: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    rows: list[Row] = field(default_factory=list)

    def dumps(self) -> str:
        head = [HEADER, f"source={self.source}"] + ([f"rev={self.rev}"] if self.rev else [])
        head += [f"{k}={v}" for k, v in sorted(self.extra.items())]
        lines = ["\t".join(head)] + ["\t".join((r.signature, r.en, r.zh, r.origin)) for r in self.rows]
        return "\n".join(lines) + "\n"

    @classmethod
    def loads(cls, text: str, where: str = "alignment") -> "Alignment":
        # A Windows checkout with core.autocrlf turns the table's line ends into CRLF.
        lines = text.replace("\r\n", "\n").lstrip("﻿").rstrip("\n").split("\n")
        head = lines[0].split("\t")
        if head[0] != HEADER:
            raise ValueError(f"{where}: not a dreamdocs alignment table")
        fields = dict(part.split("=", 1) for part in head[1:])
        table = cls(fields.pop("source", ""), fields.pop("rev", ""), fields)
        for number, line in enumerate(lines[1:], 2):
            parts = line.split("\t")
            if len(parts) != 4 or parts[3] not in ORIGINS:
                raise ValueError(f"{where}:{number}: expected signature, en, zh, origin")
            table.rows.append(Row(*parts))
        return table

    @classmethod
    def read(cls, path: Path) -> "Alignment":
        return cls.loads(path.read_text(encoding="utf-8"), str(path))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8", newline="\n")


def translatable(slot: Slot) -> bool:
    return any(inline.translatable(t) for t in slot.texts)


@dataclass
class SlotState:
    index: int                 # slot index in the current English page
    state: str                 # current | stale | missing | skip
    old: int | None = None     # translated slot that holds the previous translation, if any
    edited: bool = False       # that translation was changed by hand since it was recorded


@dataclass
class Comparison:
    states: list[SlotState]
    mismatch: str = ""         # why the translated page no longer matches its table
    edited: list[int] = field(default_factory=list)   # translated slots changed by hand

    def count(self, state: str) -> int:
        return sum(1 for s in self.states if s.state == state)


def compare(table: Alignment, english: Doc, translated: Doc) -> Comparison:
    """Line the current English page up with the translation the table describes."""
    signatures = [s.signature for s in translated.slots]
    recorded = [r.signature for r in table.rows]
    if signatures != recorded:
        first = next((i for i, (a, b) in enumerate(zip(signatures, recorded)) if a != b),
                     min(len(signatures), len(recorded)))
        return Comparison([], f"slot {first + 1}: page has {signatures[first] if first < len(signatures) else 'nothing'},"
                              f" table has {recorded[first] if first < len(recorded) else 'nothing'}")
    edited = [i for i, (slot, row) in enumerate(zip(translated.slots, table.rows))
              if slot.text_key != row.zh and row.origin != "s"]
    old = [row.known or f"-{i}" for i, row in enumerate(table.rows)]
    new = [slot.key for slot in english.slots]
    states: list[SlotState] = [SlotState(j, "missing") for j in range(len(new))]
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                verified = table.rows[i1 + k].verified
                states[j1 + k] = SlotState(j1 + k, "current" if verified else "stale", i1 + k)
        elif tag == "replace":
            for k in range(min(j2 - j1, i2 - i1)):
                states[j1 + k] = SlotState(j1 + k, "stale", i1 + k)  # Extra new slots stay missing.
    for s in states:
        if s.old is not None:
            s.edited = s.old in edited
        if not translatable(english.slots[s.index]):
            s.state = "skip"
    return Comparison(states, edited=edited)
