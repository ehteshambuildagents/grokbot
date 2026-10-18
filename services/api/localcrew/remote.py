"""Optional hosted-model provider: any OpenAI-compatible Chat Completions API (OpenAI, OpenRouter, Groq, Together,
DeepSeek, Mistral, LM Studio, vLLM, ...).

Disabled unless LOCALCREW_REMOTE_PROVIDERS_ENABLED=true and a base URL + API key are configured. When used, these
leave the machine: the bot's instructions, the conversation, retrieved memories, and tool results (page text, file
contents, connector data). The API key comes from the environment, is redacted from all logs/events, and is never
put in a prompt. Bots select it with model names like "remote:gpt-4o-mini".
"""
import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .config import settings
from .llm import Cancelled, Caps, ChatResult, ModelError
from .security import Redactor

PREFIX = "remote:"
EGRESS = ("bot instructions and role", "the conversation and task objective", "retrieved memories",
          "tool results (web page text, workspace file contents, connector data)", "deliverable drafts")


class RemoteConfigError(ValueError):
    """Not configured or misconfigured: fail the task with a clear message instead of retrying."""


def model_id(model: str) -> str:
    return model[len(PREFIX):] if model.startswith(PREFIX) else model


def check_config():
    s = settings()
    if not s.remote_providers_enabled:
        raise RemoteConfigError("remote model APIs are disabled (set LOCALCREW_REMOTE_PROVIDERS_ENABLED=true; "
                                "data then leaves this machine, see docs/MODELS.md)")
    if not s.remote_base_url or not s.remote_api_key:
        raise RemoteConfigError("set LOCALCREW_REMOTE_BASE_URL and LOCALCREW_REMOTE_API_KEY in .env")
    u = urlsplit(s.remote_base_url)
    if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RemoteConfigError("refusing to send an API key over plain http to a non-local host; use https")
    Redactor.register(s.remote_api_key)


def to_openai(messages: list[dict]) -> list[dict]:
    """Convert LocalCrew's checkpoint messages (Ollama style) to Chat Completions format with tool-call ids.
    Tool results whose call was compacted away become user messages, so the request stays valid."""
    out, pending = [], []

    def close_pending():
        while pending:
            out.append({"role": "tool", "tool_call_id": pending.pop(0), "content": "(no result recorded)"})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "tool":
            if pending:
                out.append({"role": "tool", "tool_call_id": pending.pop(0), "content": m.get("content", "")})
            else:
                out.append({"role": "user", "content": f"[Result of tool {m.get('tool_name', '')}]\n{m.get('content', '')}"})
            continue
        close_pending()
        if role == "assistant" and m.get("tool_calls"):
            ids = [f"call_{i}_{j}" for j in range(len(m["tool_calls"]))]
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": cid, "type": "function", "function": {
                    "name": c["function"]["name"], "arguments": json.dumps(c["function"].get("arguments") or {})}}
                for cid, c in zip(ids, m["tool_calls"], strict=True)]})
            pending = list(ids)
        else:
            out.append({"role": role, "content": m.get("content", "")})
    close_pending()
    return out


class OpenAICompatibleProvider:
    name = "remote"

    def health(self) -> dict:
        try:
            check_config()
            r = self._request("GET", "/models", None, timeout=10)
            data = json.loads(r)
            return {"ok": True, "models": [m.get("id") for m in data.get("data", [])][:50]}
        except (RemoteConfigError, ModelError, OSError, ValueError) as e:
            return {"ok": False, "error": Redactor.text(str(e))}

    def capabilities(self, model: str) -> Caps:
        # Hosted chat models used here support tool calling; images are never sent, so vision stays False.
        return Caps(model=model, tools=True, vision=False, context_length=settings().remote_context_length)

    def _conn(self, timeout: float):
        u = urlsplit(settings().remote_base_url)
        if u.scheme == "https":
            return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                               context=ssl.create_default_context()), u.path.rstrip("/")
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout), u.path.rstrip("/")

    def _headers(self):
        return {"Authorization": f"Bearer {settings().remote_api_key}", "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json", "User-Agent": "LocalCrew"}

    def _request(self, method, path, body, timeout=30) -> bytes:
        conn, base = self._conn(timeout)
        try:
            conn.request(method, base + path, body=json.dumps(body) if body is not None else None, headers=self._headers())
            r = conn.getresponse()
            data = r.read()
            if r.status >= 400:
                raise ModelError(f"remote API {r.status}: {Redactor.text(data[:300].decode('utf-8', 'replace'))}")
            return data
        finally:
            conn.close()

    def chat(self, model, messages, tools=None, fmt=None, on_delta=None, cancel=None, timeout=None, options=None):
        check_config()
        s, options = settings(), dict(options or {})
        body = {"model": model_id(model), "messages": to_openai(messages), "stream": True}
        if tools:
            body["tools"] = tools
        if fmt and s.remote_json_mode:
            body["response_format"] = {"type": "json_object"}
        if "num_predict" in options:
            body["max_tokens"] = options["num_predict"]
        for k in ("temperature", "top_p"):
            if k in options:
                body[k] = options[k]
        deadline = time.monotonic() + (timeout or s.model_timeout_s)
        stop, why = threading.Event(), {}
        conn, base = self._conn(60)

        def watchdog():  # cancellation/deadline even while waiting for the first byte
            while not stop.wait(0.2):
                if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                    why["reason"] = "cancel" if cancel is not None and cancel.is_set() else "timeout"
                    try:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
                    return

        out, calls, usage = [], {}, {}
        try:
            conn.connect()
            conn.sock.settimeout(None)
            threading.Thread(target=watchdog, daemon=True).start()
            conn.request("POST", base + "/chat/completions", body=json.dumps(body), headers=self._headers())
            r = conn.getresponse()
            if r.status >= 400:
                detail = Redactor.text(r.read(500).decode("utf-8", "replace"))
                if r.status in (408, 409, 429) or r.status >= 500:
                    raise ModelError(f"remote API {r.status} (will retry): {detail}")
                raise RemoteConfigError(f"remote API {r.status}: {detail}")  # bad key/model/request: no retry
            while not why:
                raw = r.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise ModelError(f"remote API error: {Redactor.text(str(chunk['error']))[:300]}")
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        out.append(delta["content"])
                        if on_delta:
                            on_delta(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        acc = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        acc["name"] += fn.get("name") or ""
                        acc["arguments"] += fn.get("arguments") or ""
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
            if not why:
                raise ModelError(f"remote API unreachable or bad response: {Redactor.text(str(e))}") from e
        finally:
            stop.set()
            conn.close()
        if why.get("reason") == "cancel":
            raise Cancelled()
        if why.get("reason") == "timeout":
            raise ModelError("model timeout")
        parsed = []
        for _, c in sorted(calls.items()):
            try:
                args = json.loads(c["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": c["arguments"]}
            parsed.append({"name": c["name"], "arguments": args})
        return ChatResult("".join(out), parsed, {k: usage[k] for k in ("prompt_tokens", "completion_tokens") if k in usage})
