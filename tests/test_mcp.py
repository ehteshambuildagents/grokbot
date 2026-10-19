"""MCP servers end to end: real MCP protocol (official SDK) against tests/fixtures/mcp_server.py, through the API,
computer service, engine and policy. The bot's decisions come from the MOCK scripted model."""
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest
from conftest import ROOT, call, drain, set_scripts

SERVER = str(ROOT / "tests" / "fixtures" / "mcp_server.py")
HTTP_PORT = 18950


def add_stdio(client, name="fixture", token="fixture-secret-token", extra_env=None):
    return client.ok("post", "/api/connections", status=201, json={
        "connector": "mcp", "name": name, "secret": token,
        "config": {"transport": "stdio", "command": sys.executable, "args": [SERVER],
                   "env": {"FIXTURE_MCP_NAME": name, **(extra_env or {})},
                   "secret_as": {"kind": "env", "name": "FIXTURE_TOKEN"}}})


def run(client, key, steps, tools, text="go"):
    set_scripts({key: steps})
    bot = client.bot(model=f"scripted:{key}", tools=tools)
    tid, _ = client.say(bot, text)
    drain()
    return client.task(tid)


def test_add_server_discovers_tools_with_effects(client):
    c = add_stdio(client)
    assert c["status"] == "active", c["config"].get("last_error")
    tools = {t["name"]: t for t in c["config"]["tools"]}
    assert set(tools) == {"add", "secret_length", "create_ticket", "wipe_everything", "slow", "broken"}
    assert tools["add"]["exposed_name"] == "mcp__fixture__add"
    listed = {t["name"]: t for t in client.ok("get", "/api/tools") if t.get("mcp_server")}
    assert listed["mcp__fixture__add"]["effect"] == "read"
    assert listed["mcp__fixture__create_ticket"]["effect"] == "external_write"
    assert listed["mcp__fixture__wipe_everything"]["effect"] == "destructive"
    assert "fixture-secret-token" not in str(c)  # secret never returned by the API


def test_bot_calls_read_tool_and_secret_stays_out_of_model_context(client):
    c = add_stdio(client)
    t = run(client, "mcpadd", [{"tool_calls": [call("mcp__fixture__add", a=2, b=3)]},
                               {"tool_calls": [call("mcp__fixture__secret_length")]}, {"content": "2+3=5"}],
            tools=[f"mcp:{c['id']}"])
    assert t["status"] == "succeeded", t["error"]
    assert [x["status"] for x in t["tool_calls"]] == ["succeeded", "succeeded"]
    assert t["tool_calls"][0]["result"]["text"] == "5"
    assert t["tool_calls"][1]["result"]["text"] == str(len("fixture-secret-token"))  # injected as env, not echoed
    from localcrew.db import session_scope
    from localcrew.models import Task
    from sqlalchemy import text
    with session_scope() as db:
        cp = str(db.get(Task, t["id"]).checkpoint)
        dump = str(db.execute(text("select data from task_events")).all()) + str(db.execute(text("select result from tool_calls")).all())
    assert "<untrusted_tool_output" in cp and "fixture-secret-token" not in cp + dump


def test_write_and_destructive_tools_need_approval(client):
    c = add_stdio(client)
    t = run(client, "mcpw", [{"tool_calls": [call("mcp__fixture__create_ticket", title="Printer broken")]}, {"content": "x"}],
            tools=[f"mcp:{c['id']}"])
    assert t["status"] == "waiting_for_approval"
    appr = client.ok("get", "/api/approvals")[0]
    assert appr["operation"] == "mcp__fixture__create_ticket" and appr["target"] == "fixture/create_ticket"
    assert appr["payload"] == {"title": "Printer broken"}
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    drain()
    t = client.task(t["id"])
    assert t["status"] == "succeeded" and "ticket #1 created" in t["tool_calls"][0]["result"]["text"]
    t2 = run(client, "mcpd", [{"tool_calls": [call("mcp__fixture__wipe_everything", confirm=True)]}, {"content": "x"}],
             tools=[f"mcp:{c['id']}"])
    assert t2["status"] == "waiting_for_approval"


def test_server_wide_policy_rule_with_wildcard(client):
    c = add_stdio(client)
    client.ok("post", "/api/policy-rules", status=201, json={"tool": "mcp__fixture__*", "decision": "allow"})
    t = run(client, "mcprule", [{"tool_calls": [call("mcp__fixture__create_ticket", title="auto")]}, {"content": "x"}],
            tools=[f"mcp:{c['id']}"])
    assert t["status"] == "succeeded" and t["tool_calls"][0]["status"] == "succeeded"


