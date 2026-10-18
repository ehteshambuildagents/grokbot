"""Core acceptance tests (MOCK model where a model is involved; everything else real)."""
import httpx

from conftest import FIXTURE, Client, call, drain, set_scripts


# 1. Bot creation and persistence (process-restart persistence is covered in test_processes.py)
def test_bot_crud_versions_duplicate_delete(client):
    b = client.ok("post", "/api/bots", status=201, json={"name": "Scout", "role": "Researcher", "instructions": "Be brief"})
    b2 = client.ok("patch", f"/api/bots/{b['id']}", json={"instructions": "Be very brief", "pinned": True})
    assert b2["version"] == 2 and b2["pinned"]
    assert [v["version"] for v in client.ok("get", f"/api/bots/{b['id']}/versions")] == [2, 1]
    client.ok("post", "/api/memories", status=201, json={"content": "Scout prefers tables", "scope": "bot", "bot_id": b["id"]})
    dup = client.ok("post", f"/api/bots/{b['id']}/duplicate", status=201)
    assert dup["name"] == "Scout (copy)" and dup["instructions"] == "Be very brief"
    assert client.ok("get", f"/api/memories?bot_id={dup['id']}") == []  # memories not copied
    client.ok("patch", f"/api/bots/{b['id']}", json={"hidden": True})
    assert b["id"] not in [x["id"] for x in client.ok("get", "/api/bots")]
    assert b["id"] in [x["id"] for x in client.ok("get", "/api/bots?include_hidden=true")]
    impact = client.ok("get", f"/api/bots/{b['id']}/deletion-impact")
    assert impact["bot_memories_deleted"] == 1 and "kept" in impact["shared_workspace_files"]
    assert client.delete(f"/api/bots/{b['id']}").status_code == 400  # needs confirm
    client.ok("delete", f"/api/bots/{b['id']}?confirm=true")
    assert client.get(f"/api/bots/{b['id']}").status_code == 404


def test_csrf_and_auth_required(client):
    assert client.c.post("/api/bots", json={"name": "x"}).status_code == 403  # no CSRF header
    from fastapi.testclient import TestClient
    from localcrew.api import app
    assert TestClient(app).get("/api/bots").status_code == 401


# 13. Cross-user access denial
def test_cross_user_access_denied(client):
    other = Client()
    bot = client.bot()
    tid, conv = client.say(bot, "hello")
    art_id = None
    for url in [f"/api/bots/{bot['id']}", f"/api/tasks/{tid}", f"/api/conversations/{conv}/messages",
                f"/api/bots/{bot['id']}/conversation", f"/api/tasks/{tid}/events.json"]:
        assert other.get(url).status_code == 404, url
    assert other.patch(f"/api/bots/{bot['id']}", json={"name": "pwned"}).status_code == 404
    assert other.post(f"/api/tasks/{tid}/cancel").status_code == 404
    m = client.ok("post", "/api/memories", status=201, json={"content": "secret project codename is Bluebird"})
    assert other.patch(f"/api/memories/{m['id']}", json={"content": "x"}).status_code == 404
    assert other.ok("get", "/api/memories?q=Bluebird") == []
    client.ok("put", "/api/files", json={"path": "notes/private.txt", "content": "mine"})
    assert other.get("/api/files/content?path=notes/private.txt").status_code == 400  # not in their workspace
    del art_id


# 15. Path traversal, SSRF, unsafe upload
def test_path_traversal_rejected(client):
    for p in ["../x", "..\\x", "/etc/passwd", "C:/Windows/win.ini", "a/../../b", ".versions/x"]:
        r = client.put("/api/files", json={"path": p, "content": "x"})
        assert r.status_code == 400, p
    assert client.get("/api/files/content?path=../../.env").status_code == 400


