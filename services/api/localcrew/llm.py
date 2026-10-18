"""Model provider layer. Ollama is the only real provider; remote adapters are not implemented
(capability matrix: unavailable). The scripted provider is a test MOCK, gated by config."""
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

import httpx

from .config import settings


class ModelError(Exception):
    """Transient model failure (connection, timeout). Tasks retry with backoff."""


class Cancelled(Exception):
    pass


@dataclass
class Caps:
    model: str
    tools: bool = False
    vision: bool = False
    audio: bool = False
    structured: bool = True
    context_length: int = 4096


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"name":..., "arguments": {...}}]
    usage: dict = field(default_factory=dict)


class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str | None = None):
        self.url = (url or settings().ollama_url).rstrip("/")

    def health(self) -> dict:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=3)
            r.raise_for_status()
            return {"ok": True, "models": [m["name"] for m in r.json().get("models", [])]}
        except httpx.HTTPError as e:
            return {"ok": False, "error": str(e)}

    def capabilities(self, model: str) -> Caps:
        return _ollama_caps(self.url, model)

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None, fmt: dict | None = None,
             on_delta: Callable[[str], None] | None = None, cancel: threading.Event | None = None,
             timeout: float | None = None, options: dict | None = None) -> ChatResult:
        caps = self.capabilities(model)
        body = {"model": model, "messages": messages, "stream": True,
                "options": {"num_ctx": min(caps.context_length, 8192), **(options or {})}}
        if tools:
            if not caps.tools:
                raise ValueError(f"model {model} does not support tool calling")
            body["tools"] = tools
        if fmt:
            body["format"] = fmt
        out, calls, usage = [], [], {}
        deadline = time.monotonic() + (timeout or settings().model_timeout_s)
        stop, why = threading.Event(), {}
        u = urlsplit(self.url)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=60)

        def watchdog():
            """Cancellation/deadline must also work while Ollama is still evaluating the prompt (it sends no
            headers until the first token): shut the socket down. Ollama aborts generation on disconnect."""
            while not stop.wait(0.2):
                if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                    why["reason"] = "cancel" if cancel is not None and cancel.is_set() else "timeout"
                    try:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
                    return

        try:
            conn.connect()
            conn.sock.settimeout(None)  # the watchdog enforces cancellation and the deadline
            threading.Thread(target=watchdog, daemon=True).start()
            conn.request("POST", "/api/chat", body=json.dumps(body), headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            if r.status >= 400:
                raise ModelError(f"ollama {r.status}: {r.read(300).decode('utf-8', 'replace')}")
            while not why:
                line = r.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    if why:  # Ollama reports our own disconnect as "context canceled"
                        break
                    raise ModelError(chunk["error"])
                msg = chunk.get("message") or {}
                if msg.get("content"):
                    out.append(msg["content"])
                    if on_delta:
                        on_delta(msg["content"])
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    calls.append({"name": fn.get("name"), "arguments": args or {}})
                if chunk.get("done"):
                    usage = {k: chunk.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration",
                                                       "eval_duration") if k in chunk}
        except (OSError, http.client.HTTPException, ValueError) as e:
            if not why:
                raise ModelError(f"ollama unreachable or bad response: {e}") from e
        finally:
            stop.set()
            conn.close()
        if why.get("reason") == "cancel":
            raise Cancelled()
        if why.get("reason") == "timeout":
            raise ModelError("model timeout")
        return ChatResult("".join(out), calls, usage)


@lru_cache(maxsize=64)
def _ollama_caps(url: str, model: str) -> Caps:
    try:
        r = httpx.post(f"{url}/api/show", json={"model": model}, timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ModelError(f"cannot inspect model {model}: {e}") from e
    info = r.json()
    caps = set(info.get("capabilities") or [])
    ctx = next((v for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), 4096)
    return Caps(model=model, tools="tools" in caps, vision="vision" in caps, audio="audio" in caps,
                context_length=int(ctx))


class ScriptedProvider:
    """MOCK provider for deterministic tests (never selectable unless LOCALCREW_ALLOW_SCRIPTED_MODEL=1).
    Stateless: the reply is scripts[key][n] where n = number of assistant turns already in the conversation,
    so it behaves identically after a worker crash and resume. Scripts come from the class attribute or the
    JSON file named by LOCALCREW_SCRIPT_FILE (for subprocess workers). Step fields: content, tool_calls,
    delay (seconds, cancellable), raise (ModelError message)."""
    name = "scripted"
    scripts: dict[str, list[dict]] = {}

    def health(self):
        return {"ok": True, "models": ["scripted:mock"], "mock": True}

    def capabilities(self, model):
        return Caps(model=model, tools=True, context_length=8192)

    def _scripts(self):
        import os
        f = os.environ.get("LOCALCREW_SCRIPT_FILE")
        if f and os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
        return type(self).scripts

    def chat(self, model, messages, tools=None, fmt=None, on_delta=None, cancel=None, timeout=None, options=None):
        if fmt is not None:
            return ChatResult(json.dumps({"summary": "scripted plan", "steps": ["follow the script"]}), [], {"mock": True})
        key = model.split(":", 1)[1] if ":" in model else "default"
        turns = self._scripts().get(key, [])
        idx = sum(1 for m in messages if m.get("role") == "assistant")
        step = turns[idx] if idx < len(turns) else {"content": "done"}
        start = time.monotonic()
        while time.monotonic() < start + float(step.get("delay", 0)):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if timeout is not None and time.monotonic() - start > timeout:
                raise ModelError("model timeout")
            time.sleep(0.05)
        if step.get("raise"):
            raise ModelError(step["raise"])
        if on_delta and step.get("content"):
            on_delta(step["content"])
        return ChatResult(step.get("content", ""), step.get("tool_calls", []), {"mock": True})


def provider_for(model: str):
    if model.startswith("scripted:"):
        if not settings().allow_scripted_model:
            raise ValueError("scripted MOCK model is disabled")
        return ScriptedProvider()
    if model.startswith("remote:"):
        from .remote import OpenAICompatibleProvider, check_config
        check_config()  # disabled/unconfigured → clear error, task fails instead of silently going local
        return OpenAICompatibleProvider()
    return OllamaProvider()


_model_sem = None


def model_slot():
    """Global concurrency limit for local inference (CPU host: 1)."""
    global _model_sem
    if _model_sem is None:
        _model_sem = threading.BoundedSemaphore(settings().model_concurrency)
    return _model_sem
