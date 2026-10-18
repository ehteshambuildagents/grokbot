"""Tests that use real separate processes: API server restart, worker subprocess, worker crash."""
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import Client, call, drain, set_scripts
from sqlalchemy import select

API_PORT = 18000
API = f"http://127.0.0.1:{API_PORT}"


def wait_for(fn, timeout=60, every=0.3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("condition not met in time")


def start_api(procs):
    procs.start("api", ["-m", "uvicorn", "localcrew.api:app", "--port", str(API_PORT), "--app-dir", "services/api"],
                f"{API}/api/setup")


def http_login(email):
    c = httpx.Client(base_url=API)
    r = c.post("/api/auth/login", json={"email": email, "password": "correct horse battery"})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c


# 1. Bot creation and persistence after restart
def test_bot_persists_across_api_restart(procs):
    u = Client()
    start_api(procs)
    c = http_login(u.email)
    b = c.post("/api/bots", json={"name": "Persisto", "role": "Archivist"}).json()
    procs.kill("api")
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{API}/api/setup", timeout=1)
    start_api(procs)
    c = http_login(u.email)  # sessions persist in the DB too, but log in fresh to prove the account persisted
    got = c.get(f"/api/bots/{b['id']}").json()
    assert got["name"] == "Persisto" and got["role"] == "Archivist"
    procs.kill("api")


# 6. Scheduled run with the frontend (and API) closed
def test_scheduled_routine_runs_with_frontend_closed(procs):
    u = Client()
    set_scripts({"r": [{"content": "Daily digest: nothing new."}]})
    bot = u.bot("Atlas", model="scripted:r")
    prev = u.ok("post", "/api/routines/parse", json={"text": "every minute"})
    assert prev["cron"] == "* * * * *" and len(prev["next_runs"]) == 3
    assert u.post("/api/routines", json={"name": "Digest", "bot_id": bot["id"], "prompt": "digest",
                                         "schedule_text": "every minute"}).status_code == 400  # not confirmed
    r = u.ok("post", "/api/routines", status=201, json={"name": "Digest", "bot_id": bot["id"], "prompt": "digest",
                                                         "schedule_text": "every minute", "confirmed": True,
                                                         "deliverable": "digest.md"})
    assert "api" not in procs.p  # no API server and no browser client running
    procs.start("worker", ["services/worker/worker.py"])
    from localcrew.db import session_scope
    from localcrew.models import RoutineRun, Task

    def done():
        with session_scope() as db:
            run = db.scalar(select(RoutineRun).where(RoutineRun.routine_id == r["id"]))
            return run and run.task_id and db.get(Task, run.task_id).status == "succeeded" and run
    run = wait_for(done, timeout=100, every=1)
    procs.kill("worker")
    runs = u.ok("get", f"/api/routines/{r['id']}/runs")
    assert runs[0]["status"] == "succeeded" and runs[0]["trigger"] in ("schedule", "missed")
    t = u.task(runs[0]["task_id"])
    assert t["artifacts"][0]["name"] == "digest.md"
    del run


# 7. Worker failure and checkpoint recovery
def test_worker_crash_recovers_from_checkpoint(procs):
    u = Client()
    set_scripts({"crash": [{"tool_calls": [call("write_file", path="crash/step1.txt", content="one")]},
                           {"delay": 8, "tool_calls": [call("create_artifact", name="after.md", content="resumed")]},
                           {"content": "completed after recovery"}]})
    bot = u.bot("Atlas", model="scripted:crash")
    tid, _ = u.say(bot, "two steps")
    procs.start("worker", ["services/worker/worker.py"])
    wait_for(lambda: any(c["status"] == "succeeded" for c in u.task(tid)["tool_calls"]), timeout=60)
    time.sleep(1.5)  # now inside the 8 s model "thinking" delay after the checkpoint
    procs.kill("worker")  # hard crash: no cleanup, lease left behind
    assert u.task(tid)["status"] == "running"
    procs.start("worker2", ["services/worker/worker.py"])
    t = wait_for(lambda: (x := u.task(tid))["status"] == "succeeded" and x, timeout=90)
    procs.kill("worker2")
    assert t["attempts"] == 1
    ev = [e["type"] for e in u.ok("get", f"/api/tasks/{tid}/events.json")]
    assert "recovered" in ev
    seqs = [c["seq"] for c in t["tool_calls"]]
    assert seqs == [1, 2]  # step 1 was not executed twice
    from localcrew import workspace
    assert not (workspace.root(u.user_id) / ".versions").exists()  # step1.txt never rewritten
    assert t["artifacts"][0]["name"] == "after.md"


# Routine semantics (in-process scheduler tick; deterministic clock)
def _routine(u, policy, text="every hour"):
    set_scripts({"q": [{"content": "ok"}]})
    bot = u.bot("Atlas", model="scripted:q")
    return u.ok("post", "/api/routines", status=201, json={"name": f"R-{policy}", "bot_id": bot["id"], "prompt": "p",
                                                            "schedule_text": text, "confirmed": True,
                                                            "missed_policy": policy})


def _set_next(rid, when):
    from localcrew.db import session_scope
    from localcrew.models import Routine
    with session_scope() as db:
        db.get(Routine, rid).next_run_at = when


def test_missed_runs_policy(client):
    from localcrew import routines
    from localcrew.db import session_scope, utcnow
    from localcrew.models import RoutineRun
    base = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=5)
    now = base + timedelta(hours=5, minutes=30)  # deterministic clock: host was asleep for 5 hourly slots
    r1 = _routine(client, "run_once")
    _set_next(r1["id"], base)
    routines.tick(now)
    runs = client.ok("get", f"/api/routines/{r1['id']}/runs")
    assert len(runs) == 1 and runs[0]["trigger"] == "missed"  # one catch-up run, not five
    r2 = _routine(Client(), "skip")
    _set_next(r2["id"], base)
    routines.tick(now)
    with session_scope() as db:
        runs = db.scalars(select(RoutineRun).where(RoutineRun.routine_id == r2["id"])).all()
        assert [x.status for x in runs] == ["skipped_missed"] and runs[0].task_id is None
    nxt = client.ok("get", "/api/routines")[0]["next_run_at"]
    assert datetime.fromisoformat(nxt) > utcnow()


