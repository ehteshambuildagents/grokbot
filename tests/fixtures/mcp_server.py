"""Fixture MCP server for tests (real MCP protocol via the official SDK; no external services).

  python tests/fixtures/mcp_server.py                 # stdio
  python tests/fixtures/mcp_server.py --http 18950    # Streamable HTTP on 127.0.0.1:18950/mcp (requires a bearer token)
"""
import asyncio
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

NAME = os.environ.get("FIXTURE_MCP_NAME", "fixture")
server = MCPServer(NAME)
TICKETS: list[str] = []


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def secret_length() -> str:
    """Report how long the injected FIXTURE_TOKEN is (never the value)."""
    return str(len(os.environ.get("FIXTURE_TOKEN", "")))


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def create_ticket(title: str) -> str:
    """Create a ticket in the fixture tracker (an external write)."""
    TICKETS.append(title)
    return f"ticket #{len(TICKETS)} created: {title}"


@server.tool(annotations=ToolAnnotations(destructive_hint=True))
def wipe_everything(confirm: bool) -> str:
    """Delete all data (destructive)."""
    return "wiped" if confirm else "not confirmed"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def slow(seconds: float) -> str:
    """Wait for a number of seconds."""
    await asyncio.sleep(seconds)
    return "finished"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def broken() -> str:
    """Always fails."""
    raise RuntimeError("fixture failure")


if __name__ == "__main__":
    if "--http" in sys.argv:
        import uvicorn
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import PlainTextResponse
        port = int(sys.argv[sys.argv.index("--http") + 1])
        app = server.streamable_http_app()

        async def require_token(request, call_next):
            if request.headers.get("authorization") != "Bearer fixture-mcp-token":
                return PlainTextResponse("unauthorized", 401)
            return await call_next(request)
        app.add_middleware(BaseHTTPMiddleware, dispatch=require_token)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    else:
        server.run("stdio")