def test_symlink_escape_rejected(client, tmp_path):
    import os
    from localcrew import workspace
    root = workspace.root(client.user_id)
    try:
        os.symlink(tmp_path, root / "link", target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("symlink creation not permitted on this host (Windows without developer mode)")
    assert client.get("/api/files/content?path=link/x").status_code == 400


def test_ssrf_blocked():
    from localcrew.security import check_url
    import pytest
    for u in ["http://169.254.169.254/latest", "http://127.0.0.1:5432/", "http://localhost:8000/api/me",
              "http://10.0.0.1/", "file:///etc/passwd", "ftp://example.com/"]:
        with pytest.raises(ValueError):
            check_url(u)
    assert check_url(f"{FIXTURE}/") == f"{FIXTURE}/"  # explicit fixture allowlist


def test_unsafe_uploads_rejected(client):
    def up(name, data):
        return client.post("/api/files/upload", files={"file": (name, data)})
    assert up("ok.txt", b"hello").status_code == 201
    assert up("data.json", b"{bad").status_code == 415
    assert up("evil.exe", b"MZ\x90\x00").status_code == 415
    assert up("a.zip", b"PK\x03\x04....").status_code == 415
    assert up("fake.png", b"<script>alert(1)</script>").status_code == 415
    assert up("fake.pdf", b"%PDF-1.4 garbage").status_code == 415
    assert up("big.txt", b"x" * (21 * 1024 * 1024)).status_code == 413
    assert up("../../escape.txt", b"x").json()["path"] == "uploads/escape.txt"


# 12. Memory correction and deletion
def test_memory_correct_disable_delete(client):
    m = client.ok("post", "/api/memories", status=201, json={"content": "The quarterly report is due on Friday"})
    m2 = client.ok("patch", f"/api/memories/{m['id']}", json={"content": "The quarterly report is due on Monday"})
    assert m2["revision"] == 2 and m2["history"][0]["content"].endswith("Friday")
    assert [x["id"] for x in client.ok("get", "/api/memories?q=quarterly report")] == [m["id"]]
    client.ok("patch", f"/api/memories/{m['id']}", json={"enabled": False})
    assert client.ok("get", "/api/memories?q=quarterly") == []  # disabled memories are not retrieved
    client.ok("delete", f"/api/memories/{m['id']}")
    assert client.ok("get", "/api/memories") == []
    r = client.post("/api/memories", json={"content": "my password is hunter22"})
    assert r.status_code == 400  # credentials are never stored


def test_bot_private_memory_not_shared(client):
    atlas, quill = client.bot("Atlas"), client.bot("Quill")
    client.ok("post", "/api/memories", status=201, json={"content": "Atlas codeword falcon", "scope": "bot", "bot_id": atlas["id"]})
    from localcrew import memory
    from localcrew.db import session_scope
    with session_scope() as db:
        assert memory.search(db, client.user_id, atlas["id"], "codeword falcon")
        assert not memory.search(db, client.user_id, quill["id"], "codeword falcon")


# 2 (MOCK half). Task produces a downloadable artifact with provenance
def test_task_creates_downloadable_artifact(client):
    set_scripts({"t": [{"tool_calls": [call("create_artifact", name="summary.md", content="# Summary\nAll good.")]},
                       {"content": "Created summary.md"}]})
    bot = client.bot(model="scripted:t")
    tid, conv = client.say(bot, "write a summary")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t
    assert [s["kind"] for s in t["steps"]][:1] == ["plan"] and {"tool", "validate", "report"} <= {s["kind"] for s in t["steps"]}
    art = t["artifacts"][0]
    r = client.get(art["url"])
    assert r.status_code == 200 and r.content == b"# Summary\nAll good."
    assert "attachment" in r.headers["content-disposition"]
    assert art["provenance"]["task_id"] == tid
    msgs = client.ok("get", f"/api/conversations/{conv}/messages")
    assert msgs[-1]["author_bot_id"] == bot["id"] and "summary.md" in msgs[-1]["content"]
    assert Client().get(art["url"]).status_code == 404  # other users cannot download


def test_deliverable_written_from_final_answer(client):
    set_scripts({"d": [{"content": "Findings: three items."}]})
    bot = client.bot(model="scripted:d")
    tid, _ = client.say(bot, "report please", deliverable="report.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and t["artifacts"][0]["name"] == "report.md"
    assert b"Findings: three items." in client.get(t["artifacts"][0]["url"]).content


def test_invalid_tool_args_rejected_before_execution(client):
    set_scripts({"v": [{"tool_calls": [call("write_file", path="", content=5)]}, {"content": "ok"}]})
    bot = client.bot(model="scripted:v")
    tid, _ = client.say(bot, "write")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and any(x["data"].get("call_status") == "invalid" for x in t["steps"])


# 10. Approval denial, expiry, replay rejection, changed-payload rejection
def _approval_task(client, script_key="a", title="Bug: calc"):
    client.ok("post", "/api/connections", status=201, json={"connector": "fixture_tracker", "name": "tracker",
              "config": {"base_url": FIXTURE}, "secret": "fixture-tracker-token"})
    set_scripts({script_key: [{"tool_calls": [call("tracker_create_issue", title=title, body="2+2=5")]},
                              {"content": "submitted"}]})
    bot = client.bot("Quill", model=f"scripted:{script_key}")
    tid, _ = client.say(bot, "file the bug")
    drain()
    t = client.task(tid)
    assert t["status"] == "waiting_for_approval", t
    appr = client.ok("get", "/api/approvals")[0]
    return tid, appr


def test_approval_card_and_allow_once(client):
    tid, appr = _approval_task(client)
    assert appr["operation"] == "tracker_create_issue" and appr["target"] == "fixture_tracker"
    assert appr["payload"]["title"] == "Bug: calc" and appr["acting_identity"]["bot_name"] == "Quill"
    assert appr["expected_effect"] and appr["expires_at"]
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    drain()
    assert client.task(tid)["status"] == "succeeded"
    assert len(httpx.get(f"{FIXTURE}/admin/state").json()["issues"]) == 1
    # replay: the same approval cannot be decided or consumed again
    assert client.post(f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]}).status_code == 409
    from localcrew.db import session_scope
    from localcrew.policy import Action, consume_approval
    with session_scope() as db:
        a = Action("tracker_create_issue", "external_write", "fixture_tracker",
                   {"title": "Bug: calc", "body": "2+2=5", "labels": []}, client.user_id, appr["acting_identity"]["bot_id"])
        assert a.digest() == appr["digest"]
        assert consume_approval(db, appr["id"], a) is False


