"""Additional coverage for capabilities listed in docs/CAPABILITIES.md."""
from datetime import timedelta

from conftest import FIXTURE, Client, call, drain, set_scripts


def _allow(client, tool):
    from localcrew.db import session_scope
    from localcrew.models import PolicyRule
    with session_scope() as db:
        db.add(PolicyRule(level="user", owner_user_id=client.user_id, tool=tool, decision="allow"))


def test_context_compaction_keeps_objective_and_user_constraints():
    from localcrew.engine import compact
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "OBJECTIVE: compare files"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": "", "tool_calls": []})
        msgs.append({"role": "tool", "content": f"result {i} " + "x" * 2000, "tool_name": "read_file"})
        if i == 5:
            msgs.append({"role": "user", "content": "[New instruction from the user]: only use CSV files"})
    out = compact(msgs, 4096)
    text = str(out)
    assert len(text) < len(str(msgs)) / 3
    assert "OBJECTIVE: compare files" in text and "only use CSV files" in text
    assert "read_file: result 0" in text  # older evidence collapsed to references, not dropped
    assert out[-1]["content"].startswith("result 29")


def test_browser_download_and_upload(client):
    """Download needs the signed-in fixture session from test_browser (cookie in the shared profile) or skips."""
    client.ok("put", "/api/files", json={"path": "uploads/attach.txt", "content": "attachment"})
    _allow(client, "browser_upload")
    set_scripts({"ud": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/upload")]},
                        {"tool_calls": [call("browser_upload", label="Attachment", path="uploads/attach.txt")]},
                        {"content": "uploaded"}]})
    bot = client.bot(model="scripted:ud", tools=["browser_open", "browser_upload"])
    tid, _ = client.say(bot, "upload it")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and t["tool_calls"][1]["status"] == "succeeded", t["tool_calls"]
    # Download: sign in through the human channel first (fixture account), then the bot downloads.
    from test_browser import login_via_takeover
    login_via_takeover(client)
    client.ok("post", "/api/computer/release")
    set_scripts({"dl": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/dashboard")]},
                        {"tool_calls": [call("browser_download", role="link", name="Download report")]},
                        {"content": "downloaded"}]})
    client.bot(model="scripted:dl", tools=["browser_open", "browser_download"])
    tid, _ = client.say(bot, "download the report")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t
    assert client.ok("get", "/api/files/content?path=downloads/report.csv")["content"].startswith("month,total")


def test_terminal_timeout_env_scrub_and_cwd(client, monkeypatch):
    monkeypatch.setenv("LOCALCREW_SECRET_PROBE", "should-not-leak")
    _allow(client, "run_command")
    client.ok("put", "/api/files", json={"path": "proj/readme.txt", "content": "x"})
    set_scripts({"term": [
        {"tool_calls": [call("run_command", argv=["python", "-c", "import os;print(sorted(os.environ));print(os.getcwd())"], cwd="proj")]},
        {"tool_calls": [call("run_command", argv=["python", "-c", "import time;time.sleep(30)"], timeout_s=2)]},
        {"content": "ok"}]})
    bot = client.bot(model="scripted:term", tools=["run_command"])
    tid, _ = client.say(bot, "run")
    drain()
    t = client.task(tid)
    out = t["tool_calls"][0]["result"]
    assert out["exit_code"] == 0 and "LOCALCREW" not in out["output"] and "proj" in out["output"]
    assert t["tool_calls"][1]["result"]["timed_out"] is True
    # run_shell is a separate tool, off by default
    assert "run_shell" not in client.bot()["tools"]


def test_shell_requires_approval_by_default(client):
    set_scripts({"sh": [{"tool_calls": [call("run_command", argv=["python", "--version"])]}, {"content": "x"}]})
    bot = client.bot(model="scripted:sh", tools=["run_command"])
    tid, _ = client.say(bot, "run")
    drain()
    assert client.task(tid)["status"] == "waiting_for_approval"


def test_local_files_connector(client):
    from localcrew.connectors import REGISTRY, PathArgs
    from localcrew.db import session_scope
    from localcrew.models import Connection
    client.ok("put", "/api/files", json={"path": "c/a.txt", "content": "hello connector"})
    c = client.ok("post", "/api/connections", status=201, json={"connector": "local_files", "name": "files"})
    with session_scope() as db:
        conn = db.get(Connection, c["id"])
        lf = REGISTRY["local_files"](db, conn)
        assert lf.health()["ok"]
        assert lf.call("list", PathArgs(path="c"), "k1")["entries"][0]["name"] == "a.txt"
        assert lf.call("read", PathArgs(path="c/a.txt"), "k2")["content"] == "hello connector"


