"""2. Real local-model task that creates a downloadable report (requires Ollama + the model; slow on CPU).

Run: pytest -m realmodel tests/test_realmodel.py
Asserts pipeline mechanics (plan → run → validate → report → downloadable artifact with provenance),
plus a weak content check. Output *quality* is reported in docs/TEST_REPORT.md, not asserted here.
"""
import os

import httpx
import pytest

from conftest import drain

MODEL = os.environ.get("LOCALCREW_EVAL_MODEL", "qwen2.5:1.5b")


def _ollama_has(model):
    try:
        return any(m["name"] == model for m in httpx.get("http://127.0.0.1:11434/api/tags", timeout=3).json()["models"])
    except httpx.HTTPError:
        return False


@pytest.mark.realmodel
@pytest.mark.skipif(not _ollama_has(MODEL), reason=f"Ollama model {MODEL} not available")
def test_real_model_report(client):
    bot = client.bot("Atlas", model=MODEL, tools=["create_artifact"])
    tid, _ = client.say(bot, "Write a short sales report in markdown. Data: September sales 120, October sales 135. "
                             "Include both numbers and one recommendation.", deliverable="sales-report.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", (t["status"], t["error"])
    assert t["result"]["model"] == MODEL
    art = next(a for a in t["artifacts"] if a["name"] == "sales-report.md")
    body = client.get(art["url"]).content.decode()
    assert "120" in body and "135" in body
    assert art["provenance"]["task_id"] == tid
    print("\n--- real model output ---\n", body)
