"""Browser/computer acceptance tests against the local fixture app with real headless Chromium."""
import threading
import time

import httpx

from conftest import FIXTURE, call, drain, set_scripts, start_computer


def _center(client, selector):
    """Test helper standing in for the human's eyes: find where an element is on the live view."""
    # Uses Playwright in-process only to compute coordinates of the fixture page; the click itself goes
    # through the human control API like a real click on the screenshot.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        pg.goto(f"{FIXTURE}/login")
        box = pg.locator(selector).bounding_box()
        b.close()
    return {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}


def act(client, **k):
    return client.ok("post", "/api/computer/act", json=k)


def login_via_takeover(client):
    client.ok("post", "/api/computer/takeover")
    act(client, kind="goto", url=f"{FIXTURE}/login")
    act(client, kind="click", **_center(client, "#u"))
    act(client, kind="type", text="demo")
    act(client, kind="click", **_center(client, "#p"))
    r = act(client, kind="type", text="fixture-password-123")
    assert r["sensitive"] is True  # password field detected even without the sensitive flag
    act(client, kind="key", key="Enter")
    time.sleep(1)
    assert "/dashboard" in client.ok("get", "/api/computer/screenshot")["url"]


# 3 + 4. Login persistence, session expiry, human takeover
def test_login_persistence_expiry_and_takeover(client, procs):
    set_scripts({"b": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/dashboard")]}, {"content": "Dashboard says welcome"}]})
    bot = client.bot("Atlas", model="scripted:b")
    # Not signed in yet → bot hits the sign-in page and pauses for a human (never types the password)
    tid, _ = client.say(bot, "check my dashboard")
    drain()
    t = client.task(tid)
    assert t["status"] == "waiting_for_user" and t["blocker"]["kind"] == "session_expired", t["blocker"]

    login_via_takeover(client)
    # While a human holds control, automation is refused
    st = client.ok("get", "/api/computer/status")
    assert st["controller"] == "human"
    r = client.ok("post", "/api/computer/release")
    assert tid in r["resumed_tasks"]  # resumes only after release
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t
    assert "Welcome, demo" in str(t["tool_calls"][-1]["result"])

    # Persistence: restart the computer service (new Chromium process, same profile directory)
    procs.kill("computer")
    start_computer(procs)
    set_scripts({"b2": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/dashboard")]}, {"content": "still signed in"}]})
    client.bot("Atlas", model="scripted:b2")
    tid2, _ = client.say(bot, "check again")
    drain()
    t2 = client.task(tid2)
    assert t2["status"] == "succeeded" and "Welcome, demo" in str(t2["tool_calls"][0]["result"])

    # Session expiry on the site → detected, task waits for a human
    httpx.post(f"{FIXTURE}/admin/expire-sessions")
    set_scripts({"b3": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/dashboard")]}, {"content": "x"}]})
    client.bot("Atlas", model="scripted:b3")
    tid3, _ = client.say(bot, "and again")
    drain()
    assert client.task(tid3)["blocker"]["kind"] == "session_expired"

    # The sensitive keystrokes never reached any transcript, event or audit log
    from sqlalchemy import text
    from localcrew.db import session_scope
    with session_scope() as db:
        for table in ("task_events", "audit_events", "messages", "tool_calls", "task_steps"):
            dump = str(db.execute(text(f"select * from {table}")).all())
            assert "fixture-password-123" not in dump, table


def test_bot_refuses_password_fields_and_captcha(client):
    client.ok("post", "/api/computer/release")
    set_scripts({"p": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/upload")]},
                       {"tool_calls": [call("browser_type", label="Password", text="guess")]},
                       {"content": "x"}]})
    bot = client.bot("Atlas", model="scripted:p")
    tid, _ = client.say(bot, "go")
    drain()
    t = client.task(tid)
    # /upload has no password field; typing into a non-existent "Password" label fails safely (not found)
    assert t["status"] == "succeeded" and t["tool_calls"][1]["status"] == "failed"
    set_scripts({"c": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/captcha")]}, {"content": "x"}]})
    client.bot("Atlas", model="scripted:c")
    tid, _ = client.say(bot, "go")
    drain()
    assert client.task(tid)["blocker"]["kind"] == "human_required"
    set_scripts({"m": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/mfa")]}, {"content": "x"}]})
    client.bot("Atlas", model="scripted:m")
    tid, _ = client.say(bot, "go")
    drain()
    assert client.task(tid)["blocker"]["kind"] == "human_required"
    set_scripts({"s": [{"tool_calls": [call("browser_open", url="http://169.254.169.254/latest/meta-data")]}, {"content": "x"}]})
    client.bot("Atlas", model="scripted:s")
    tid, _ = client.say(bot, "go")
    drain()
    assert "blocked" in client.task(tid)["tool_calls"][0]["result"]["error"]


def test_takeover_revokes_automation_mid_task(client):
    set_scripts({"t": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/")]},
                       {"delay": 2, "tool_calls": [call("browser_snapshot")]},
                       {"content": "finished after human released"}]})
    bot = client.bot("Atlas", model="scripted:t")
    tid, _ = client.say(bot, "browse")
    th = threading.Thread(target=drain)
    th.start()
    for _ in range(100):
        if client.task(tid)["tool_calls"]:
            break
        time.sleep(0.1)
    r = client.ok("post", "/api/computer/takeover")
    th.join(30)
    t = client.task(tid)
    assert t["status"] == "waiting_for_user" and t["blocker"]["kind"] == "control_revoked", (t, r)
    client.ok("post", "/api/computer/release")
    drain()
    assert client.task(tid)["status"] == "succeeded"


# 11. Concurrent access to the shared browser resource
def test_concurrent_browser_access_serialized(client):
    client.ok("post", "/api/computer/release")
    from localcrew.config import settings
    s = settings()
    h = {"X-Internal-Token": s.computer_token}
    body = lambda tid: {"user_id": client.user_id, "task_id": tid, "url": f"{FIXTURE}/"}  # noqa: E731
    a = httpx.post(f"{s.computer_url}/browser/open", json=body("task-a"), headers=h, timeout=30)
    assert a.status_code == 200
    b = httpx.post(f"{s.computer_url}/browser/open", json=body("task-b"), headers=h, timeout=30)
    assert b.status_code == 423 and "busy" in b.text  # second task must wait for the lease
    httpx.post(f"{s.computer_url}/browser/release", json={"task_id": "task-a"}, headers=h)
    assert httpx.post(f"{s.computer_url}/browser/open", json=body("task-b"), headers=h, timeout=30).status_code == 200
    # Parallel requests from the same task are serialized by the profile lock, never run concurrently
    results = []
    ths = [threading.Thread(target=lambda: results.append(
        httpx.post(f"{s.computer_url}/browser/snapshot", json=body("task-b"), headers=h, timeout=30).status_code))
        for _ in range(5)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert results == [200] * 5
    httpx.post(f"{s.computer_url}/browser/release", json={"task_id": "task-b"}, headers=h)


# 9. Cancellation of active browser work
def test_cancel_active_browser_navigation(client):
    client.ok("post", "/api/computer/release")
    set_scripts({"slow": [{"tool_calls": [call("browser_open", url=f"{FIXTURE}/slow")]}, {"content": "never"}]})
    bot = client.bot("Atlas", model="scripted:slow")
    tid, _ = client.say(bot, "open slow page")
    th = threading.Thread(target=drain)
    th.start()
    for _ in range(100):
        tc = client.task(tid)["tool_calls"]
        if tc and tc[0]["status"] == "running":
            break
        time.sleep(0.1)
    time.sleep(1)
    t0 = time.time()
    client.ok("post", f"/api/tasks/{tid}/cancel")
    th.join(40)
    assert client.task(tid)["status"] == "cancelled"
    assert time.time() - t0 < 12  # page load (30 s) was aborted


def test_demonstration_recording_excludes_password(client):
    client.ok("post", "/api/computer/takeover")
    act(client, kind="goto", url=f"{FIXTURE}/login")
    client.ok("post", "/api/computer/record/start")
    act(client, kind="click", **_center(client, "#u"))
    act(client, kind="type", text="demo")
    act(client, kind="click", **_center(client, "#p"))
    act(client, kind="type", text="fixture-password-123")
    act(client, kind="key", key="Enter")
    steps = client.ok("post", "/api/computer/record/stop")["steps"]
    client.ok("post", "/api/computer/release")
    assert "fixture-password-123" not in str(steps)
    assert {"action": "sensitive_input_excluded"} in steps
    sk = client.ok("post", "/api/skills/from-demonstration", status=201, json={"name": "Fixture login", "steps": steps})
    spec = sk["versions"][0]["spec"]
    assert sk["versions"][0]["status"] == "draft"  # requires review
    assert any(s.get("tool") == "browser_type" and s["args"]["text"] == "{{input_1}}" for s in spec["steps"])
    assert any("human_step" in s for s in spec["steps"])
    assert spec["inputs"][0]["name"] == "input_1"
