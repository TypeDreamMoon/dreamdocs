"""Split a Markdown page into skeleton and slots.

A slot is a run of prose the translator may replace: a frontmatter title or
description, a heading, a paragraph, or the cells of a table row. Everything
else -- markers, indentation, pipes, fences, code, HTML, anchors, blank lines --
is skeleton and is copied from the source page unchanged.

The parser covers the CommonMark/GFM block grammar the plugin manuals use.
Inline markup stays inside the slot text; see inline.py for what is protected
from translation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
ATX = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)")
THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
QUOTE = re.compile(r"^ {0,3}> ?")
LIST = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)")
SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
DELIMITER = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
ANCHOR = re.compile(r'^ {0,3}<a (?:id|name)="[^"<>]*"></a>[ \t]*$')
COMMENT = re.compile(r"^ {0,3}<!--")
HTML_RAW = re.compile(r"^ {0,3}<(script|pre|style|textarea)(?=[\s>]|$)", re.I)
HTML_BLOCK = re.compile(
    r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
    r"dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|"
    r"iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|source|summary|"
    r"table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?=[\s/>]|$)", re.I)
HTML_TAG_LINE = re.compile(
    r"""^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*"""
    r"""\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)[ \t]*$""")
ALERT = re.compile(r"^ {0,3}\[![A-Za-z]+\][ \t]*$")
CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
EXPLICIT_ID = re.compile(r"[ \t]*\[#[-\w]+\][ \t]*$")
FRONTMATTER_FIELD = re.compile(r"^(title|description):[ \t]*(.*?)[ \t]*$")

PROSE = ("p", "li", "q")


@dataclass
class Slot:
    """One translatable unit. A table row has one span per cell; anything else has one span."""
    kind: str                      # meta | h | p | li | q | row
    spans: list[tuple[int, int]]   # absolute [start, end) offsets in Doc.src
    texts: list[str]               # prose of each span, source lines joined by one space
    level: int = 0                 # heading level
    name: str = ""                 # meta: frontmatter field name
    quote: str = ""                # meta: quote character around the value, if any
    setext: bool = False           # h: underlined heading, whose text parses like a paragraph

    @property
    def group(self) -> str:
        return "prose" if self.kind in PROSE else self.kind

    @property
    def signature(self) -> str:
        """Structural identity used to line a translation up with its source."""
        if self.kind == "row":
            return f"row{len(self.spans)}"
        if self.kind == "h":
            return f"h{self.level}"
        if self.kind == "meta":
            return f"meta:{self.name}"
        return self.kind

    @property
    def key(self) -> str:
        """Content fingerprint of the source prose (12 hex digits)."""
        return digest(self.group + "\0" + "\x1f".join(squash(t) for t in self.texts))[:12]

    @property
    def text_key(self) -> str:
        """Fingerprint of a translation's text (8 hex digits), blind to all whitespace.

        Telling a hand edit from an untouched translation must not depend on how wrapped
        lines are joined, which differs between languages.
        """
        return digest(self.group + "\0" + "\x1f".join(re.sub(r"\s+", "", t) for t in self.texts))[:8]


@dataclass
class Doc:
    src: str
    slots: list[Slot] = field(default_factory=list)

    def render(self, values: dict[int, list[str]] | None = None, inserts: dict[int, str] | None = None) -> str:
        """Rebuild the page; values maps a slot index to replacement text per span.

        inserts adds skeleton text at source offsets (before any slot starting there).
        """
        edits = [((at, at), text) for at, text in (inserts or {}).items()]
        for index, slot in enumerate(self.slots):
            replaced = (values or {}).get(index)
            for n, span in enumerate(slot.spans):
                if replaced is not None:
                    edits.append((span, replaced[n]))
        edits.sort(key=lambda e: (e[0][0], e[0][1] != e[0][0]))
        out, pos = [], 0
        for (start, end), text in edits:
            out.append(self.src[pos:start])
            out.append(text)
            pos = end
        out.append(self.src[pos:])
        return "".join(out)


_LEADING_BLOCK = re.compile(r"^(?:#{1,6}(?:[ \t]|$)|>|[-*+](?:[ \t]|$)|=+[ \t]*$|-+[ \t]*$|`{3}|~{3}|<|\[![A-Za-z]+\])")
_LEADING_ORDERED = re.compile(r"^(\d{1,9})([.)])(?=[ \t]|$)")
_YAML_PLAIN_UNSAFE = re.compile(r"^[\s\[\]{}&*!|>'\"%@`#,?:-]|: | #|[\s:]$")


def escape(slot: Slot, text: str) -> str:
    """Make one line of replacement prose safe to drop into the slot's span.

    The skeleton must parse the same way afterwards: a translation may not start
    a new block, close a table cell, or break the frontmatter.
    """
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text.strip())
    if slot.kind == "row":
        return re.sub(r"(?<!\\)\|", r"\\|", text)
    if slot.kind == "meta":
        if slot.quote == '"':
            return text.replace("\\", "\\\\").replace('"', '\\"')
        if slot.quote == "'":
            return text.replace("'", "''")
        if _YAML_PLAIN_UNSAFE.search(text):
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return text
    if slot.kind != "h" or slot.setext:  # After an ATX "## " marker any text is heading text.
        ordered = _LEADING_ORDERED.match(text)
        if ordered:
            text = ordered[1] + "\\" + text[len(ordered[1]):]
        elif _LEADING_BLOCK.match(text):
            text = "\\" + text
    if slot.kind == "h" and not slot.setext:
        text = re.sub(r"([ \t])(#+)[ \t]*$", lambda m: m[1] + "\\" + m[2], text)
    return text


_CJK = re.compile(r"[⺀-鿿가-힯豈-﫿︰-﹏＀-￯　-〿]")


def join_lines(lines) -> str:
    """Join wrapped source lines: a space, except where both sides are CJK (Chinese is not spaced).

    Emphasis and link brackets at the break are looked through: "以及\\n[模块]" joins as "以及[模块]".
    """
    out = ""
    for line in lines:
        if out and line:
            before, after = out.rstrip("*_])"), line.lstrip("*_[(")
            if not (before and after and _CJK.match(before[-1]) and _CJK.match(after[0])):
                out += " "
        out += line
    return out


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_newlines(text: str) -> str:
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def parse(text: str) -> Doc:
    src = normalize_newlines(text)
    parser = _Parser(src)
    lines, pos = [], 0
    for raw in src.split("\n"):
        lines.append((pos, pos + len(raw)))
        pos += len(raw) + 1
    if lines and lines[-1][0] == lines[-1][1] == len(src):
        lines.pop()  # The empty string after a final newline is not a line.
    start = parser.frontmatter(lines)
    parser.blocks(lines[start:], "root")
    parser.slots.sort(key=lambda s: s.spans[0][0])
    return Doc(src, parser.slots)


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.slots: list[Slot] = []

    def text(self, line) -> str:
        return self.src[line[0]:line[1]]

    # -- frontmatter -------------------------------------------------------
    def frontmatter(self, lines) -> int:
        if not lines or self.text(lines[0]) != "---":
            return 0
        for close in range(1, len(lines)):
            if self.text(lines[close]) == "---":
                break
        else:
            return 0
        for line in lines[1:close]:
            match = FRONTMATTER_FIELD.match(self.text(line))
            if not match or not match[2] or match[2][0] in ">|":
                continue
            value_start, value_end = line[0] + match.start(2), line[0] + match.end(2)
            value, quote = match[2], ""
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                quote, value_start, value_end = value[0], value_start + 1, value_end - 1
                value = value[1:-1]
                value = value.replace("''", "'") if quote == "'" else re.sub(r'\\(["\\])', r"\1", value)
            if value.strip():
                self.slots.append(Slot("meta", [(value_start, value_end)], [value], name=match[1], quote=quote))
        return close + 1

    # -- block dispatch ----------------------------------------------------
    def interrupts(self, t: str) -> bool:
        """Whether a line starts a block that may cut a paragraph short."""
        fence = FENCE.match(t)
        if fence and not (fence[2][0] == "`" and "`" in fence[3]):
            return True
        if ATX.match(t) or THEMATIC.match(t) or QUOTE.match(t) or COMMENT.match(t):
            return True
        if ANCHOR.match(t) or HTML_RAW.match(t) or HTML_BLOCK.match(t):
            return True
        item = LIST.match(t)
        if item and t[item.end():].strip():
            marker = item[2]
            return marker in "-*+" or marker[:-1] == "1"
        return False

    def table_start(self, header, delimiter) -> bool:
        h, d = self.text(header), self.text(delimiter)
        if "|" not in h or "|" not in d or not DELIMITER.match(d) or indent(h) >= 4:
            return False
        return len(self._cells(header)) == len(self._cells(delimiter))

    def blocks(self, lines, ctx: str) -> None:
        i, n, first = 0, len(lines), True
        while i < n:
            t = self.text(lines[i])
            if not t.strip():
                i += 1
                continue
            if ctx == "quote" and first and ALERT.match(t):
                i, first = i + 1, False
                continue
            first = False
            fence = FENCE.match(t)
            if fence and not (fence[2][0] == "`" and "`" in fence[3]):
                i = self._fence(lines, i, fence)
            elif indent(t) >= 4:
                i += 1
                while i < n and (not self.text(lines[i]).strip() or indent(self.text(lines[i])) >= 4):
                    i += 1
            elif COMMENT.match(t):
                i = self._until(lines, i, "-->", t.find("<!--") + 4)
            elif ANCHOR.match(t) or THEMATIC.match(t):
                i += 1
            elif HTML_RAW.match(t):
                i = self._until(lines, i, "</" + HTML_RAW.match(t)[1].lower(), 0, fold=True)
            elif HTML_BLOCK.match(t) or HTML_TAG_LINE.match(t):
                while i < n and self.text(lines[i]).strip():
                    i += 1
            elif ATX.match(t):
                self._atx(lines[i], len(ATX.match(t)[1]))
                i += 1
            elif QUOTE.match(t):
                i = self._quote(lines, i)
            elif LIST.match(t):
                i = self._item(lines, i, LIST.match(t))
            elif i + 1 < n and self.table_start(lines[i], lines[i + 1]):
                i = self._table(lines, i)
            else:
                i = self._paragraph(lines, i, ctx)

    # -- leaf blocks -------------------------------------------------------
    def _fence(self, lines, i, opening) -> int:
        char, size = opening[2][0], len(opening[2])
        close = re.compile(r"^ {0,3}" + re.escape(char) + "{" + str(size) + r",}[ \t]*$")
        i += 1
        while i < len(lines):
            if close.match(self.text(lines[i])):
                return i + 1
            i += 1
        return i

    def _until(self, lines, i, marker, offset, fold=False) -> int:
        while i < len(lines):
            t = self.text(lines[i])
            if (t.lower() if fold else t).find(marker, offset) >= 0:
                return i + 1
            i, offset = i + 1, 0
        return i

    def _atx(self, line, level) -> None:
        t = self.text(line)
        start = ATX.match(t).end()
        content = t[start:]
        content = EXPLICIT_ID.sub("", content)
        content = CLOSING_HASHES.sub("", content)
        lead = len(content) - len(content.lstrip())
        body = content.strip()
        if body:
            begin = line[0] + start + lead
            self.slots.append(Slot("h", [(begin, begin + len(body))], [body], level=level))

    def _paragraph(self, lines, i, ctx) -> int:
        para = [lines[i]]
        i += 1
        while i < len(lines):
            t = self.text(lines[i])
            if not t.strip():
                break
            underline = SETEXT.match(t)
            if underline:
                self._prose(para, "h", level=1 if underline[1][0] == "=" else 2)
                return i + 1
            if self.interrupts(t) or (i + 1 < len(lines) and self.table_start(lines[i], lines[i + 1])):
                break
            para.append(lines[i])
            i += 1
        self._prose(para, {"root": "p", "quote": "q", "item": "li"}[ctx])
        return i

    def _prose(self, para, kind, level=0) -> None:
        first, last = self.text(para[0]), self.text(para[-1])
        start = para[0][0] + indent(first)
        end = para[-1][1] - (len(last) - len(last.rstrip()))
        text = join_lines(self.text(line).strip() for line in para)
        self.slots.append(Slot(kind, [(start, end)], [text], level=level, setext=kind == "h"))

    def _cells(self, line) -> list[tuple[int, int]]:
        t = self.text(line)
        k = indent(t)
        if t[k:k + 1] == "|":
            k += 1
        cells, cur = [], k
        while k < len(t):
            if t[k] == "\\" and k + 1 < len(t):
                k += 2
                continue
            if t[k] == "|":
                cells.append((cur, k))
                cur = k + 1
            k += 1
        if t[cur:].strip() or not cells:
            cells.append((cur, len(t)))
        return cells

    def _table(self, lines, i) -> int:
        rows = [lines[i]]
        i += 2  # Header row, then the delimiter row stays in the skeleton.
        while i < len(lines):
            t = self.text(lines[i])
            if not t.strip() or self.interrupts(t):
                break
            rows.append(lines[i])
            i += 1
        width = len(self._cells(rows[0]))
        for row in rows:
            spans, texts = [], []
            # GFM renders as many cells as the header has; extra cells are dropped, so they stay skeleton.
            for a, b in self._cells(row)[:width]:
                raw = self.text(row)[a:b]
                lead = len(raw) - len(raw.lstrip())
                body = raw.strip()
                start = row[0] + a + lead
                spans.append((start, start + len(body)))
                texts.append(body)
            self.slots.append(Slot("row", spans, texts))
        return i

    # -- containers --------------------------------------------------------
    def _lazy(self, inner, t) -> bool:
        """A paragraph continuation line that kept less indentation than its container."""
        return bool(inner) and bool(self.text(inner[-1]).strip()) and not self.interrupts(t) \
            and not LIST.match(t) and not FENCE.match(self.text(inner[-1]))

    def _quote(self, lines, i) -> int:
        inner = []
        while i < len(lines):
            t = self.text(lines[i])
            marker = QUOTE.match(t)
            if marker:
                inner.append((lines[i][0] + marker.end(), lines[i][1]))
            elif t.strip() and self._lazy(inner, t):
                inner.append(lines[i])
            else:
                break
            i += 1
        self.blocks(inner, "quote")
        return i

    def _item(self, lines, i, marker) -> int:
        t = self.text(lines[i])
        gap = marker[3]
        width = len(marker[1]) + len(marker[2]) + (1 if not gap or len(gap) > 4 else len(gap))
        inner = [(lines[i][0] + min(width, len(t)), lines[i][1])]
        i += 1
        while i < len(lines):
            t = self.text(lines[i])
            if not t.strip():
                inner.append((lines[i][1], lines[i][1]))
            elif indent(t) >= width:
                inner.append((lines[i][0] + width, lines[i][1]))
            elif self._lazy(inner, t):
                inner.append(lines[i])
            else:
                break
            i += 1
        while len(inner) > 1 and not self.text(inner[-1]).strip():
            inner.pop()
            i -= 1
        self.blocks(inner, "item")
        return i
