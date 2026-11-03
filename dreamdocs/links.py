"""Relative links in a translated page, which lives one folder deeper than its English.

A link to another manual points at its translation when there is one, otherwise
at the English; links to images and other files are re-pointed at the same file.
Absolute links, site routes (/docs/...) and same-page anchors are left alone.
"""
from __future__ import annotations

import posixpath
import re
import urllib.parse

from .inline import CODE

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_LINK = re.compile(r"(\]\(<?)([^\s)>]+)(>?(?:\s+\"[^\"]*\")?\))")
_REFERENCE = re.compile(r"^(\s*\[[^\]]+\]:\s*<?)([^\s>]+)")
_ATTRIBUTE = re.compile(r"(\b(?:href|src)\s*=\s*)([\"'])([^\"']+)(\2)")


def _outside_code(text: str, convert) -> str:
    out, fence = [], None
    for line in text.split("\n"):
        marker = _FENCE.match(line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            out.append(line)
            continue
        if marker:
            fence = marker[1]
            out.append(line)
            continue
        if line.startswith("    "):
            out.append(line)
            continue
        pieces, pos = [], 0
        for m in CODE.finditer(line):
            pieces.append(convert(line[pos:m.start()]))
            pieces.append(m.group(0))
            pos = m.end()
        pieces.append(convert(line[pos:]))
        out.append("".join(pieces))
    return "\n".join(out)


def _rewrite(text: str, base_dir: str, new_dir: str, retarget) -> str:
    """Re-point every relative link written from base_dir so it works from new_dir."""
    def move(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
            return url
        resolved = posixpath.normpath(posixpath.join(base_dir, urllib.parse.unquote(parts.path)))
        if resolved.startswith("../") or resolved == "..":
            return url  # Leaves the plugin; nothing sensible to re-point.
        destination = retarget(resolved)
        relative = posixpath.relpath(destination, new_dir)
        return urllib.parse.urlunsplit(("", "", urllib.parse.quote(relative, safe="/"), parts.query, parts.fragment))

    def convert(segment: str) -> str:
        segment = _INLINE_LINK.sub(lambda m: m[1] + move(m[2]) + m[3], segment)
        segment = _REFERENCE.sub(lambda m: m[1] + move(m[2]), segment)
        return _ATTRIBUTE.sub(lambda m: m[1] + m[2] + move(m[3]) + m[4], segment)

    return _outside_code(text, convert)


def relocate(text: str, page: str, target: str, translated: set[str]) -> str:
    """Rewrite the links of a translation of Docs/<page>, written as in the English, for Docs/<target>/<page>.

    translated holds the pages (relative to Docs/) that have, or are getting, a translation.
    A link to the page's own English (a language switch) keeps pointing at the English.
    """
    def retarget(resolved: str) -> str:
        if resolved.startswith("Docs/") and resolved[5:] in translated and resolved != f"Docs/{page}":
            return f"Docs/{target}/{resolved[5:]}"
        return resolved

    return _rewrite(text, posixpath.dirname("Docs/" + page), posixpath.dirname(f"Docs/{target}/{page}"), retarget)


def to_source(text: str, page: str, target: str, english_exists) -> str:
    """The inverse of relocate: links of an existing translation, written as the English page writes them.

    A link to a translated page goes back to its English, unless that English does not
    exist (a file only the translation folder has).
    """
    prefix = f"Docs/{target}/"

    def retarget(resolved: str) -> str:
        if resolved.startswith(prefix) and english_exists(resolved[len(prefix):]):
            return "Docs/" + resolved[len(prefix):]
        return resolved

    return _rewrite(text, posixpath.dirname(f"Docs/{target}/{page}"), posixpath.dirname("Docs/" + page), retarget)
