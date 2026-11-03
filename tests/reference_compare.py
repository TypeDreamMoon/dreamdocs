"""Compare the splitter with markdown-it-py, a CommonMark reference parser.

Development aid, not part of the tool: needs `pip install markdown-it-py==3.0.0`.

    python tests/reference_compare.py <folder> [<folder> ...]

Every heading, paragraph and table row markdown-it finds must be a slot with the
same kind, starting line and text, and the splitter must find nothing else.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from markdown_it import MarkdownIt  # noqa: E402

from dreamdocs.markdown import join_lines, normalize_newlines, parse, squash  # noqa: E402

MD = MarkdownIt("commonmark").enable("table")


def reference(src: str) -> list[tuple]:
    lines = src.split("\n")
    if lines and lines[0] == "---" and "---" in lines[1:]:
        close = lines.index("---", 1)
        src = "\n" * (close + 1) + "\n".join(lines[close + 1:])  # Keep line numbers; frontmatter is not Markdown.
    found, stack, row = [], [], None
    tokens = MD.parse(src)
    for n, token in enumerate(tokens):
        if token.type in ("bullet_list_open", "ordered_list_open", "blockquote_open"):
            stack.append(token.type)
        elif token.type in ("bullet_list_close", "ordered_list_close", "blockquote_close"):
            stack.pop()
        elif token.type == "tr_open":
            row = (token.map[0], [])
        elif token.type == "tr_close":
            found.append(("row", row[0], tuple(row[1])))
            row = None
        elif token.type == "inline":
            parent = tokens[n - 1].type
            text = squash(join_lines(line.strip() for line in token.content.split("\n")))
            if parent in ("th_open", "td_open"):
                row[1].append(text)
            elif parent == "heading_open":
                found.append(("h", tokens[n - 1].map[0], (text,)))
            elif parent == "paragraph_open":
                inner = stack[-1] if stack else "root"
                kind = {"root": "p", "blockquote_open": "q"}.get(inner, "li")
                line = tokens[n - 1].map[0]
                # Intended differences from plain CommonMark: a GitHub alert marker line and a
                # lone <a id> anchor line are skeleton, not prose.
                alert = re.match(r"^\[![A-Za-z]+\]\s*", token.content)
                if kind == "q" and alert and "\n" in token.content[:alert.end() + 1]:
                    rest = token.content[alert.end():]
                    text, line = squash(join_lines(part.strip() for part in rest.split("\n"))), line + 1
                if re.fullmatch(r'<a (?:id|name)="[^"<>]*"></a>', text):
                    continue
                if text:
                    found.append((kind, line, (text,)))
    return found


def ours(src: str) -> list[tuple]:
    doc = parse(src)
    out = []
    for slot in doc.slots:
        if slot.kind == "meta":
            continue
        line = src.count("\n", 0, slot.spans[0][0])
        texts = tuple(squash(t.replace("\\|", "|")) if slot.kind == "row" else squash(t) for t in slot.texts)
        out.append((slot.kind, line, texts))
    return out


def main(folders: list[str]) -> int:
    bad = total = 0
    for folder in folders:
        for path in sorted(Path(folder).rglob("*.md")):
            src = normalize_newlines(path.read_text(encoding="utf-8"))
            a, b = reference(src), ours(src)
            # markdown-it pads short rows with empty cells and drops extra ones; compare what both saw.
            b = [(k, l, t) for k, l, t in b]
            total += 1
            if a != b:
                bad += 1
                first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                ref = a[first] if first < len(a) else None
                got = b[first] if first < len(b) else None
                print(f"{path}: block {first + 1}\n   reference {str(ref)[:220]}\n   splitter  {str(got)[:220]}")
    print(f"{total} files, {bad} differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
