"""Which slots of each translated page are current, stale or missing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .align import Alignment, Comparison, compare
from .markdown import parse
from .migrate import untranslated
from .plugin import Plugin, read


@dataclass
class PageStatus:
    page: str
    state: str          # current | outdated | missing | untracked | mismatch | manual
    comparison: Comparison | None = None
    detail: str = ""


def page_status(plugin: Plugin, page: str, tables: Path | None = None) -> PageStatus:
    english = parse(read(plugin.english(page)))
    zh = plugin.translation(page)
    table_path = (tables / (page + ".tsv")) if tables else plugin.alignment(page)
    if not zh.exists():
        return PageStatus(page, "missing", untranslated(english))
    if not table_path.exists():
        return PageStatus(page, "untracked", detail="no alignment table; run dreamdocs migrate")
    result = compare(Alignment.read(table_path), english, parse(read(zh)))
    if result.mismatch:
        return PageStatus(page, "mismatch", result, result.mismatch)
    state = "current" if all(s.state in ("current", "skip") for s in result.states) else "outdated"
    if page in plugin.manual and state != "current":
        state = "manual"
    return PageStatus(page, state, result)


def plugin_status(plugin: Plugin, tables: Path | None = None) -> list[PageStatus]:
    return [page_status(plugin, page, tables) for page in plugin.pages()]
