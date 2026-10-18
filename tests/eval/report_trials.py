"""Repeat the real-model sales-report task N times per model and record outcomes (not a unit test).

  python tests/eval/report_trials.py 3 qwen2.5:1.5b qwen2.5:3b
Needs the test database (same env as pytest) and Ollama. Writes docs/eval/report_trials.json.
"""
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # noqa: E402  (sets the isolated test environment)

REQUEST = ("Write a short sales report in markdown. Data: September sales 120, October sales 135; returns September 4, "
           "October 9. Include the percentage change in sales and one recommendation.")


def main(n: int, models: list[str]):
    from localcrew import validation
    from localcrew.config import settings
    from localcrew.db import session_scope
    from localcrew.models import Artifact, Task
    from sqlalchemy import select
    results = []
    for model in models:
        for i in range(n):
            c = conftest.Client()
            bot = c.bot("Atlas", model=model, tools=["create_artifact", "calculate"])
            t0 = time.monotonic()
            tid, _ = c.say(bot, REQUEST, deliverable="sales-report.md")
            conftest.drain()
            secs = round(time.monotonic() - t0, 1)
            t = c.task(tid)
            with session_scope() as db:
                arts = db.scalars(select(Artifact).where(Artifact.task_id == tid).order_by(Artifact.version)).all()
                body = (settings().artifacts / arts[-1].storage_path).read_text("utf-8") if arts else ""
                task = db.get(Task, tid)
                ev = validation.evidence_values([task.objective, *[m["content"] for m in task.checkpoint.get("messages", [])
                                                                    if m["role"] in ("system", "tool")]])
            row = {"model": model, "trial": i + 1, "status": t["status"], "seconds": secs,
                   "used_calculate": any(x["tool"] == "calculate" and x["status"] == "succeeded" for x in t["tool_calls"]),
                   "validation_steps": [s["status"] for s in t["steps"] if s["kind"] == "validate"],
                   "has_12_5": "12.5" in body,
                   "ungrounded_in_final_file": validation.ungrounded(
                       body, ev, validation.derivations(validation.evidence_values([task.objective]))),
                   "structure_issues": validation.structure_issues("sales-report.md", body) if body else ["no file"],
                   "content_issues": validation.content_issues("sales-report.md", body, REQUEST) if body else [],
                   "error": t["error"][:300], "report": body[:1500]}
            results.append(row)
            print(model, i + 1, row["status"], secs, "s calc:", row["used_calculate"], "12.5:", row["has_12_5"],
                  "ungrounded:", row["ungrounded_in_final_file"][:5], row["content_issues"][:1], row["error"][:160])
    out = ROOT / "docs" / "eval" / "report_trials.json"
    out.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "request": REQUEST, "results": results}, indent=2))
    print("written", out)


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2:] or ["qwen2.5:1.5b"])
