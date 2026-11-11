"""Where a plugin keeps its English manuals, their Chinese copies and the bookkeeping."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Navigation pages that only point elsewhere; the old site scripts skipped them by name.
LEGACY_EXCLUDE = ("Docs/DuiLanguage.md", "Docs/FontsAndPackaging.md", "Docs/Migration.md")
CONFIG_NAME = "Docs/dreamdocs.json"


@dataclass
class Plugin:
    root: Path
    target: str = "zh-CN"
    exclude: tuple[str, ...] = LEGACY_EXCLUDE
    codes: str = r"(?:DSH|DFX|DUI|DNS|DNA)\d{4}"
    manual: tuple[str, ...] = ()
    chinese_original: tuple[str, ...] = ()   # pages written in Chinese first; their English is the translation
    config: dict = field(default_factory=dict)

    @classmethod
    def open(cls, root: str | Path) -> "Plugin":
        root = Path(root).resolve()
        if not (root / "Docs").is_dir():
            raise ValueError(f"{root} has no Docs/ folder")
        config = {}
        path = root / CONFIG_NAME
        if path.exists():
            config = json.loads(path.read_text(encoding="utf-8"))
            if config.get("version") != 2:
                raise ValueError(f"{CONFIG_NAME}: unsupported version {config.get('version')!r}")
        targets = config.get("targets", ["zh-CN"])
        if len(targets) != 1:
            raise ValueError(f"{CONFIG_NAME}: exactly one target language is supported")
        plugin = cls(root, targets[0], tuple(config.get("exclude", LEGACY_EXCLUDE)), config=config,
                     manual=tuple(config.get("manual", ())),
                     chinese_original=tuple(config.get("chineseOriginal", ())))
        if config.get("codes"):
            plugin.codes = "(?:" + "|".join(config["codes"]) + ")"
        return plugin

    @property
    def mirror(self) -> Path:
        return self.root / "Docs" / self.target

    def pages(self) -> list[str]:
        """English originals, as paths relative to Docs/ (e.g. language/types.md).

        Pages written in Chinese first (chineseOriginal) are left out: their English is a translation.
        """
        docs = self.root / "Docs"
        found = []
        for path in sorted(docs.rglob("*.md")):
            name = path.relative_to(self.root).as_posix()
            relative = path.relative_to(docs).as_posix()
            first = relative.split("/", 1)[0]
            if first in (self.target, "Website") or first.startswith(".") or name in self.exclude \
                    or relative in self.chinese_original:
                continue
            found.append(relative)
        return found

    def reverse_pages(self) -> list[str]:
        """Pages whose original is the Chinese (chineseOriginal) and that have one."""
        return [page for page in self.chinese_original if (self.mirror / page).exists()]

    def directions(self) -> list["Direction"]:
        found = [Direction(self)]
        if self.chinese_original:
            found.append(Direction(self, reverse=True))
        return found

    def english(self, page: str) -> Path:
        return self.root / "Docs" / page

    def translation(self, page: str) -> Path:
        return self.mirror / page

    def alignment(self, page: str) -> Path:
        return self.mirror / ".align" / (page + ".tsv")

    def legacy_record(self, page: str) -> Path:
        return self.mirror / ".translations" / (page + ".json")

    def revision(self) -> str:
        """HEAD of the plugin checkout, or the .rev file of an exported tree."""
        exported = self.root / ".rev"
        if exported.exists():
            return exported.read_text(encoding="utf-8").strip()
        try:
            return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""


@dataclass(frozen=True)
class Direction:
    """Which file is the original and which the translation, for every page of one kind.

    Forward: Docs/<page> (English) -> Docs/<target>/<page>, table Docs/<target>/.align/<page>.tsv.
    Reverse (chineseOriginal pages): Docs/<target>/<page> -> Docs/<page>, table Docs/.align/<page>.tsv.
    """
    plugin: Plugin
    reverse: bool = False

    @property
    def source_lang(self) -> str:
        return self.plugin.target if self.reverse else "en"

    @property
    def target_lang(self) -> str:
        return "en" if self.reverse else self.plugin.target

    @property
    def label(self) -> str:
        return f"{self.source_lang} -> {self.target_lang}"

    def pages(self) -> list[str]:
        return self.plugin.reverse_pages() if self.reverse else self.plugin.pages()

    def source(self, page: str) -> Path:
        return self.plugin.mirror / page if self.reverse else self.plugin.root / "Docs" / page

    def translation(self, page: str) -> Path:
        return self.plugin.root / "Docs" / page if self.reverse else self.plugin.mirror / page

    def alignment(self, page: str) -> Path:
        if self.reverse:
            return self.plugin.root / "Docs" / ".align" / (page + ".tsv")
        return self.plugin.mirror / ".align" / (page + ".tsv")

    def translation_display(self, page: str) -> str:
        return f"Docs/{page}" if self.reverse else f"Docs/{self.plugin.target}/{page}"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