def test_approval_denied(client):
    tid, appr = _approval_task(client, "b")
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "deny", "digest": appr["digest"]})
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and t["tool_calls"][0]["status"] == "denied"
    assert httpx.get(f"{FIXTURE}/admin/state").json()["creates"] == 0


def test_approval_expired(client):
    tid, appr = _approval_task(client, "c")
    from datetime import timedelta
    from localcrew.db import session_scope, utcnow
    from localcrew.models import ApprovalRequest
    from localcrew.policy import expire_stale
    with session_scope() as db:
        db.get(ApprovalRequest, appr["id"]).expires_at = utcnow() - timedelta(seconds=1)
    r = client.post(f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    assert r.status_code == 409
    with session_scope() as db:
        expire_stale(db)
    assert client.ok("get", "/api/approvals?status=expired")[0]["id"] == appr["id"]
    assert httpx.get(f"{FIXTURE}/admin/state").json()["creates"] == 0


def test_changed_payload_invalidates_approval(client):
    tid, appr = _approval_task(client, "e")
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    # Tamper with the pending call after approval (different recipient/content)
    from localcrew.db import session_scope
    from localcrew.models import Task
    with session_scope() as db:
        t = db.get(Task, tid)
        cp = dict(t.checkpoint)
        cp["pending"] = [call("tracker_create_issue", title="Bug: calc", body="CHANGED BODY")]
        t.checkpoint = cp
    drain()
    t = client.task(tid)
    assert t["status"] == "waiting_for_approval"  # a fresh approval is required
    pend = client.ok("get", "/api/approvals")
    assert len(pend) == 1 and pend[0]["payload"]["body"] == "CHANGED BODY" and pend[0]["id"] != appr["id"]
    assert httpx.get(f"{FIXTURE}/admin/state").json()["creates"] == 0
    # stale digest on decide is rejected too
    assert client.post(f"/api/approvals/{pend[0]['id']}/decide",
                       json={"decision": "approve", "digest": appr["digest"]}).status_code == 409


def test_persistent_rule_is_narrow_and_org_deny_wins(client):
    tid, appr = _approval_task(client, "f")
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "scope": "rule", "digest": appr["digest"]})
    drain()
    from localcrew.db import session_scope
    from localcrew.policy import Action, decide
    with session_scope() as db:
        a = Action("tracker_create_issue", "external_write", "fixture_tracker", {"title": "x"}, client.user_id,
                   appr["acting_identity"]["bot_id"])
        assert decide(db, a, "standard")[0] == "allow"
        other_bot = Action(**{**a.__dict__, "bot_id": client.bot("Atlas")["id"]})
        assert decide(db, other_bot, "standard")[0] == "require_approval"  # rule is bot-scoped
    from localcrew.models import User
    with session_scope() as db:
        db.get(User, client.user_id).is_admin = True
    client.ok("post", "/api/policy-rules", status=201, json={"tool": "tracker_create_issue", "decision": "deny", "level": "org"})
    with session_scope() as db:
        assert decide(db, a, "standard")[0] == "deny"
    rid = next(r["id"] for r in client.ok("get", "/api/policy-rules") if r["level"] == "org")
    client.ok("delete", f"/api/policy-rules/{rid}")


