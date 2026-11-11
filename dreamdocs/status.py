"""Which slots of each translated page are current, stale or missing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .align import Alignment, Comparison, compare
from .markdown import digest, parse
from .migrate import untranslated
from .plugin import Direction, Plugin, read


@dataclass
class PageStatus:
    page: str
    state: str          # current | outdated | missing | untracked | mismatch | manual
    comparison: Comparison | None = None
    detail: str = ""
    reverse: bool = False   # the Chinese is the original and the English the translation


def page_status(plugin: Plugin, page: str, tables: Path | None = None, direction: Direction | None = None) -> PageStatus:
    direction = direction or Direction(plugin)
    lang = direction.source_lang
    english = parse(read(direction.source(page)))
    zh = direction.translation(page)
    table_path = (tables / (page + ".tsv")) if tables else direction.alignment(page)
    if not zh.exists():
        return PageStatus(page, "missing", untranslated(english, lang), reverse=direction.reverse)
    if not table_path.exists():
        return PageStatus(page, "untracked", detail="no alignment table; run dreamdocs migrate or adopt",
                          reverse=direction.reverse)
    table = Alignment.read(table_path)
    result = compare(table, english, parse(read(zh)), lang)
    if result.mismatch:
        return PageStatus(page, "mismatch", result, result.mismatch, reverse=direction.reverse)
    state = "current" if all(s.state in ("current", "skip") for s in result.states) else "outdated"
    detail = ""
    if state == "current" and table.extra.get("page") != digest(english.src)[:12]:
        # Every slot is current but code, tables or layout around them changed: rebuild, nothing to translate.
        state, detail = "outdated", "layout changed; dreamdocs translate rebuilds it without sending anything"
    if page in plugin.manual and state != "current":
        state = "manual"
    return PageStatus(page, state, result, detail, reverse=direction.reverse)


def plugin_status(plugin: Plugin, tables: Path | None = None) -> list[PageStatus]:
    """Every page in both directions: English originals, then Chinese originals (their English copy)."""
    rows = [page_status(plugin, page, tables) for page in plugin.pages()]
    for direction in plugin.directions()[1:]:
        rows += [page_status(plugin, page, None, direction) for page in direction.pages()]
    return rows
