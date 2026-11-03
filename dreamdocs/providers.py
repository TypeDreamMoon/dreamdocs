"""HTTP clients for the three request formats: chat completions, responses and messages.

OpenCodeGo is a preset: it reads the subscription's public model list to learn
which format and address each model uses, and sends the User-Agent and
x-opencode-session headers its terms ask clients for.
"""
from __future__ import annotations

import email.utils
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__
from .cache import Cache
from .profiles import Profile

USER_AGENT = f"dreamdocs/{__version__} (+https://github.com/TypeDreamMoon/dreamdocs)"
OPENCODE_GO_MODELS = "https://opencode.ai/zen/go/v1/models"
DEFAULT_BASE = {"OpenAI": "https://api.openai.com/v1", "Anthropic": "https://api.anthropic.com/v1"}
API_NAMES = {"chat-completions": "chat", "responses": "responses", "messages": "messages"}


class ProviderError(ValueError):
    """A request failed; retrying the same request may help."""


class Busy(ProviderError):
    def __init__(self, message: str, wait: float | None = None):
        super().__init__(message)
        self.wait = wait


class Stop(ProviderError):
    """Every further request would fail the same way: no key, no balance, unknown model, used-up limit."""


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Endpoint:
    api: str
    base: str
    price: tuple[float, float] | None = None   # USD per million input / output tokens, when known


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("the API redirected; configure the final BaseUrl (keys are not forwarded on redirects)")


def _opener(profile: Profile):
    handlers = [_NoRedirect()]
    if profile.proxy:
        handlers.append(urllib.request.ProxyHandler({"http": profile.proxy, "https": profile.proxy}))
    return urllib.request.build_opener(*handlers)


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"\s+", " ", text).strip()


def _retry_after(headers) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time()) if when else None