def test_overlap_prevented_and_pause(client):
    r = _routine(client, "run_once")
    a = client.ok("post", f"/api/routines/{r['id']}/run-now")
    b = client.ok("post", f"/api/routines/{r['id']}/run-now")
    assert a["status"] == "started" and b["status"] == "skipped_overlap"
    drain()
    c = client.ok("post", f"/api/routines/{r['id']}/run-now")
    assert c["status"] == "started"
    client.ok("patch", f"/api/routines/{r['id']}", json={"enabled": False})
    from localcrew import routines
    from localcrew.db import utcnow
    _set_next(r["id"], utcnow() - timedelta(minutes=1))
    assert routines.tick() == 0  # paused routines do not fire


def test_dst_and_timezones():
    from localcrew.routines import next_runs, parse_schedule
    assert parse_schedule("every weekday at 9am") == "0 9 * * 1-5"
    assert parse_schedule("every monday at 18:30") == "30 18 * * 1"
    assert parse_schedule("every 15 minutes") == "*/15 * * * *"
    assert parse_schedule("0 7 * * *") == "0 7 * * *"
    with pytest.raises(ValueError):
        parse_schedule("whenever you feel like it")
    # Asia/Karachi: UTC+5, no DST
    k = next_runs("0 9 * * *", "Asia/Karachi", datetime(2026, 10, 18, 0, 0, tzinfo=UTC), 1)[0]
    assert k == datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    # New York spring-forward 2026-03-08: 02:30 does not exist → runs at 03:30 EDT (07:30 UTC), once
    ny = next_runs("30 2 * * *", "America/New_York", datetime(2026, 3, 7, 12, 0, tzinfo=UTC), 3)
    assert ny[0] == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert len(set(ny)) == 3 and ny[1] == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    # Fall-back 2026-11-01: 01:30 happens twice → runs once (first occurrence, EDT 05:30 UTC)
    fb = next_runs("30 1 * * *", "America/New_York", datetime(2026, 10, 31, 12, 0, tzinfo=UTC), 2)
    assert fb == [datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 2, 6, 30, tzinfo=UTC)]


def test_webhook_auth_and_dedup(client):
    from fastapi.testclient import TestClient
    from localcrew.api import app
    set_scripts({"w": [{"content": "handled"}]})
    bot = client.bot("Atlas", model="scripted:w")
    r = client.ok("post", "/api/routines", status=201, json={"name": "Hook", "bot_id": bot["id"], "prompt": "triage",
                                                              "trigger": "webhook"})
    secret = r["webhook_secret_shown_once"]
    assert secret and "webhook_secret_shown_once" not in client.ok("get", "/api/routines")[0]
    anon = TestClient(app)

    def send(body=b'{"event":"push"}', delivery="d1", ts=None, key=secret):
        ts = str(int(ts or time.time()))
        sig = hmac.new(key.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return anon.post(r["webhook_url"], content=body, headers={"X-LocalCrew-Timestamp": ts,
                         "X-LocalCrew-Signature": sig, "X-LocalCrew-Delivery": delivery})
    first = send()
    assert first.status_code == 200 and first.json()["status"] == "started"
    assert send().json() == {"duplicate": True}
    assert send(key="wrong").status_code == 401
    assert send(delivery="d2", ts=time.time() - 3600).status_code == 401
    drain()
    t = client.task(first.json()["task_id"])
    assert t["status"] == "succeeded" and "untrusted data" in t["objective"]