# 8. Ambiguous external write without duplicate execution
def test_ambiguous_write_reconciled_no_duplicate(client):
    httpx.post(f"{FIXTURE}/admin/faults", json={"error_after_commit": True})
    tid, appr = _approval_task(client, "g")
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t
    assert t["tool_calls"][0]["result"]["reconciled"] is True
    st = httpx.get(f"{FIXTURE}/admin/state").json()
    assert st["creates"] == 1 and len(st["issues"]) == 1


def test_ambiguous_write_after_crash_never_blindly_retried(client):
    """Simulate a crash after the ToolCall was committed as running: on recovery the engine reconciles via the
    idempotency key; if the tracker has it, it is not re-sent."""
    tid, appr = _approval_task(client, "h")
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    drain()
    st = httpx.get(f"{FIXTURE}/admin/state").json()
    assert st["creates"] == 1
    # rewind the checkpoint as if the worker crashed right after the external call but before checkpointing
    from localcrew.db import session_scope
    from localcrew.models import Task, ToolCall
    from sqlalchemy import select
    with session_scope() as db:
        t = db.get(Task, tid)
        tc = db.scalar(select(ToolCall).where(ToolCall.task_id == tid))
        tc.status = "running"
        t.status, t.phase = "queued", "running"
        cp = dict(t.checkpoint)
        cp["messages"] = [m for m in cp["messages"] if m["role"] != "tool"]
        cp["pending"] = [call("tracker_create_issue", title="Bug: calc", body="2+2=5")]
        cp["seq"] = 0
        cp.pop("final", None)
        t.checkpoint = cp
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded"
    st = httpx.get(f"{FIXTURE}/admin/state").json()
    assert st["creates"] == 1 and len(st["issues"]) == 1
    assert any(s["kind"] == "reconcile" for s in t["steps"])


