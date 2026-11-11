"""Inline spans inside a slot that the translator must leave alone.

Within one slot the frozen spans become short numbered placeholders, ⟦1⟧ ⟦2⟧ ...,
numbered from 1 in every slot. Link text is prose and stays translatable; only
the destination is frozen.
"""
from __future__ import annotations

import collections
import hashlib
import re

PLACEHOLDER = re.compile(r"⟦(\d+)⟧")
CODE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
AUTOLINK = re.compile(r"<(?:https?|mailto):[^\s<>]+>")
HTML = re.compile(r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
LINK_DESTINATION = re.compile(r"(?<=\])(?:\([^()\s]*(?:\([^()\s]*\)[^()\s]*)*(?:\s+\"[^\"]*\")?\)|\[[^\]]*\])")
URL = re.compile(r"https?://[^\s<>\"⟦]+?(?=[)\s<>\"⟦]|[.,;:!?](?:\s|$)|$)")
BRACES = re.compile(r"\{[^{}]*\}")
EXPLICIT_ID = re.compile(r"\[#[-\w]+\]")
WORD = re.compile(r"[A-Za-z]{2,}")


def mask(text: str, codes: str = r"(?:DSH|DFX|DUI|DNS|DNA)\d{4}") -> tuple[str, list[str]]:
    """Return (masked text, frozen values); values[n - 1] is placeholder ⟦n⟧."""
    values: list[str] = []

    def hold(match: re.Match) -> str:
        values.append(match.group(0))
        return f"⟦{len(values)}⟧"

    if PLACEHOLDER.search(text):
        raise ValueError("source prose already contains a ⟦n⟧ placeholder")
    for pattern in (CODE, AUTOLINK, HTML, LINK_DESTINATION, URL, BRACES, EXPLICIT_ID, re.compile(r"\b" + codes + r"\b")):
        text = pattern.sub(hold, text)
    return text, values


def unmask(text: str, values: list[str]) -> str:
    return PLACEHOLDER.sub(lambda m: values[int(m[1]) - 1], text)


def check_placeholders(masked: str, translated: str) -> str | None:
    """Return why a translation broke its placeholders, or None when they are intact."""
    want = collections.Counter(PLACEHOLDER.findall(masked))
    got = collections.Counter(PLACEHOLDER.findall(translated))
    if want == got:
        return None
    missing = sorted((want - got).elements(), key=int)
    extra = sorted((got - want).elements(), key=int)
    parts = []
    if missing:
        parts.append("missing " + " ".join(f"⟦{n}⟧" for n in missing))
    if extra:
        parts.append("unexpected " + " ".join(f"⟦{n}⟧" for n in extra))
    return "; ".join(parts)


CJK = re.compile(r"[㐀-鿿豈-﫿぀-ヿ가-힯]")


def translatable(text: str, lang: str = "en") -> bool:
    """Whether anything is left to translate once the frozen spans are taken out.

    English needs a word of two letters; Chinese (or any CJK source) needs a CJK character.
    """
    masked, _ = mask(text)
    rest = PLACEHOLDER.sub(" ", masked)
    return bool((WORD if lang == "en" else CJK).search(rest))


def anchor_tokens(text: str) -> str:
    """Fingerprint of the code spans a faithful translation must keep verbatim.

    Link destinations are left out: translated pages may point at the Chinese copy.
    Only the code is compared, not its backticks: a model that wrapped a protected
    `x` in backticks again produced ``x``, which renders the same.
    """
    tokens = sorted(m.group(2).strip() for m in CODE.finditer(text))
    return hashlib.sha256("\x1f".join(tokens).encode("utf-8")).hexdigest()[:6] if tokens else ""
