"""Regression tests for the verification pass: link escapes, DNS rebinding, terminal limits, output validation."""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURE, FIXTURE_PORT, ROOT, call, drain, set_scripts

IS_WIN = os.name == "nt"


# ---------------------------------------------------------------- link escapes

def _junction(link: Path, target: Path):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not IS_WIN, reason="junctions are Windows-only (symlink test covers POSIX)")
def test_junction_escape_blocked_everywhere(client):
    from localcrew import workspace
    outside = Path(tempfile.mkdtemp())
    (outside / "secret.txt").write_text("TOP SECRET outside workspace")
    _junction(workspace.root(client.user_id) / "jn", outside)
    assert workspace.search(client.user_id, "TOP SECRET") == []  # was leaking before the fix
    assert "jn" not in [e["name"] for e in client.ok("get", "/api/files")]
    assert client.get("/api/files/content?path=jn/secret.txt").status_code == 400
    assert client.put("/api/files", json={"path": "jn/new.txt", "content": "x"}).status_code == 400
    assert not (outside / "new.txt").exists()


def test_hardlink_alias_refused(client):
    from localcrew import workspace
    outside = Path(tempfile.mkdtemp()) / "data.txt"
    outside.write_text("outside data")
    try:
        os.link(outside, workspace.root(client.user_id) / "alias.txt")
    except OSError:
        pytest.skip("hard links not supported on this filesystem")
    assert client.get("/api/files/content?path=alias.txt").status_code == 400
    assert workspace.search(client.user_id, "outside data") == []


# ---------------------------------------------------------------- DNS rebinding

