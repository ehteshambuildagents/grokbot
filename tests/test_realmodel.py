"""Real local-model tests (require Ollama and the model; slow on CPU). Not mocked.

Run: pytest -m realmodel tests/test_realmodel.py   (LOCALCREW_EVAL_MODEL selects the model)
"""
import os
import threading
import time
import uuid

import httpx
import pytest
from conftest import drain

MODEL = os.environ.get("LOCALCREW_EVAL_MODEL", "qwen2.5:3b")


def _ollama_has(model):
    try:
        return any(m["name"] == model for m in httpx.get("http://127.0.0.1:11434/api/tags", timeout=3).json()["models"])
    except httpx.HTTPError:
        return False


pytestmark = [pytest.mark.realmodel,
              pytest.mark.skipif(not _ollama_has(MODEL), reason=f"Ollama model {MODEL} not available")]


def test_real_model_report_is_correct_or_rejected(client):
    """End to end with the real model: task → tools → validation → downloadable report.
    A report may only succeed if it passes structure and numeric-grounding validation; the growth figure must be
    the correct 12.5%. A wrong report must end as `failed` (never `succeeded` because a file exists)."""
    from localcrew import validation
    bot = client.bot("Atlas", model=MODEL, tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, "Write a short sales report in markdown. Data: September sales 120, October sales 135. "
                             "Include the percentage change in sales and one recommendation.", deliverable="sales-report.md")
    drain()
    t = client.task(tid)
    print("\nstatus:", t["status"], "| error:", t["error"][:300])
    if t["status"] == "succeeded":
        art = max((a for a in t["artifacts"] if a["name"] == "sales-report.md"), key=lambda a: a["version"])
        body = client.get(art["url"]).text
        print(body)
        assert "12.5" in body, "succeeded without the correct growth figure"
        assert validation.structure_issues("sales-report.md", body) == []
        assert art["provenance"]["task_id"] == tid
    else:
        assert t["status"] == "failed" and t["error"].startswith("validation failed"), t["error"]
        pytest.fail(f"{MODEL} could not produce a validated report (correctly rejected): {t['error'][:300]}")


def test_real_ollama_cancellation_stops_generation():
    """Cancel during prompt evaluation of a long, never-seen prompt; Ollama must stop (follow-up is not queued)."""
    from localcrew.llm import Cancelled, OllamaProvider
    p = OllamaProvider()
    p.chat(MODEL, [{"role": "user", "content": "hi"}], options={"num_predict": 1})  # model loaded
    nonce = uuid.uuid4().hex  # defeats Ollama's prompt cache so the cancel lands in prompt evaluation
    long = f"[{nonce}] Summarize this log:\n" + "\n".join(f"line {i}: ok latency {i % 97} ms" for i in range(1500))
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        p.chat(MODEL, [{"role": "user", "content": long}], cancel=cancel, options={"num_predict": 2000, "num_ctx": 8192})
    took = time.monotonic() - t0
    t1 = time.monotonic()
    p.chat(MODEL, [{"role": "user", "content": "Say OK"}], options={"num_predict": 3})
    follow = time.monotonic() - t1
    print(f"\ncancelled after {took:.2f}s; follow-up request {follow:.2f}s")
    assert took < 5 and follow < 15
