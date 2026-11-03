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
                     manual=tuple(config.get("manual", ())))
        if config.get("codes"):
            plugin.codes = "(?:" + "|".join(config["codes"]) + ")"
        return plugin

    @property
    def mirror(self) -> Path:
        return self.root / "Docs" / self.target

    def pages(self) -> list[str]:
        """English manuals, as paths relative to Docs/ (e.g. language/types.md)."""
        docs = self.root / "Docs"
        found = []
        for path in sorted(docs.rglob("*.md")):
            name = path.relative_to(self.root).as_posix()
            relative = path.relative_to(docs).as_posix()
            if relative.split("/", 1)[0] in (self.target, "Website") or name in self.exclude:
                continue
            found.append(relative)
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


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