def test_skill_from_task_and_approval_gate(client):
    set_scripts({"sk": [{"tool_calls": [call("list_files", path="")]}, {"content": "listed"}]})
    bot = client.bot(model="scripted:sk")
    tid, conv = client.say(bot, "list my files")
    drain()
    sk = client.ok("post", f"/api/skills/from-task/{tid}", status=201)
    v = sk["versions"][0]
    assert v["status"] == "draft" and v["spec"]["permissions"] == ["list_files"]
    r = client.post(f"/api/conversations/{conv}/messages", json={"content": "go", "skill_id": sk["id"]})
    assert r.status_code == 400  # drafts cannot be used until approved
    client.ok("post", f"/api/skills/{sk['id']}/versions/1/approve")
    r = client.ok("post", f"/api/conversations/{conv}/messages", status=201, json={"content": "go", "skill_id": sk["id"]})
    drain()
    from localcrew.db import session_scope
    from localcrew.models import Task
    with session_scope() as db:
        assert db.get(Task, r["task_ids"][0]).skill_version_id == v["id"]


def test_routine_failure_notification(client):
    set_scripts({"fail": [{"content": ""}] * 3})  # stays empty after the 2 empty-reply nudges → validation fails
    bot = client.bot(model="scripted:fail")
    r = client.ok("post", "/api/routines", status=201, json={"name": "Nightly", "bot_id": bot["id"], "prompt": "p",
                                                              "schedule_text": "every day at 2am", "confirmed": True})
    run = client.ok("post", f"/api/routines/{r['id']}/run-now")
    drain()
    t = client.task(run["task_id"])
    assert t["status"] == "failed" and "validation failed" in t["error"]
    msgs = client.ok("get", f"/api/conversations/{t['conversation_id']}/messages")
    assert "Routine run failed" in msgs[-1]["content"]
    assert client.ok("get", f"/api/routines/{r['id']}/runs")[0]["status"] == "failed"


def test_task_deadline_enforced(client):
    set_scripts({"slow": [{"delay": 3, "content": "late"}]})
    bot = client.bot(model="scripted:slow")
    tid, _ = client.say(bot, "hurry")
    from localcrew.db import session_scope, utcnow
    from localcrew.models import Task
    with session_scope() as db:
        db.get(Task, tid).deadline_at = utcnow() + timedelta(seconds=1)
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "deadline" in t["error"]


def test_team_shared_bot_private_conversations(client):
    member, outsider = Client(), Client()
    team = client.ok("post", "/api/teams", status=201, json={"name": "Ops"})
    client.ok("post", f"/api/teams/{team['id']}/members", status=201, json={"email": member.email, "role": "member"})
    bot = client.bot("Atlas")
    client.ok("post", f"/api/bots/{bot['id']}/share/{team['id']}")
    assert bot["id"] in [b["id"] for b in member.ok("get", "/api/bots")]
    assert bot["id"] not in [b["id"] for b in outsider.ok("get", "/api/bots")]
    assert member.patch(f"/api/bots/{bot['id']}", json={"name": "x"}).status_code == 404  # members cannot edit
    c_owner = client.ok("get", f"/api/bots/{bot['id']}/conversation")
    c_member = member.ok("get", f"/api/bots/{bot['id']}/conversation")
    assert c_owner["id"] != c_member["id"]  # private per person
    assert member.get(f"/api/conversations/{c_owner['id']}/messages").status_code == 404
    # personal memory is never moved into team memory; members cannot write team memory
    assert member.post("/api/memories", json={"content": "x", "scope": "team", "team_id": team["id"]}).status_code == 403


def test_template_export_import_is_sanitized(client):
    bot = client.bot("Quill")
    client.ok("post", "/api/memories", status=201, json={"content": "Quill private fact", "scope": "bot", "bot_id": bot["id"]})
    tpl = client.ok("get", f"/api/bots/{bot['id']}/template")
    assert set(tpl["bot"]) == {"name", "avatar", "role", "instructions", "model", "model_options", "tools",
                               "policy_profile", "memory_enabled"}
    other = Client()
    b2 = other.ok("post", "/api/bots/import", status=201, json=tpl)
    assert b2["name"] == "Quill" and other.ok("get", f"/api/memories?bot_id={b2['id']}") == []


def test_web_search_reports_unavailable_without_config(client):
    set_scripts({"ws": [{"tool_calls": [call("web_search", query="localcrew")]}, {"content": "x"}]})
    bot = client.bot(model="scripted:ws", tools=["web_search"])
    tid, _ = client.say(bot, "search")
    drain()
    assert client.task(tid)["tool_calls"][0]["result"]["available"] is False


def test_sse_stream_does_not_hold_a_db_connection(client):
    """An open event stream must not keep a pooled connection 'idle in transaction' (pool exhaustion)."""
    import threading
    import time

    from localcrew.db import engine
    from sqlalchemy import text
    stop = threading.Event()

    def listen():
        with client.c.stream("GET", "/api/events?after=-1") as r:
            for _ in r.iter_lines():
                if stop.is_set():
                    break
    th = threading.Thread(target=listen, daemon=True)
    th.start()
    time.sleep(2)
    with engine().connect() as c:
        n = c.execute(text("select count(*) from pg_stat_activity where datname = current_database() "
                           "and state = 'idle in transaction' and query ilike '%auth_sessions%'")).scalar()
    stop.set()
    assert n == 0
