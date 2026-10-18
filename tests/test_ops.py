"""16. Backup restoration into a clean environment; key rotation; QA workflow."""
import os
import subprocess
import sys

import httpx
from sqlalchemy import create_engine, text

from conftest import ENV, FIXTURE, ROOT, TMP, call, drain, set_scripts

RESTORE_DB = ENV["LOCALCREW_DATABASE_URL"].rsplit("/", 1)[0] + "/localcrew_restore"


def test_backup_restore_into_clean_environment(client):
    set_scripts({"bk": [{"tool_calls": [call("create_artifact", name="keep.md", content="precious")]}, {"content": "ok"}]})
    bot = client.bot(model="scripted:bk")
    tid, _ = client.say(bot, "make it")
    drain()
    art = client.task(tid)["artifacts"][0]
    client.ok("put", "/api/files", json={"path": "docs/plan.txt", "content": "workspace file"})
    client.ok("post", "/api/memories", status=201, json={"content": "restore check memory"})
    conn = client.ok("post", "/api/connections", status=201, json={"connector": "fixture_tracker", "name": "t",
                     "config": {"base_url": FIXTURE}, "secret": "fixture-tracker-token"})
    dest = TMP / "backup1"
    subprocess.run([sys.executable, "infra/scripts/ops.py", "backup", str(dest)], cwd=ROOT, check=True)
    admin = create_engine(ENV["LOCALCREW_DATABASE_URL"].rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("drop database if exists localcrew_restore"))
        c.execute(text("create database localcrew_restore"))
    new_data, new_key = TMP / "restored-data", TMP / "restored-keys" / "secrets.key"
    subprocess.run([sys.executable, "infra/scripts/ops.py", "restore", str(dest), "--database-url", RESTORE_DB,
                    "--data-dir", str(new_data), "--key-file", str(new_key)], cwd=ROOT, check=True)
    # Verify in a separate process configured only with the restored environment
    check = f"""
import sys; sys.path.insert(0, r'{ROOT / "services/api"}')
from localcrew.db import session_scope
from localcrew.models import Bot, Memory, Artifact, Connection, User
from localcrew.security import reveal_secret
from localcrew.config import settings
with session_scope() as db:
    u = db.query(User).filter_by(email='{client.email}').one()
    assert db.query(Bot).filter_by(owner_user_id=u.id).count() >= 2
    assert db.query(Memory).filter_by(owner_user_id=u.id, content='restore check memory').count() == 1
    a = db.get(Artifact, '{art['id']}')
    assert (settings().artifacts / a.storage_path).read_bytes() == b'precious'
    assert (settings().workspaces / u.id / 'docs' / 'plan.txt').read_text() == 'workspace file'
    c = db.get(Connection, '{conn['id']}')
    assert reveal_secret(db, c.secret_id, u.id) == 'fixture-tracker-token'
print('RESTORE-OK')
"""
    env = {**os.environ, "LOCALCREW_DATABASE_URL": RESTORE_DB, "LOCALCREW_DATA_DIR": str(new_data),
           "LOCALCREW_SECRET_KEY_FILE": str(new_key)}
    out = subprocess.run([sys.executable, "-c", check], env=env, capture_output=True, text=True)
    assert "RESTORE-OK" in out.stdout, out.stderr
    # Restoring over a non-empty database is refused
    bad = subprocess.run([sys.executable, "infra/scripts/ops.py", "restore", str(dest), "--database-url", RESTORE_DB,
                          "--data-dir", str(new_data), "--key-file", str(new_key)], cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode != 0 and "not empty" in bad.stderr


def test_key_rotation_keeps_secrets_readable(client):
    conn = client.ok("post", "/api/connections", status=201, json={"connector": "fixture_tracker", "name": "rot",
                     "config": {"base_url": FIXTURE}, "secret": "fixture-tracker-token"})
    subprocess.run([sys.executable, "infra/scripts/ops.py", "rotate-keys"], cwd=ROOT, check=True)
    assert client.ok("get", f"/api/connections/{conn['id']}/health")["ok"] is True


def test_qa_workflow_reproduce_report_and_ask_before_submit(client):
    """QA bot opens the local issue fixture, reproduces the seeded defect, captures a screenshot and logs, writes a
    bug report, and must ask before submitting to the external tracker. (MOCK model drives the steps.)"""
    client.ok("post", "/api/connections", status=201, json={"connector": "fixture_tracker", "name": "tracker",
              "config": {"base_url": FIXTURE}, "secret": "fixture-tracker-token"})
    report = "# Bug: 2+2 returns 5\n\nSteps: open /app/calc?a=2&b=2\nExpected: 4\nActual: 5\nEvidence: screenshot attached"
    set_scripts({"qa": [
        {"tool_calls": [call("browser_open", url=f"{FIXTURE}/issues")]},
        {"tool_calls": [call("browser_open", url=f"{FIXTURE}/app/calc?a=2&b=2")]},
        {"tool_calls": [call("browser_screenshot")]},
        {"tool_calls": [call("create_artifact", name="bug-report.md", content=report)]},
        {"tool_calls": [call("tracker_create_issue", title="Bug: 2+2 returns 5", body=report, labels=["bug"])]},
        {"content": "Bug reproduced; report written and submitted after approval."}]})
    quill = client.bot("Quill", model="scripted:qa")
    tid, _ = client.say(quill, "Reproduce the calculator defect and file it")
    drain()
    t = client.task(tid)
    assert t["status"] == "waiting_for_approval"
    assert "Result: 5" in str(t["tool_calls"][1]["result"])
    assert "calc: suspicious result 5" in str(t["tool_calls"][1]["result"]["console"])  # console log evidence
    names = {a["name"].split("-")[0] for a in t["artifacts"]}
    assert {"screenshot", "bug"} <= names
    shot = next(a for a in t["artifacts"] if a["name"].startswith("screenshot"))
    assert client.get(shot["url"]).content[:8] == b"\x89PNG\r\n\x1a\n"
    assert httpx.get(f"{FIXTURE}/admin/state").json()["creates"] == 0  # nothing submitted yet
    appr = client.ok("get", "/api/approvals")[0]
    assert appr["payload"]["title"] == "Bug: 2+2 returns 5"
    client.ok("post", f"/api/approvals/{appr['id']}/decide", json={"decision": "approve", "digest": appr["digest"]})
    drain()
    assert client.task(tid)["status"] == "succeeded"
    assert httpx.get(f"{FIXTURE}/admin/state").json()["issues"][0]["title"] == "Bug: 2+2 returns 5"
