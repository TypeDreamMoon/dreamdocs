"""The local cache: translations already paid for, so an interrupted run resumes for free.

The default folder is .dreamdocs-cache in the tool checkout (git-ignored), so it
sits with the tool rather than on the system drive; DREAMDOCS_CACHE overrides it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path


def folder() -> Path:
    override = os.environ.get("DREAMDOCS_CACHE")
    if override:
        return Path(override)
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / "pyproject.toml").exists() and os.access(checkout, os.W_OK):
        return checkout / ".dreamdocs-cache"
    return Path(os.path.expanduser("~")) / ".cache" / "dreamdocs"


class Cache:
    def __init__(self, root: Path | None = None, enabled: bool = True):
        self.root = root or folder()
        self.enabled = enabled

    def _path(self, model: str, target: str, key: str) -> Path:
        name = re.sub(r"[^\w.-]+", "_", model) or "model"
        return self.root / "translations" / target / name / key[:2] / f"{key}.json"

    def get(self, model: str, target: str, key: str) -> list[str] | None:
        if not self.enabled:
            return None
        path = self._path(model, target, key)
        try:
            return json.loads(path.read_text(encoding="utf-8"))["texts"]
        except (OSError, ValueError, KeyError):
            return None

    def put(self, model: str, target: str, key: str, texts: list[str]) -> None:
        if not self.enabled:
            return
        path = self._path(model, target, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"texts": texts, "at": int(time.time())}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def blob(self, name: str, max_age: float) -> bytes | None:
        path = self.root / name
        try:
            if time.time() - path.stat().st_mtime <= max_age:
                return path.read_bytes()
        except OSError:
            pass
        return None

    def keep_blob(self, name: str, data: bytes) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def job_key(slot_key: str, masked: list[str | None]) -> str:
    """What a cached translation is filed under: the English fingerprint plus what was actually sent."""
    return hashlib.sha256((slot_key + "\0" + json.dumps(masked, ensure_ascii=False)).encode("utf-8")).hexdigest()[:24]
