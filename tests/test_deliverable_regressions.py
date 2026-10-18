"""Regressions from real-model report trials (docs/eval/history): formatting and repair of deliverables."""
from conftest import call, drain, set_scripts
from test_hardening import REQUEST

FENCE = "`" * 3


def test_embedded_markdown_in_written_file_rejected_then_repaired(client):
    """A file the model writes itself must be plain Markdown; a wrapped one is rejected and repaired."""
    wrapped = f"Here is the report:\n{FENCE}markdown\n# Sales\nSales rose from 120 to 135.\n{FENCE}\nLet me know!"
    set_scripts({"wrap": [{"tool_calls": [call("create_artifact", name="sales.md", content=wrapped)]},
                          {"content": "Done."},
                          {"tool_calls": [call("create_artifact", name="sales.md", content="# Sales\n\nSales rose from 120 to 135.\n")]},
                          {"content": "Sales rose from 120 to 135."}]})
    bot = client.bot(model="scripted:wrap", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t["error"]
    v = [s for s in t["steps"] if s["kind"] == "validate"]
    assert "embedded " + FENCE + "markdown" in " ".join(c["check"] for c in v[0]["data"]["checks"])


def test_markdown_block_extracted_from_chatty_answer(client):
    """Real-trial failure mode: the report arrives inside a fenced markdown block with chat around it. The block is
    extracted (formatting only) and its content is still validated."""
    answer = (f"Here is your report:\n\n{FENCE}markdown\n# Sales report\n\n- September: 120\n- October: 135\n"
              f"- Change: +12.5%\n{FENCE}\n\nLet me know if you need anything else!")
    set_scripts({"chatty": [{"tool_calls": [call("calculate", expression="(135-120)/120*100")]}, {"content": answer}]})
    bot = client.bot(model="scripted:chatty", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t["error"]
    body = client.get(t["artifacts"][0]["url"]).text
    assert body.startswith("# Sales report") and "Let me know" not in body and FENCE not in body


def test_extracted_block_with_wrong_numbers_still_fails(client):
    answer = f"Sure!\n{FENCE}markdown\n# Sales report\n\nSales grew 11.67%.\n{FENCE}"
    set_scripts({"chatbad": [{"content": answer}, {"content": answer}]})
    bot = client.bot(model="scripted:chatbad", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "11.67" in t["error"]


def test_repair_rewrites_answer_derived_deliverable(client):
    """After a repair turn, a corrected final answer must replace the earlier answer-derived file (previously the
    corrected answer was silently ignored because the file already existed)."""
    set_scripts({"rew": [{"content": "# Sales\n\nSales grew 11.67%."},
                         {"tool_calls": [call("calculate", expression="(135-120)/120*100")]},
                         {"content": "# Sales\n\nSales grew 12.5% (from 120 to 135)."}]})
    bot = client.bot(model="scripted:rew", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "succeeded", t["error"]
    latest = max(t["artifacts"], key=lambda a: a["version"])
    assert latest["version"] == 2 and "12.5%" in client.get(latest["url"]).text


def test_wrong_formula_via_calculate_does_not_ground_a_number(client):
    """Real trial (qwen2.5:1.5b): it computed a wrong expression with `calculate` and reported '-20%'. A calculation
    proves arithmetic, not that the formula was right, so the figure must still recompute from the user's data."""
    report = "# Sales report\n\nSales went from 120 to 135, a change of -20%.\n"
    set_scripts({"wrongcalc": [{"tool_calls": [call("calculate", expression="(120-150)/150*100")]},
                               {"tool_calls": [call("create_artifact", name="sales.md", content=report)]},
                               {"content": "Done: -20%."}, {"content": "Done: -20%."}]})
    bot = client.bot(model="scripted:wrongcalc", tools=["create_artifact", "calculate"])
    tid, _ = client.say(bot, REQUEST, deliverable="sales.md")
    drain()
    t = client.task(tid)
    assert t["status"] == "failed" and "-20" in t["error"]


def test_percentage_must_recompute_as_percentage():
    from localcrew import validation as v
    base = v.derivations(v.ordered_values([REQUEST]))
    assert v.ungrounded("Sales rose 15% (from 120 to 135).", v.evidence_values([REQUEST]), base) == ["15"]
    assert v.ungrounded("Sales rose by 15 units (12.5%).", v.evidence_values([REQUEST]), base) == []
    assert v.ungrounded("Returns rose 125% (4 to 9).", v.evidence_values([REQUEST]), base) == []


def test_commentary_and_language_mismatch_rejected():
    from localcrew import validation as v
    assert v.content_issues("r.md", "# Report\n\nIt seems there was an error. Sales 120 to 135.", REQUEST)
    assert v.content_issues("r.md", "# Report\n\nLet me know if you need more! Sales 120 to 135.", REQUEST)
    assert v.content_issues("r.md", "# 销售报告\n9月销售额为120，10月为135，增长12.5%。", REQUEST)
    assert v.content_issues("r.md", "# Sales report\n\nSales rose from 120 to 135 (12.5%).", REQUEST) == []
