"""Remote (OpenAI-compatible) model provider, tested against a MOCK API server (tests/fixtures/fake_openai.py).
Real-provider validation needs your own API key: see docs/MODELS.md "Hosted model APIs"."""
import threading
import time

import pytest
import uvicorn
from conftest import ROOT, call, drain
from test_hardening import REQUEST

PORT = 18900
KEY = "test-remote-key"


@pytest.fixture(scope="module")
def fake_api():
    import sys
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    import fake_openai
    server = uvicorn.Server(uvicorn.Config(fake_openai.app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield fake_openai.STATE
    server.should_exit = True


@pytest.fixture
def remote(fake_api, monkeypatch):
    from localcrew.config import settings
    s = settings()
    for k, v in {"remote_providers_enabled": True, "remote_base_url": f"http://127.0.0.1:{PORT}/v1",
                 "remote_api_key": KEY, "remote_models": "fake-model"}.items():
        monkeypatch.setattr(s, k, v)
    fake_api.update({"script": [], "requests": [], "status": 200, "delay": 0.0})
    return fake_api


def test_remote_disabled_by_default_fails_task_and_keeps_worker(client):
    bot = client.bot(model="remote:fake-model")
    tid, _ = client.say(bot, "hello")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "disabled" in t["error"]
    later = client.bot("Quill")  # the worker survived and still runs other tasks
    tid2, _ = client.say(later, "still alive?")
    drain()
    assert client.task(tid2)["status"] == "succeeded"


def test_remote_task_end_to_end_with_tools_and_validation(client, remote):
    remote["script"] = [
        {"tool_calls": [call("calculate", expression="(135-120)/120*100")]},
        {"tool_calls": [call("create_artifact", name="sales.md",
                             content="# Sales report\n\n- September: 120\n- October: 135\n- Change: +12.5%\n")]},
        {"content": "Sales grew 12.5% from 120 to 135."}]
    bot = client.bot(model="remote:fake-model", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t["error"]
    assert t["tool_calls"][0]["result"]["result"] == 12.5  # fragmented arguments were reassembled
    assert "12.5%" in client.get(t["artifacts"][0]["url"]).text
    reqs = remote["requests"]
    assert reqs[0]["headers"]["authorization"] == f"Bearer {KEY}" and reqs[0]["body"]["model"] == "fake-model"
    msgs = reqs[-1]["body"]["messages"]  # tool results are linked to their calls by id
    ids = {tc["id"] for m in msgs if m["role"] == "assistant" for tc in m.get("tool_calls") or []}
    assert {m["tool_call_id"] for m in msgs if m["role"] == "tool"} <= ids and ids
    assert all(KEY not in str(r["body"]) for r in reqs)  # the key is never part of what the model sees
    from localcrew.db import session_scope
    from sqlalchemy import text
    with session_scope() as db:
        assert KEY not in str(db.execute(text("select data from task_events")).all())


def test_remote_auth_error_fails_without_retry_and_rate_limit_retries(client, remote):
    remote["status"] = 401
    bot = client.bot(model="remote:fake-model")
    tid, _ = client.say(bot, "hi")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "401" in t["error"] and KEY not in t["error"]
    remote["status"] = 429
    tid, _ = client.say(bot, "hi again")
    drain()
    assert client.task(tid)["status"] == "retry_wait"


def test_remote_cancellation_before_first_byte(remote):
    from localcrew.llm import Cancelled
    from localcrew.remote import OpenAICompatibleProvider
    remote["delay"] = 30
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        OpenAICompatibleProvider().chat("remote:fake-model", [{"role": "user", "content": "hi"}], cancel=cancel)
    assert time.monotonic() - t0 < 4


def test_remote_models_listed_with_disclosure(client, remote):
    m = client.ok("get", "/api/models")
    assert any(x["name"] == "remote:fake-model" and x["remote"] for x in m["models"])
    rp = m["remote_providers"]
    assert rp["enabled"] and rp["host"] == "127.0.0.1" and "tool results" in rp["data_leaving_machine"]
    assert KEY not in str(m)


def test_plain_http_to_remote_host_refused(remote, monkeypatch):
    from localcrew.config import settings
    from localcrew.remote import RemoteConfigError, check_config
    monkeypatch.setattr(settings(), "remote_base_url", "http://api.example.com/v1")
    with pytest.raises(RemoteConfigError, match="https"):
        check_config()


def test_message_conversion_handles_compacted_tool_results():
    from localcrew.remote import to_openai
    out = to_openai([{"role": "system", "content": "s"}, {"role": "tool", "content": "orphan", "tool_name": "read_file"},
                     {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a", "arguments": {}}},
                                                                         {"function": {"name": "b", "arguments": {}}}]},
                     {"role": "tool", "content": "ra", "tool_name": "a"}, {"role": "user", "content": "next"}])
    assert out[1]["role"] == "user" and "orphan" in out[1]["content"]  # no dangling tool message
    tool_ids = [m["tool_call_id"] for m in out if m["role"] == "tool"]
    assert tool_ids == ["call_2_0", "call_2_1"]  # unanswered call gets a placeholder so the request stays valid