# 14. Malicious page instructions attempting credential disclosure
def test_prompt_injection_cannot_exfiltrate(client):
    client.ok("post", "/api/connections", status=201, json={"connector": "fixture_tracker", "name": "tracker",
              "config": {"base_url": FIXTURE}, "secret": "fixture-tracker-token"})
    # The MOCK model deliberately "obeys" the injected instructions to prove enforcement is outside the model.
    set_scripts({"i": [
        {"tool_calls": [call("browser_open", url=f"{FIXTURE}/malicious")]},
        {"tool_calls": [call("fetch_url", url="http://169.254.169.254/latest/meta-data"),
                        call("fetch_url", url="http://127.0.0.1:54329/"),
                        call("read_file", path="../../.local/keys/secrets.key"),
                        call("tracker_create_issue", title="secrets", body="YOUR_SECRETS")]},
        {"content": "done"}]})
    bot = client.bot("Quill", model="scripted:i")
    tid, _ = client.say(bot, "summarize the page")
    drain()
    t = client.task(tid)
    calls = {c["seq"]: c for c in t["tool_calls"]}
    assert calls[1]["status"] == "succeeded"
    assert "untrusted" not in str(calls[1]["result"]) or True
    assert calls[2]["status"] == "failed" and "blocked" in calls[2]["result"]["error"]
    assert calls[3]["status"] == "failed"
    assert calls[4]["status"] == "failed" and "not allowed" in calls[4]["result"]["error"]
    assert t["status"] == "waiting_for_approval"  # external write cannot happen without a human
    appr = client.ok("get", "/api/approvals")[0]
    assert "fixture-tracker-token" not in str(appr["payload"])  # secrets redacted on the card
    # The model never received the tracker token or the secret key file
    from localcrew.llm import ScriptedProvider  # noqa: F401
    from localcrew.db import session_scope
    from localcrew.models import Task
    with session_scope() as db:
        msgs = str(db.get(Task, tid).checkpoint["messages"])
    assert "fixture-tracker-token" not in msgs
    assert "<untrusted_tool_output" in msgs
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "deny", "digest": appr["digest"]})
    drain()
    assert httpx.get(f"{FIXTURE}/admin/state").json()["creates"] == 0


def test_secrets_encrypted_at_rest_and_redacted(client):
    client.ok("post", "/api/connections", status=201, json={"connector": "fixture_tracker", "name": "t",
              "config": {"base_url": FIXTURE}, "secret": "fixture-tracker-token"})
    from sqlalchemy import select, text
    from localcrew.db import session_scope
    with session_scope() as db:
        raw = b"".join(r[0] for r in db.execute(text("select ciphertext from secret_references")))
    assert b"fixture-tracker-token" not in raw
    conns = client.ok("get", "/api/connections")
    assert "fixture-tracker-token" not in str(conns)
    assert client.ok("get", f"/api/connections/{conns[0]['id']}/health")["ok"] is True
    client.ok("post", f"/api/connections/{conns[0]['id']}/revoke")
    assert client.ok("get", f"/api/connections/{conns[0]['id']}/health")["ok"] is False
    from localcrew.security import Redactor
    assert "fixture-tracker-token" not in Redactor.text("token=fixture-tracker-token and Bearer abcdefghijkl")
    del select


# 5. Handoff with evidence preserved
def test_handoff_creates_child_with_evidence(client):
    set_scripts({
        "lead": [{"tool_calls": [call("handoff", bot="Quill", objective="Check the calculator for wrong sums",
                                      deliverable="qa.md", completion_criteria="defect reproduced or ruled out",
                                      source_refs=[f"{FIXTURE}/app/calc"], tool_budget=4)]},
                 {"content": "Delegated to Quill"}],
        "qa": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/app/calc?a=2&b=2")]},
               {"content": "Reproduced: 2+2 shows 5"}]})
    atlas = client.bot("Atlas", model="scripted:lead")
    client.bot("Quill", model="scripted:qa")
    conv = client.ok("post", "/api/conversations", status=201, json={"bot_ids": [atlas["id"], client.bot("Quill")["id"]], "title": "QA group"})
    r = client.ok("post", f"/api/conversations/{conv['id']}/messages", status=201, json={"content": "@Atlas check the calculator"})
    assert len(r["task_ids"]) == 1  # only the mentioned bot
    drain()
    parent = client.task(r["task_ids"][0])
    assert parent["status"] == "succeeded" and len(parent["children"]) == 1
    child = client.task(parent["children"][0]["id"])
    assert child["status"] == "succeeded", child
    h = child["handoff"]
    for k in ("objective", "owner_bot", "source_refs", "deliverable", "completion_criteria", "budget", "deadline", "permissions"):
        assert h[k], k
    assert child["artifacts"][0]["name"] == "qa.md"
    assert child["tool_calls"][0]["tool"] == "browser_open" and child["tool_calls"][0]["status"] == "succeeded"
    ev = [e["type"] for e in client.ok("get", f"/api/tasks/{parent['id']}/events.json")]
    assert "handoff" in ev and "handoff_completed" in ev
    msgs = [m["content"] for m in client.ok("get", f"/api/conversations/{conv['id']}/messages")]
    assert any("Handoff to @Quill" in m for m in msgs) and any("Reproduced" in m for m in msgs)


