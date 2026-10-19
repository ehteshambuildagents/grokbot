"""MCP servers as LocalCrew connections, and their tools as policy-checked bot tools.

A user can add any number of MCP servers (Connections → MCP). Discovery stores each server's tool list on the
connection. Bots enable tools individually (`mcp__<server>__<tool>`) or per server (`mcp:<connection_id>`).

Every MCP tool call goes through the same path as built-in tools: JSON-Schema argument validation, policy
(effect from the tool's annotations: read-only → read, destructive → destructive, otherwise external_write, which
requires approval by default), idempotency key, untrusted-output wrapping, redaction. The server's credential (one
secret per connection, injected as an env var or HTTP header) is resolved here and never reaches the model.
"""
import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .models import Bot, Connection
from .security import Redactor, reveal_secret
from .tools import Tool, ToolFailed

PREFIX = "mcp__"


class ArgsInvalid(ValueError):
    def __init__(self, details: list[str]):
        super().__init__("; ".join(details))
        self.details = details


class JsonArgs(dict):
    """Validated MCP arguments; quacks like a pydantic model for the engine (model_dump)."""

    def model_dump(self):
        return dict(self)


def exposed_name(server: str, tool: str) -> str:
    import re
    clean = re.compile(r"[^A-Za-z0-9_-]")
    return f"{PREFIX}{clean.sub('_', server)[:20]}__{clean.sub('_', tool)}"[:64]


def payload(db: Session, conn: Connection) -> dict:
    """Connection settings for the computer service, with the secret resolved by the broker."""
    cfg = conn.config or {}
    out = {"id": conn.id, "user_id": conn.owner_user_id, "transport": cfg.get("transport", "stdio"),
           "command": cfg.get("command", ""), "args": cfg.get("args", []), "url": cfg.get("url", ""),
           "env": dict(cfg.get("env") or {}), "headers": {}}
    if conn.secret_id:
        secret = reveal_secret(db, conn.secret_id, conn.owner_user_id)
        target = cfg.get("secret_as") or {"kind": "header", "name": "Authorization", "prefix": "Bearer "}
        if target.get("kind") == "env":
            out["env"][target["name"]] = secret
        else:
            out["headers"][target.get("name", "Authorization")] = target.get("prefix", "") + secret
    return out


def _computer(path: str, body: dict, timeout: float = 120) -> dict:
    s = settings()
    try:
        r = httpx.post(f"{s.computer_url}{path}", json=body, headers={"X-Internal-Token": s.internal_token()},
                       timeout=timeout)
    except httpx.HTTPError as e:
        raise ToolFailed(f"computer service unreachable: {type(e).__name__}") from e
    if r.status_code == 499:
        from .llm import Cancelled
        raise Cancelled()
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise ToolFailed(Redactor.text(str(detail))[:500])
    return r.json()


def discover(db: Session, conn: Connection) -> list[dict]:
    """Ask the server for its tools and store them (with exposed names) on the connection."""
    try:
        tools = _computer("/mcp/list", payload(db, conn), timeout=90)["tools"]
    except ToolFailed as e:
        conn.status = "error"
        conn.config = {**(conn.config or {}), "last_error": str(e), "checked_at": utcnow().isoformat()}
        raise
    seen = set()
    for t in tools:
        name = exposed_name(conn.name, t["name"])
        if name in seen:
            name = name[:58] + "_" + str(len(seen))
        seen.add(name)
        t["exposed_name"] = name
        try:
            Draft202012Validator.check_schema(t["input_schema"])
            t["schema_ok"] = True
        except SchemaError:
            t["schema_ok"] = False  # unusable: the engine cannot validate arguments for it
    conn.config = {**(conn.config or {}), "tools": tools, "discovered_at": utcnow().isoformat(), "last_error": ""}
    conn.status = "active"
    return tools


def effect_of(t: dict) -> str:
    if t.get("read_only"):
        return "read"
    return "destructive" if t.get("destructive") else "external_write"


def _make_tool(conn: Connection, t: dict) -> Tool:
    schema = t["input_schema"] if isinstance(t.get("input_schema"), dict) else {"type": "object"}
    validator = Draft202012Validator(schema)
    conn_id, tool_name, server = conn.id, t["name"], conn.name

    def parse(raw):
        if not isinstance(raw, dict):
            raise ArgsInvalid(["arguments must be an object"])
        errors = [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(raw)]
        if errors:
            raise ArgsInvalid(errors[:5])
        return JsonArgs(raw)

    def run(ctx, a, key):
        conn_now = ctx.db.get(Connection, conn_id)
        if conn_now is None or conn_now.status == "revoked":
            raise ToolFailed(f"MCP server {server!r} is no longer connected")
        res = _computer("/mcp/call", {"conn": payload(ctx.db, conn_now), "tool": tool_name, "arguments": dict(a),
                                      "timeout_s": 120, "task_id": ctx.task.id}, timeout=200)
        if res.get("is_error"):
            raise ToolFailed(f"{server}/{tool_name} returned an error: {res.get('text', '')[:1000]}")
        return {"server": server, "tool": tool_name, "text": res.get("text", ""), "structured": res.get("structured")}

    description = f"[MCP {server}] {t.get('description') or tool_name}"[:1024]
    return Tool(t["exposed_name"], description, None, effect_of(t), run, target=lambda a: f"{server}/{tool_name}",
                untrusted_output=True, parse=parse, json_schema=schema,
                expected_effect=lambda a: f"call {tool_name} on MCP server {server}")


def user_tools(db: Session, user_id: str) -> dict[str, tuple[Connection, dict]]:
    out = {}
    for c in db.scalars(select(Connection).where(Connection.owner_user_id == user_id, Connection.connector == "mcp",
                                                 Connection.status == "active")):
        for t in (c.config or {}).get("tools", []):
            if t.get("schema_ok", True):
                out[t["exposed_name"]] = (c, t)
    return out


def bot_tools(db: Session, bot: Bot) -> dict[str, Tool]:
    """MCP tools this bot may use: entries `mcp__server__tool` or whole servers `mcp:<connection_id>`."""
    wanted = [n for n in (bot.tools or []) if n.startswith((PREFIX, "mcp:"))]
    if not wanted:
        return {}
    available = user_tools(db, bot.owner_user_id)
    out = {}
    for name, (c, t) in available.items():
        if name in wanted or f"mcp:{c.id}" in wanted:
            out[name] = _make_tool(c, t)
    return out