@pytest.fixture
def rebinding_dns(monkeypatch):
    """rebind.test resolves to a public address on the first lookup and to 127.0.0.1 afterwards."""
    real = socket.getaddrinfo
    calls = {"n": 0}

    def fake(host, port, *a, **kw):
        if host == "rebind.test":
            calls["n"] += 1
            ip = "93.184.215.14" if calls["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]
        return real(host, port, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


def test_dns_rebinding_blocked_for_http_client(rebinding_dns):
    from localcrew.security import check_url, safe_http_client
    url = f"http://rebind.test:{FIXTURE_PORT}/health"
    # The attack works against "check, then connect with a normal client" (the previous implementation):
    check_url(url)
    assert httpx.get(url, timeout=5).text == "ok"
    # The pinned client resolves once at connect time and refuses the internal address.
    rebinding_dns["n"] = 0
    check_url(url)
    with safe_http_client(timeout=5) as c, pytest.raises(httpx.ConnectError, match="blocked"):
        c.get(url)


def test_dns_rebinding_blocked_in_fetch_tool(client, rebinding_dns):
    set_scripts({"rb": [{"tool_calls": [call("fetch_url", url=f"http://rebind.test:{FIXTURE_PORT}/health")]}, {"content": "x"}]})
    bot = client.bot(model="scripted:rb", tools=["fetch_url"])
    tid, _ = client.say(bot, "fetch")
    drain()
    tc = client.task(tid)["tool_calls"][0]
    assert tc["status"] == "failed" and "blocked" in tc["result"]["error"]


def test_egress_proxy_pins_resolution(rebinding_dns):
    sys.path.insert(0, str(ROOT / "services" / "computer"))
    from egress_proxy import EgressProxy
    loop = asyncio.new_event_loop()
    proxy = loop.run_until_complete(EgressProxy().start())
    threading.Thread(target=loop.run_forever, daemon=True).start()
    try:
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy.port}", timeout=10) as c:
            assert c.get(f"{FIXTURE}/health").text == "ok"  # allowlisted fixture passes through
            rebinding_dns["n"] = 1  # public answer already consumed by a "check"; the proxy resolves again
            assert c.get(f"http://rebind.test:{FIXTURE_PORT}/health").status_code == 403
            assert c.get("http://169.254.169.254/latest/meta-data").status_code == 403
            with pytest.raises(httpx.ProxyError):
                c.get("https://127.0.0.1:5432/")  # CONNECT to an internal port refused
        assert any("rebind.test" in b for b in proxy.blocked)
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_browser_traffic_goes_through_egress_proxy(client):
    from localcrew.config import settings
    s = settings()
    h = {"X-Internal-Token": s.internal_token()}
    before = (httpx.get(f"{s.computer_url}/health").json().get("egress_proxy") or {}).get("requests", 0)
    r = httpx.post(f"{s.computer_url}/browser/open", headers=h, timeout=30,
                   json={"user_id": client.user_id, "task_id": "proxy-check", "url": f"{FIXTURE}/"})
    assert r.status_code == 200
    httpx.post(f"{s.computer_url}/browser/release", headers=h, json={"task_id": "proxy-check"})
    after = httpx.get(f"{s.computer_url}/health").json()["egress_proxy"]["requests"]
    assert after > before  # Chromium's loopback request was proxied (not bypassed)


# ---------------------------------------------------------------- terminal limits

def _run(client, argv, timeout_s=30):
    from localcrew.config import settings
    s = settings()
    r = httpx.post(f"{s.computer_url}/terminal/run", headers={"X-Internal-Token": s.internal_token()}, timeout=timeout_s + 30,
                   json={"user_id": client.user_id, "task_id": "limits", "argv": argv, "timeout_s": timeout_s})
    assert r.status_code == 200, r.text
    return r.json()


def test_terminal_memory_limit(client):
    r = _run(client, [sys.executable, "-c", "b = bytearray(1536 * 1024 * 1024); print('ALLOC' + '_OK')"])
    assert r["exit_code"] != 0 and "MemoryError" in r["output"] and "ALLOC_OK" not in r["output"]


def test_terminal_large_output_is_drained_and_capped(client):
    r = _run(client, [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5_000_000); print('end')"], 20)
    assert r["exit_code"] == 0 and not r["timed_out"] and r["seconds"] < 15
    assert r["output_truncated"] is True and len(r["output"]) <= 65536


@pytest.mark.skipif(not IS_WIN, reason="per-command process cap is a Job Object limit; POSIX RLIMIT_NPROC is per-user")
def test_terminal_process_limit(client):
    code = ("import subprocess, sys\nps = []\nfor i in range(100):\n"
            "    try: ps.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']))\n"
            "    except OSError: print('blocked at', i); break\n")
    r = _run(client, [sys.executable, "-c", code])
    assert "blocked at" in r["output"]


def test_terminal_requires_internal_token(client):
    from localcrew.config import settings
    r = httpx.post(f"{settings().computer_url}/terminal/run", json={"user_id": client.user_id, "argv": ["whoami"]})
    assert r.status_code == 401


# ---------------------------------------------------------------- calculation & validation

def test_calculator_is_safe_and_exact():
    from localcrew.calc import CalcError, evaluate
    assert evaluate("(135-120)/120*100") == 12.5 and evaluate("avg(4, 9)") == 6.5
    for bad in ["__import__('os').system('x')", "(1).__class__", "9**9**9", "open('x')", "1/0", "lambda: 1"]:
        with pytest.raises(CalcError):
            evaluate(bad)


BAD_REPORT = ("| Month | Sales | Returns | |---|---|---| | September | $120 | 0 | |---|---|---| "
              "| Percentage Change | 11.67% | 100% |")
GOOD_REPORT = "# Sales report\n\n| Month | Sales | Returns |\n| --- | --- | --- |\n| September | 120 | 4 |\n| October | 135 | 9 |\n\nSales grew 12.5%.\n"
REQUEST = "Write a sales report. Data: September sales 120, October sales 135; returns Sep 4, Oct 9. Include the growth %."


def test_wrong_numbers_trigger_repair_then_success(client):
    """The exact malformed, miscalculated report from the earlier real-model run is rejected; the repaired one passes."""
    set_scripts({"fix": [
        {"tool_calls": [call("create_artifact", name="sales.md", content=BAD_REPORT)]},
        {"content": "Report written: sales grew 11.67%."},
        {"tool_calls": [call("calculate", expression="(135-120)/120*100")]},
        {"tool_calls": [call("create_artifact", name="sales.md", content=GOOD_REPORT)]},
        {"content": "Corrected: sales grew 12.5%."}]})
    bot = client.bot(model="scripted:fix", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t["error"]
    v = [s for s in t["steps"] if s["kind"] == "validate"]
    assert v[0]["status"] == "failed" and v[-1]["status"] == "done"
    failed_checks = " ".join(c["check"] for c in v[0]["data"]["checks"] if not c["ok"])
    assert "11.67" in failed_checks and "malformed table" in failed_checks
    latest = max((a for a in t["artifacts"] if a["name"] == "sales.md"), key=lambda a: a["version"])
    assert latest["version"] == 2 and "12.5%" in client.get(latest["url"]).text
    assert t["tool_calls"][1]["result"]["result"] == 12.5


def test_unrepaired_wrong_numbers_fail_even_though_file_exists(client):
    set_scripts({"nofix": [
        {"tool_calls": [call("create_artifact", name="sales.md", content="# Report\n\nSales grew 11.67%.\n")]},
        {"content": "Done."}, {"content": "Still 11.67%."}]})
    bot = client.bot(model="scripted:nofix", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "11.67" in t["error"] and "numbers not found" in t["error"]
    assert t["artifacts"]  # a file was created, and the task is still not a success


def test_malformed_csv_and_fenced_markdown_rejected():
    from localcrew.validation import structure_issues
    assert structure_issues("a.csv", "a,b\n1,2,3\n")
    assert structure_issues("a.md", "```markdown\n# Report\n```")
    assert structure_issues("a.json", "{not json")
    assert structure_issues("a.md", "# Ok\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n") == []


def test_plain_answer_with_unverified_numbers_is_labelled(client):
    set_scripts({"chat": [{"content": "Your total is 37."}]})
    bot = client.bot(model="scripted:chat")
    tid, conv = client.say(bot, "Add 12 and 24")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and "Unverified" in t["result"]["answer"] and "37" in t["result"]["answer"]


def test_non_answer_deliverable_is_rejected(client):
    """Real-model trial regression: a 'please provide instructions' reply must not succeed as a report."""
    set_scripts({"meta": [{"content": "To respond, I need a brief plan. Please provide a clear instruction."},
                          {"content": "Please provide a clear instruction."}]})
    bot = client.bot(model="scripted:meta", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "asks for clarification" in t["error"]


def test_empty_model_reply_is_retried(client):
    set_scripts({"empty": [{"content": ""}, {"content": "Here is the answer: nothing to compute."}]})
    bot = client.bot(model="scripted:empty")
    tid, _ = client.say(bot, "say something")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded" and t["result"]["answer"].startswith("Here is the answer")


def test_wrong_percentage_after_correct_calculation_is_rejected(client):
    """Real trial regression (qwen2.5:3b): calculate returned 12.5 but the report said 25%. It was accepted because
    the clock time in the system prompt and the expression's constant 100 leaked into the evidence."""
    from localcrew import validation
    report = ("## Sales Data\n- September Sales: 120\n- October Sales: 135\n- September Returns: 4\n- October Returns: 9\n\n"
              "## Percentage Change\nThe sales percentage change from September to October is **25%**.\n")
    ev = validation.evidence_values(["Current time: 2026-10-18 10:25 (Asia/Karachi).", REQUEST,
                                     '{"expression": "(135-120)/120*100", "result": 12.5}'])
    assert "25" in validation.ungrounded(report, ev, validation.derivations(validation.evidence_values([REQUEST, "12.5"])))
    set_scripts({"pct": [{"tool_calls": [call("calculate", expression="(135-120)/120*100")]},
                         {"tool_calls": [call("create_artifact", name="sales.md", content=report)]},
                         {"content": "Sales grew 25%."}, {"content": "Sales grew 25%."}]})
    bot = client.bot(model="scripted:pct", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "25" in t["error"]


def test_wrong_returns_increase_is_rejected(client):
    """Real trial regression (qwen2.5:3b): '+50%' returns increase (correct: +125%) was accepted because the
    recomputation multiplied an input by a calculate result (4 × 12.5)."""
    from localcrew import validation
    report = ("# Sales report\n\n- September Sales: 120\n- October Sales: 135\n- Percentage Change: +12.5%\n"
              "- September Returns: 4\n- October Returns: 9\n- Increase in Returns: +50%\n")
    ev = validation.evidence_values([REQUEST, '{"result": 12.5}'])
    assert validation.ungrounded(report, ev, validation.derivations(validation.evidence_values([REQUEST]))) == ["50"]
    ok = report.replace("+50%", "+125%")
    assert validation.ungrounded(ok, ev, validation.derivations(validation.evidence_values([REQUEST]))) == []
