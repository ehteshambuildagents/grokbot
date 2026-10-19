"""MCP client hub inside the computer service (the execution boundary).

Any number of MCP servers can be connected (one LocalCrew connection each). Transports: stdio (the server runs as a
child process under the terminal sandbox limits, with a scrubbed environment) and Streamable HTTP. Each session is
owned by one long-lived task (anyio requires a transport to be closed by the task that opened it). Sessions are
reused across calls and restarted when the connection's configuration changes.
"""
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

LAUNCHER = Path(__file__).with_name("mcp_launch.py")


def _root_cause(e: BaseException) -> str:
    """anyio wraps failures in ExceptionGroups; report the innermost real error."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


@dataclass
class Session:
    digest: str
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    client: object = None
    error: str = ""


class MCPHub:
    def __init__(self, memory_mb: int = 1024, max_procs: int = 64):
        self.sessions: dict[str, Session] = {}
        self.calls: dict[str, set[asyncio.Task]] = {}  # task_id → in-flight MCP calls (for cancellation)
        self.memory_mb, self.max_procs = memory_mb, max_procs

    @staticmethod
    def digest(conn: dict) -> str:
        return hashlib.sha256(json.dumps(conn, sort_keys=True).encode()).hexdigest()

    def _stdio_params(self, conn: dict):
        from mcp import StdioServerParameters
        base = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "PATHEXT", "COMSPEC", "LANG") if k in os.environ}
        home = Path(conn["home"])
        home.mkdir(parents=True, exist_ok=True)
        env = {**base, "HOME": str(home), "USERPROFILE": str(home), "TEMP": str(home), "TMP": str(home),
               **{str(k): str(v) for k, v in (conn.get("env") or {}).items()}}
        argv = [sys.executable, str(LAUNCHER), str(self.memory_mb), str(self.max_procs), "--", conn["command"],
                *conn.get("args", [])]
        return StdioServerParameters(command=argv[0], args=argv[1:], env=env, cwd=str(home))

    async def _run(self, conn: dict, s: Session):
        from mcp import ClientSession
        try:
            if conn["transport"] == "stdio":
                from mcp.client.stdio import stdio_client
                params = self._stdio_params(conn)
                errlog = open(Path(conn["home"]) / "stderr.log", "w", encoding="utf-8")  # noqa: SIM115 — closed with the process
                transport = stdio_client(params, errlog=errlog)
            else:
                import httpx2
                from mcp.client.streamable_http import streamable_http_client
                http = httpx2.AsyncClient(headers=conn.get("headers") or {}, timeout=httpx2.Timeout(30, read=300))
                transport = streamable_http_client(conn["url"], http_client=http)
            async with transport as streams, ClientSession(streams[0], streams[1], read_timeout_seconds=300) as client:
                await client.initialize()
                s.client = client
                s.ready.set()
                await s.stop.wait()
        except Exception as e:  # noqa: BLE001 — surface any startup/transport failure to the caller
            s.error = _root_cause(e)
            stderr = Path(conn.get("home", ".")) / "stderr.log"
            if conn.get("transport") == "stdio" and stderr.exists():
                tail = stderr.read_text("utf-8", errors="replace")[-600:].strip()
                if tail:
                    s.error += f" | server stderr: {tail}"
            s.error = s.error[:1200]
        finally:
            s.client = None
            s.ready.set()

    async def session(self, conn: dict):
        key, dg = conn["id"], self.digest(conn)
        s = self.sessions.get(key)
        if s and (s.digest != dg or s.error or (s.task and s.task.done())):
            await self.close(key)
            s = None
        if s is None:
            s = Session(dg)
            self.sessions[key] = s
            s.task = asyncio.create_task(self._run(conn, s))
        try:
            await asyncio.wait_for(s.ready.wait(), 60)
        except TimeoutError as e:
            await self.close(key)
            raise RuntimeError("MCP server did not finish initializing within 60 s") from e
        if s.client is None:
            err = s.error or "MCP session closed"
            self.sessions.pop(key, None)
            raise RuntimeError(f"MCP server failed to start: {err}")
        return s.client

    async def close(self, key: str):
        s = self.sessions.pop(key, None)
        if s and s.task:
            s.stop.set()
            try:
                await asyncio.wait_for(s.task, 10)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                s.task.cancel()

    async def list_tools(self, conn: dict) -> list[dict]:
        from mcp import types
        client = await self.session(conn)
        tools, cursor = [], None
        for _ in range(50):  # pagination guard
            res = await client.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
            tools += res.tools
            cursor = getattr(res, "next_cursor", None)
            if not cursor:
                break
        out = []
        for t in tools:
            ann = t.annotations
            out.append({"name": t.name, "description": (t.description or "")[:1000],
                        "input_schema": t.input_schema or {"type": "object"},
                        "read_only": bool(ann and ann.read_only_hint),
                        "destructive": bool(ann and ann.destructive_hint)})
        return out

    async def call(self, conn: dict, tool: str, arguments: dict, timeout_s: float, task_id: str) -> dict:
        client = await self.session(conn)
        job = asyncio.create_task(client.call_tool(tool, arguments, read_timeout_seconds=timeout_s))
        self.calls.setdefault(task_id, set()).add(job)
        try:
            res = await asyncio.wait_for(job, timeout_s + 5)
        finally:
            self.calls.get(task_id, set()).discard(job)
        parts = []
        for c in getattr(res, "content", None) or []:
            kind = getattr(c, "type", "")
            if kind == "text":
                parts.append(c.text)
            elif kind == "resource" and getattr(c.resource, "text", None):
                parts.append(c.resource.text)
            else:
                parts.append(f"[{kind or 'binary'} content omitted]")
        structured = getattr(res, "structured_content", None)
        return {"is_error": bool(getattr(res, "is_error", False)), "text": "\n".join(parts)[:20000],
                "structured": structured}

    def cancel(self, task_id: str) -> int:
        jobs = self.calls.pop(task_id, set())
        for j in jobs:
            j.cancel()
        return len(jobs)