def test_schema_validation_and_tool_errors(client):
    c = add_stdio(client)
    t = run(client, "mcpbad", [{"tool_calls": [call("mcp__fixture__add", a="two", b=3)]},
                               {"tool_calls": [call("mcp__fixture__broken")]}, {"content": "x"}],
            tools=[f"mcp:{c['id']}"])
    assert t["status"] == "succeeded"
    steps = [s["data"].get("call_status") for s in t["steps"] if s["kind"] == "tool"]
    assert steps == ["invalid", "failed"]
    assert len(t["tool_calls"]) == 1 and "returned an error" in t["tool_calls"][0]["result"]["error"]  # invalid never ran


def test_only_enabled_tools_are_available(client):
    add_stdio(client)
    t = run(client, "mcpno", [{"tool_calls": [call("mcp__fixture__add", a=1, b=1)]}, {"content": "x"}],
            tools=["calculate", "mcp__fixture__secret_length"])
    assert [s["data"].get("call_status") for s in t["steps"] if s["kind"] == "tool"] == ["rejected"]


def test_many_servers_at_once(client):
    a, b = add_stdio(client, "alpha"), add_stdio(client, "beta")
    assert client.post("/api/connections", json={"connector": "mcp", "name": "alpha",
                       "config": {"transport": "stdio", "command": sys.executable, "args": [SERVER]}}).status_code == 409
    t = run(client, "mcpmany", [{"tool_calls": [call("mcp__alpha__add", a=1, b=2), call("mcp__beta__add", a=3, b=4)]},
                                {"content": "x"}], tools=[f"mcp:{a['id']}", f"mcp:{b['id']}"])
    assert [x["result"]["text"] for x in t["tool_calls"]] == ["3", "7"]


def test_revoked_server_tools_disappear(client):
    c = add_stdio(client)
    client.ok("post", f"/api/connections/{c['id']}/revoke")
    assert not [t for t in client.ok("get", "/api/tools") if t.get("mcp_server")]
    t = run(client, "mcprev", [{"tool_calls": [call("mcp__fixture__add", a=1, b=1)]}, {"content": "x"}],
            tools=[f"mcp:{c['id']}"])
    assert t["status"] == "succeeded" and t["tool_calls"] == []  # the tool is no longer offered, so nothing runs


def test_cancel_running_mcp_call(client):
    c = add_stdio(client)
    set_scripts({"mcpslow": [{"tool_calls": [call("mcp__fixture__slow", seconds=60)]}, {"content": "never"}]})
    bot = client.bot(model="scripted:mcpslow", tools=[f"mcp:{c['id']}"])
    tid, _ = client.say(bot, "wait")
    th = threading.Thread(target=drain)
    th.start()
    for _ in range(200):
        tc = client.task(tid)["tool_calls"]
        if tc and tc[0]["status"] == "running":
            break
        time.sleep(0.1)
    t0 = time.time()
    client.ok("post", f"/api/tasks/{tid}/cancel")
    th.join(40)
    assert client.task(tid)["status"] == "cancelled" and time.time() - t0 < 15


def test_http_server_with_bearer_secret(client):
    p = subprocess.Popen([sys.executable, SERVER, "--http", str(HTTP_PORT)], env={**os.environ, "FIXTURE_MCP_NAME": "web"})
    try:
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{HTTP_PORT}/mcp", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        cfg = {"transport": "http", "url": f"http://127.0.0.1:{HTTP_PORT}/mcp"}
        bad = client.ok("post", "/api/connections", status=201, json={"connector": "mcp", "name": "web-nokey", "config": cfg})
        assert bad["status"] == "error"  # 401 without the token
        good = client.ok("post", "/api/connections", status=201, json={"connector": "mcp", "name": "web", "config": cfg,
                                                                        "secret": "fixture-mcp-token"})
        assert good["status"] == "active" and len(good["config"]["tools"]) == 6
        t = run(client, "mcphttp", [{"tool_calls": [call("mcp__web__add", a=20, b=22)]}, {"content": "42"}],
                tools=[f"mcp:{good['id']}"])
        assert t["tool_calls"][0]["result"]["text"] == "42"
    finally:
        p.kill()


def test_plain_http_to_remote_mcp_host_rejected(client):
    r = client.post("/api/connections", json={"connector": "mcp", "name": "remote-plain",
                                              "config": {"transport": "http", "url": "http://mcp.example.com/mcp"}})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [{"transport": "stdio"}, {"transport": "ftp", "url": "x"},
                                 {"transport": "stdio", "command": "x", "secret_as": {"kind": "file", "name": "x"}}])
def test_invalid_mcp_config_rejected(client, bad):
    assert client.post("/api/connections", json={"connector": "mcp", "name": "bad", "config": bad}).status_code == 422