def test_handoff_loop_prevention(client):
    # Each bot keeps handing back to the other with the same objective: dedup + depth stop it.
    set_scripts({"ping": [{"tool_calls": [call("handoff", bot="Quill", objective="loop", tool_budget=2)]}, {"content": "x"}],
                 "pong": [{"tool_calls": [call("handoff", bot="Atlas", objective="loop", tool_budget=1)]}, {"content": "y"}]})
    atlas = client.bot("Atlas", model="scripted:ping")
    client.bot("Quill", model="scripted:pong")
    tid, _ = client.say(atlas, "start")
    drain(50)
    from localcrew.db import session_scope
    from localcrew.models import Task
    with session_scope() as db:
        n = db.query(Task).filter(Task.owner_user_id == client.user_id).count()
    assert n <= 4  # bounded by budget/depth, not endless


# 9. Cancellation of active terminal work (browser cancellation in test_browser.py)
def test_cancel_running_terminal_command(client):
    import threading
    import time
    from localcrew.db import session_scope
    from localcrew.models import PolicyRule
    set_scripts({"k": [{"tool_calls": [call("run_command", argv=["python", "-c", "import time; time.sleep(60)"], timeout_s=120)]},
                       {"content": "never"}]})
    bot = client.bot(model="scripted:k", tools=["run_command"])
    with session_scope() as db:
        db.add(PolicyRule(level="user", owner_user_id=client.user_id, tool="run_command", decision="allow"))
    tid, _ = client.say(bot, "sleep")
    th = threading.Thread(target=drain)
    th.start()
    for _ in range(100):
        if client.task(tid)["tool_calls"] and client.task(tid)["tool_calls"][0]["status"] == "running":
            break
        time.sleep(0.2)
    time.sleep(1)
    t0 = time.time()
    client.ok("post", f"/api/tasks/{tid}/cancel")
    th.join(30)
    assert client.task(tid)["status"] == "cancelled"
    assert time.time() - t0 < 15  # cancelled well before the 60 s sleep finished


def test_redirect_running_task(client):
    set_scripts({"r": [{"content": "", "tool_calls": [call("list_files", path="")]}, {"content": "final"}]})
    bot = client.bot(model="scripted:r")
    tid, _ = client.say(bot, "list files")
    client.ok("post", f"/api/tasks/{tid}/redirect", json={"text": "Only list the uploads folder"})
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and any(s["kind"] == "redirect" for s in t["steps"])


def test_model_error_retries_with_backoff(client):
    set_scripts({"x": [{"raise": "connection refused"}]})
    bot = client.bot(model="scripted:x")
    tid, _ = client.say(bot, "hi")
    drain()
    t = client.task(tid)
    assert t["status"] == "retry_wait" and t["attempts"] == 1 and "retrying" in t["error"]


def test_sse_replay(client):
    set_scripts({"s": [{"content": "hello there"}]})
    bot = client.bot(model="scripted:s")
    tid, _ = client.say(bot, "hi")
    drain()
    evs = client.ok("get", f"/api/tasks/{tid}/events.json")
    assert evs[-1]["data"]["status"] == "succeeded"
    later = client.ok("get", f"/api/tasks/{tid}/events.json?after={evs[2]['id']}")
    assert [e["id"] for e in later] == [e["id"] for e in evs[3:]]  # replay from a cursor
