"""Connector interface + implementations.

Each connector declares capabilities with input schemas, read/write class, auth, scopes, rate limit.
Credentials are fetched by the broker (reveal_secret) inside the connector call and never returned to the
model. External writes carry an idempotency key and support reconciliation after ambiguous outcomes."""
import time
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import workspace
from .models import Connection
from .security import Redactor, reveal_secret, safe_http_client


class Ambiguous(Exception):
    """The remote write may or may not have happened."""


@dataclass
class Capability:
    name: str
    description: str
    args: type[BaseModel]
    effect: str  # read|external_write|workspace_write
    scopes: list[str] = field(default_factory=list)


class Connector:
    key: str
    title: str
    auth: str  # none|api_token|oauth
    requirements: str
    rate_limit_per_min: int = 60
    capabilities: dict[str, Capability] = {}
    _calls: dict[str, list[float]] = {}

    def __init__(self, db: Session, conn: Connection | None):
        self.db, self.conn = db, conn

    def _rate(self):
        k = f"{self.key}:{self.conn.id if self.conn else ''}"
        now = time.monotonic()
        recent = [t for t in self._calls.get(k, []) if now - t < 60]
        if len(recent) >= self.rate_limit_per_min:
            raise RuntimeError("connector rate limit reached")
        self._calls[k] = [*recent, now]

    def health(self) -> dict:
        return {"ok": True}

    def call(self, op: str, args: BaseModel, idempotency_key: str) -> dict:
        self._rate()
        return getattr(self, op)(args, idempotency_key)

    def reconcile(self, op: str, args: BaseModel, idempotency_key: str) -> dict | None:
        """Return the existing result if the write already happened, None if it certainly did not.
        Raise Ambiguous if it cannot be determined."""
        raise Ambiguous("connector cannot reconcile")


# ---------- local files ----------

class PathArgs(BaseModel):
    path: str = Field("", max_length=500)


class LocalFiles(Connector):
    key, title, auth = "local_files", "Local workspace files", "none"
    requirements = "None. Operates on this user's workspace directory only."
    capabilities = {
        "list": Capability("list", "List workspace directory", PathArgs, "read"),
        "read": Capability("read", "Read a workspace text file", PathArgs, "read"),
    }

    def list(self, a: PathArgs, _):
        return {"entries": workspace.list_dir(self.conn.owner_user_id, a.path)}

    def read(self, a: PathArgs, _):
        return {"content": workspace.read_text(self.conn.owner_user_id, a.path)}


# ---------- local fixture issue tracker ----------

class IssueArgs(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(max_length=20000)
    labels: list[str] = Field(default_factory=list, max_length=10)


class NoArgs(BaseModel):
    pass


class FixtureTracker(Connector):
    key, title, auth = "fixture_tracker", "Local fixture issue tracker", "api_token"
    requirements = ("Runs against tests/fixtures/fixture_app.py (no account, no cost). "
                    "Config: base_url; secret: API token.")
    capabilities = {
        "list_issues": Capability("list_issues", "List issues in the tracker", NoArgs, "read", ["issues:read"]),
        "create_issue": Capability("create_issue", "Create an issue in the tracker (external write)", IssueArgs,
                                   "external_write", ["issues:write"]),
    }

    def _client(self):
        token = reveal_secret(self.db, self.conn.secret_id, self.conn.owner_user_id)
        return safe_http_client(base_url=self.conn.config["base_url"], timeout=10,
                                headers={"Authorization": f"Bearer {token}"})

    def health(self):
        try:
            with self._client() as c:
                r = c.get("/api/issues")
                return {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as e:  # noqa: BLE001 — health reports any failure
            return {"ok": False, "error": Redactor.text(str(e))}

    def list_issues(self, a, _):
        with self._client() as c:
            r = c.get("/api/issues")
            r.raise_for_status()
            return {"issues": r.json()}

    def create_issue(self, a: IssueArgs, key: str):
        try:
            with self._client() as c:
                r = c.post("/api/issues", json=a.model_dump(), headers={"Idempotency-Key": key})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise Ambiguous(f"no response from tracker: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise Ambiguous(f"tracker returned {r.status_code}")
        r.raise_for_status()
        return r.json()

    def reconcile(self, op, a, key):
        with self._client() as c:
            r = c.get("/api/issues", params={"idempotency_key": key})
            if r.status_code != 200:
                raise Ambiguous("reconcile lookup failed")
            found = r.json()
            return found[0] if found else None


class MCPServer(Connector):
    """Any MCP server (stdio command or Streamable HTTP URL). Its tools are discovered at connect time and exposed to
    bots as mcp__<server>__<tool>; see mcp_tools.py. Add as many as you like."""
    key, title, auth = "mcp", "MCP server (any)", "optional token (env var or HTTP header)"
    requirements = ("A stdio command (runs inside the computer service under the sandbox limits) or an https URL. "
                    "The server's own requirements apply (runtime such as Node/Python, accounts, API keys).")
    capabilities = {}


REGISTRY: dict[str, type[Connector]] = {c.key: c for c in (LocalFiles, FixtureTracker, MCPServer)}

OPTIONAL = [
    {"key": "slack", "status": "unavailable",
     "requirements": "Separately authorized Slack app, workspace admin approval, current Slack API access."},
    {"key": "x", "status": "unavailable",
     "requirements": "Paid X API tier and app credentials; not implemented."},
]


def describe(c: type[Connector]) -> dict:
    return {"key": c.key, "title": c.title, "auth": c.auth, "requirements": c.requirements,
            "rate_limit_per_min": c.rate_limit_per_min,
            "capabilities": [{"name": k, "description": v.description, "effect": v.effect, "scopes": v.scopes,
                              "input_schema": v.args.model_json_schema()} for k, v in c.capabilities.items()]}
