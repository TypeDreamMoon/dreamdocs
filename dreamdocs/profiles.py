"""Translator profiles: where requests go, which model, which key.

Profiles live in the personal file DreamLoc already uses, so a key is set up
once for both tools: %APPDATA%/DreamLoc/translators.ini (DREAMLOC_TRANSLATORS_INI
overrides the path). Each [Profile <name>] section has DreamLoc's keys -- Type,
BaseUrl, Model, ApiKey / ApiKeyEnv, Proxy, JsonMode, Temperature, Local -- plus
the keys only dreamdocs reads: Api, MaxTokens, TokenField, ExtraBody, Headers,
Concurrency, Retry, ChunkChars, Timeout.

In CI the DOCS_AI_* environment variables make a profile named "env".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

TYPES = {"openai": "OpenAI", "anthropic": "Anthropic", "opencodego": "OpenCodeGo"}
APIS = ("chat", "responses", "messages")
DREAMLOC_ONLY = {"appid", "appidenv", "secret", "secretenv", "region"}


@dataclass
class Profile:
    name: str
    type: str = "OpenAI"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    proxy: str = ""
    json_mode: bool = False
    temperature: float | None = None   # None: the default (0.2 for chat and messages, left out for responses)
    api: str = ""
    max_tokens: int = 16000
    token_field: str = "max_tokens"
    extra_body: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    concurrency: int = 2
    retry: int = 2
    chunk_chars: int = 6000
    timeout: int = 180

    def key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""

    def secrets(self) -> list[str]:
        return [s for s in (self.key(),) if s]

    def describe(self) -> str:
        where = self.base_url or {"OpenCodeGo": "opencode.ai (Go)"}.get(self.type, "default address")
        return f"{self.name}: {self.type} {self.model or '(no model)'} at {where}"


def user_file() -> Path:
    override = os.environ.get("DREAMLOC_TRANSLATORS_INI")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "DreamLoc" / "translators.ini"


def _strip_comment(line: str) -> str:
    # The same rule as DreamLoc: a line starting with ; or #, or anything after " ;".
    if line.lstrip().startswith((";", "#")):
        return ""
    cut = line.find(" ;")
    return line if cut < 0 else line[:cut]


def _bool(value: str) -> bool | None:
    lowered = value.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    return None


def parse_profiles(text: str) -> tuple[dict[str, Profile], list[str]]:
    profiles: dict[str, Profile] = {}
    warnings: list[str] = []
    current: Profile | None = None
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section.startswith("Profile "):
                warnings.append(f"line {number}: [{section}] is not a [Profile <name>] section")
                current = None
                continue
            current = Profile(section[8:].strip())
            profiles[current.name] = current
            continue
        if "=" not in line or current is None:
            warnings.append(f"line {number}: expected key=value inside a [Profile <name>] section")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        lowered = key.lower()
        try:
            if lowered == "type":
                current.type = TYPES.get(value.lower(), value)
            elif lowered == "baseurl":
                current.base_url = value.rstrip("/")
            elif lowered == "model":
                current.model = value
            elif lowered == "apikey":
                current.api_key = value
            elif lowered == "apikeyenv":
                current.api_key_env = value
            elif lowered == "proxy":
                current.proxy = value
            elif lowered == "jsonmode":
                current.json_mode = bool(_bool(value))
            elif lowered == "temperature":
                current.temperature = None if value.lower() in ("", "none", "omit") else float(value)
            elif lowered == "api":
                if value not in APIS:
                    raise ValueError(f"Api must be one of {', '.join(APIS)}")
                current.api = value
            elif lowered == "maxtokens":
                current.max_tokens = int(value)
            elif lowered == "tokenfield":
                if value not in ("max_tokens", "max_completion_tokens", "omit"):
                    raise ValueError("TokenField must be max_tokens, max_completion_tokens or omit")
                current.token_field = value
            elif lowered in ("extrabody", "headers"):
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise ValueError(f"{key} must be a JSON object")
                setattr(current, "extra_body" if lowered == "extrabody" else "headers", parsed)
            elif lowered in ("concurrency", "retry", "chunkchars", "timeout"):
                attribute = {"chunkchars": "chunk_chars"}.get(lowered, lowered)
                setattr(current, attribute, max(0 if lowered == "retry" else 1, int(value)))
            elif lowered == "local" or lowered in DREAMLOC_ONLY:
                pass  # DreamLoc's; documents never go to a machine translator.
            else:
                warnings.append(f"line {number}: '{key}' is not read")
        except ValueError as error:
            warnings.append(f"line {number}: {key}: {error}")
    return profiles, warnings


def env_profile() -> Profile | None:
    """The CI profile from DOCS_AI_* variables, as the old site workflows set them."""
    if not os.environ.get("DOCS_AI_MODEL"):
        return None
    profile = Profile("env", "OpenAI", (os.environ.get("DOCS_AI_BASE_URL") or "").rstrip("/"),
                      os.environ["DOCS_AI_MODEL"], api_key_env="DOCS_AI_API_KEY")
    api = os.environ.get("DOCS_AI_API_FORMAT", "")
    profile.api = {"chat-completions": "chat", "responses": "responses", "messages": "messages"}.get(api, "chat")
    profile.json_mode = os.environ.get("DOCS_AI_JSON_MODE", "").lower() == "true"
    profile.token_field = os.environ.get("DOCS_AI_TOKEN_FIELD") or "max_tokens"
    if os.environ.get("DOCS_AI_EXTRA_BODY"):
        profile.extra_body = json.loads(os.environ["DOCS_AI_EXTRA_BODY"])
    for env, attribute in (("DOCS_AI_MAX_OUTPUT_TOKENS", "max_tokens"), ("DOCS_AI_CONCURRENCY", "concurrency"),
                           ("DOCS_AI_RETRIES", "retry"), ("DOCS_AI_CHUNK_CHARS", "chunk_chars"),
                           ("DOCS_AI_TIMEOUT", "timeout")):
        if os.environ.get(env):
            setattr(profile, attribute, int(os.environ[env]))
    return profile


def load(path: Path | None = None) -> tuple[dict[str, Profile], list[str]]:
    path = path or user_file()
    profiles, warnings = ({}, []) if not path.exists() else parse_profiles(path.read_text(encoding="utf-8"))
    env = env_profile()
    if env:
        profiles.setdefault("env", env)
    return profiles, warnings


def choose(name: str | None, configured: str | None = None, path: Path | None = None) -> Profile:
    profiles, _ = load(path)
    wanted = name or os.environ.get("DREAMDOCS_PROFILE") or configured or ("env" if "env" in profiles else None)
    if not wanted:
        raise ValueError(f"no translator chosen: pass --profile, or add one to {path or user_file()}")
    if wanted not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise ValueError(f"no profile named {wanted!r} in {path or user_file()} (profiles: {known})")
    profile = profiles[wanted]
    if profile.type not in TYPES.values():
        raise ValueError(f"profile {wanted!r} is a {profile.type} machine translator; documents need a language model "
                         f"(Type=OpenAI, Anthropic or OpenCodeGo)")
    if not profile.model:
        raise ValueError(f"profile {wanted!r} names no Model")
    return profile