@dataclass
class Client:
    profile: Profile
    cache: Cache = field(default_factory=Cache)
    opener: object = None
    models_url: str = OPENCODE_GO_MODELS
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self):
        self.opener = self.opener or _opener(self.profile)
        self._endpoint: Endpoint | None = None

    # -- where to send -----------------------------------------------------
    def endpoint(self) -> Endpoint:
        if self._endpoint:
            return self._endpoint
        p = self.profile
        if p.type == "OpenCodeGo":
            self._endpoint = self._opencode_go_endpoint()
        else:
            api = p.api or ("messages" if p.type == "Anthropic" else "chat")
            self._endpoint = Endpoint(api, p.base_url or DEFAULT_BASE[p.type])
        return self._endpoint

    def _opencode_go_endpoint(self) -> Endpoint:
        p = self.profile
        data = self.cache.blob("opencode-go-models.json", max_age=24 * 3600)
        if data is None:
            request = urllib.request.Request(self.models_url, headers={"User-Agent": USER_AGENT})
            try:
                with self.opener.open(request, timeout=p.timeout) as response:
                    data = response.read()
                self.cache.keep_blob("opencode-go-models.json", data)
            except (urllib.error.URLError, OSError) as error:
                data = self.cache.blob("opencode-go-models.json", max_age=float("inf"))
                if data is None:
                    if p.api and p.base_url:
                        return Endpoint(p.api, p.base_url)
                    raise Stop(f"cannot read the OpenCode Go model list ({error}); set Api and BaseUrl in the profile") from None
        models = {m.get("id"): m for m in json.loads(data).get("data", [])}
        model = models.get(p.model)
        if model is None:
            raise Stop(f"OpenCode Go has no model {p.model!r}; it has {', '.join(sorted(k for k in models if k))}")
        apis = {API_NAMES[k]: v for k, v in (model.get("api") or {}).items() if k in API_NAMES}
        if not apis:
            raise Stop(f"OpenCode Go serves {p.model} only through {', '.join(model.get('api') or {})}, which dreamdocs does not speak")
        api = p.api if p.api in apis else next(iter(apis))
        cost = (model.get("cost") or {}).get("text") or {}
        price = None
        try:
            price = (float(cost["input"]["price"]), float(cost["output"]["price"]))
        except (KeyError, TypeError, ValueError):
            pass
        return Endpoint(api, (p.base_url or apis[api]["base_url"]).rstrip("/"), price)

    # -- one request -------------------------------------------------------
    def complete(self, system: str, user: str, session: str = "") -> Reply:
        p, end = self.profile, self.endpoint()
        key = p.key()
        if not key and not re.match(r"https?://(localhost|127\.0\.0\.1|\[::1\])[:/]", end.base):
            where = f"environment variable {p.api_key_env}" if p.api_key_env else "ApiKey / ApiKeyEnv"
            raise Stop(f"profile {p.name!r} has no key: set {where}")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if key:
            headers["Authorization"] = "Bearer " + key
        temperature = p.temperature if p.temperature is not None else (None if end.api == "responses" else 0.2)
        if end.api == "chat":
            path = "/chat/completions"
            body = {"model": p.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if p.token_field != "omit":
                body[p.token_field] = p.max_tokens
            if p.json_mode:
                body["response_format"] = {"type": "json_object"}
        elif end.api == "responses":
            path = "/responses"
            body = {"model": p.model, "instructions": system, "input": user, "max_output_tokens": p.max_tokens, "store": False}
            if p.json_mode:
                body["text"] = {"format": {"type": "json_object"}}
        else:
            path = "/messages"
            body = {"model": p.model, "system": system, "max_tokens": p.max_tokens,
                    "messages": [{"role": "user", "content": user}]}
            if key:
                headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        if temperature is not None:
            body["temperature"] = temperature
        if p.type == "OpenCodeGo" and session:
            headers["x-opencode-session"] = session
        body.update(p.extra_body)
        headers.update(p.headers)
        request = urllib.request.Request(end.base + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        self.requests += 1
        try:
            with self.opener.open(request, timeout=p.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read(4000).decode("utf-8", "replace") if error.fp else ""
            error.close()
            detail = _redact(raw, p.secrets())[:300]
            message = f"{p.name}: HTTP {error.code} {detail}".rstrip()
            if error.code in (429, 500, 502, 503, 504, 529):
                raise Busy(message, _retry_after(error.headers)) from None
            if error.code in (400, 401, 402, 403, 404, 422):
                raise Stop(message) from None
            raise ProviderError(message) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise Busy(f"{p.name}: connection failed ({_redact(str(error), p.secrets())})") from None
        except ValueError:
            raise ProviderError(f"{p.name}: the reply is not JSON") from None
        return self._read(end.api, data)

    def _read(self, api: str, data: dict) -> Reply:
        name = self.profile.name
        try:
            if api == "chat":
                choice = data["choices"][0]
                reason = choice.get("finish_reason")
                if reason not in ("stop", "end_turn", None):
                    raise ProviderError(f"{name}: reply stopped early ({reason}); raise MaxTokens or lower ChunkChars")
                text = choice["message"].get("content") or ""
                usage = data.get("usage") or {}
                reply = Reply(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            elif api == "responses":
                if data.get("status") != "completed":
                    detail = (data.get("incomplete_details") or {}).get("reason", data.get("status"))
                    raise ProviderError(f"{name}: reply incomplete ({detail}); raise MaxTokens or lower ChunkChars")
                text = "".join(part.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                               for part in item.get("content", []) if part.get("type") == "output_text")
                usage = data.get("usage") or {}
                reply = Reply(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            else:
                if data.get("stop_reason") not in ("end_turn", "stop_sequence"):
                    raise ProviderError(f"{name}: reply stopped early ({data.get('stop_reason')}); raise MaxTokens or lower ChunkChars")
                text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
                usage = data.get("usage") or {}
                reply = Reply(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError(f"{name}: unexpected reply shape") from None
        if not reply.text.strip():
            raise ProviderError(f"{name}: empty reply")
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        return reply


def parse_json_reply(text: str) -> dict:
    """The first JSON object in a reply, tolerating code fences and chatter around it."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start = text.find("{")
    if start < 0:
        raise ProviderError("the reply holds no JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as error:
        raise ProviderError(f"the reply's JSON does not parse ({error.msg})") from None
    if not isinstance(value, dict):
        raise ProviderError("the reply's JSON is not an object")
    return value
