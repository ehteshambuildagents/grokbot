# Acceptance test report

Date: 2026-10-18 (Asia/Karachi). Host: Windows 11 Pro 10.0.26200, Intel i7-1265U, 15.6 GB RAM, no GPU.
Python 3.13.15, Node 24.19.0, PostgreSQL 17.6 (portable), Ollama 0.40.5, Playwright 1.64 / Chromium 156.

## Summary

| Suite | Command | Result |
|---|---|---|
| Pytest acceptance (incl. real-model test) | `.venv/Scripts/python -m pytest` | **54 passed, 1 skipped** in 264.8 s ([log](test-runs/2026-10-18-pytest.txt)) |
| Playwright E2E, desktop + phone | `cd apps/web && npx playwright test` | **6 passed** in 14.3 s |
| Model eval (smoke, real models) | `python tests/eval/model_eval.py qwen2.5:1.5b qwen2.5:3b` | 1.5b: 5/6, 3b: 5/6 ([json](eval/model_eval_results.json)) |
| Web typecheck + build | `cd apps/web && npm run build` | passed |

Skipped: `test_symlink_escape_rejected`. This Windows account cannot create symlinks (no developer mode).
The guard is implemented, but this result counts as **unverified**.

No flaky-test retries are configured (`retries: 0` in Playwright; no rerun plugin in pytest). Defects found while
writing the tests were fixed in code, not retried away (see "Defects found" below).

## The 16 required scenarios

"MOCK" = deterministic scripted model drives the steps. Everything else in those tests is real: PostgreSQL,
Chromium, subprocesses, HTTP fixture app.

| # | Scenario | Test(s) | Result |
|---|---|---|---|
| 1 | Bot creation and persistence after restart | `test_bot_crud_versions_duplicate_delete`, `test_bot_persists_across_api_restart` (API process killed and restarted) | pass |
| 2 | Real local-model task creating a downloadable report | `test_real_model_report` (qwen2.5:1.5b, 15.7 s) + manual UI run (165.5 s) | pass (mechanics); content quality poor, see below |
| 3 | Browser login persistence (fixture account) | `test_login_persistence_expiry_and_takeover`: human sign-in via takeover, computer service restarted, bot still signed in | pass |
| 4 | Session expiry and human takeover | same test (fixture sessions expired → `session_expired`), `test_takeover_revokes_automation_mid_task` | pass |
| 5 | Bot handoff with evidence preserved | `test_handoff_creates_child_with_evidence`, `test_handoff_loop_prevention` (MOCK) | pass |
| 6 | Scheduled run with the frontend closed | `test_scheduled_routine_runs_with_frontend_closed`: only the worker process running, no API, no browser | pass |
| 7 | Worker failure and checkpoint recovery | `test_worker_crash_recovers_from_checkpoint`: worker hard-killed mid-task; second worker resumes; step 1 not re-executed | pass |
| 8 | Ambiguous external write without duplicates | `test_ambiguous_write_reconciled_no_duplicate` (502 after commit), `test_ambiguous_write_after_crash_never_blindly_retried`; fixture counts exactly 1 create | pass |
| 9 | Cancellation of active browser and terminal work | `test_cancel_active_browser_navigation` (30 s page aborted < 12 s), `test_cancel_running_terminal_command` (60 s process tree killed < 15 s) | pass |
| 10 | Approval denial, expiry, replay, changed payload | `test_approval_denied`, `test_approval_expired`, `test_approval_card_and_allow_once` (replay), `test_changed_payload_invalidates_approval` | pass |
| 11 | Concurrent access to the shared browser | `test_concurrent_browser_access_serialized` (second task gets 423; parallel calls serialized) | pass |
| 12 | Memory correction and deletion | `test_memory_correct_disable_delete`, `test_bot_private_memory_not_shared` | pass |
| 13 | Cross-user access denial | `test_cross_user_access_denied` (bots, tasks, messages, events, memory, files, artifacts) | pass |
| 14 | Malicious page instructions (credential disclosure) | `test_prompt_injection_cannot_exfiltrate`: MOCK model *obeys* the injection; metadata-IP fetch, internal port, key-file read blocked; tracker write held for approval; token never in model context | pass |
| 15 | Path traversal, SSRF, unsafe upload rejection | `test_path_traversal_rejected`, `test_ssrf_blocked`, `test_unsafe_uploads_rejected` (symlink case skipped) | pass |
| 16 | Backup restoration into a clean environment | `test_backup_restore_into_clean_environment`: new DB, new data dir, new key path, verified in a separate process | pass |

QA workflow: `test_qa_workflow_reproduce_report_and_ask_before_submit`. Quill opens the fixture issue list,
reproduces the seeded defect (2+2 → 5), captures a PNG screenshot and console log, writes `bug-report.md`, and
stops at `waiting_for_approval`. The tracker has 0 issues until approval, then exactly 1. Pass (MOCK).

Additional coverage: `tests/test_extras.py` (compaction, upload/download, terminal timeout/env scrub, shell approval
gate, local-files connector, skill-from-task + approval gate, routine failure notification, deadline, teams,
templates, web-search-unavailable). All pass.

## Real-model results (not MOCK)

| Run | Model | Wall time | Outcome |
|---|---|---|---|
| UI, "short sales report… deliverable sales-report.md" | qwen2.5:1.5b | 165.5 s (plan + tool call + report on CPU) | `succeeded`; model called `create_artifact` itself; file downloadable |
| `test_real_model_report` | qwen2.5:1.5b | 15.7 s | `succeeded`; deliverable written from the final answer (model did not call the tool) |

**Quality finding (honest):** in the UI run the report said returns were 0 (input: 4 and 9) and sales grew
11.67% (correct: 12.5%), and the markdown table was malformed. The 3B model in the eval falsely claimed it had
fetched an attacker URL that it never called. Small local models are unreliable at arithmetic and at reporting
their own actions accurately. The deliverable and evidence mechanics are sound, but outputs need review. Use a
calculation tool and a stronger model for numeric work.

## Measured resource use (dev host, idle unless noted)

| Component | Working set |
|---|---|
| API (uvicorn) | 111 MB |
| Worker | 98 MB |
| Computer service (python) | 90 MB |
| Playwright Chromium (4 processes, one page open) | 176 MB |
| PostgreSQL (all backends) | 291 MB |
| Ollama with `qwen2.5:1.5b` loaded | 1.37 GB (Ollama `/api/ps`) |
| Ollama with `qwen2.5:3b` loaded | 2.42 GB |
| Disk: `.local` (PostgreSQL binaries + data + app data) | 946 MB |
| Disk: Ollama models (both) | 2.78 GB |

Generation speed on this CPU: ~20–24 tok/s (1.5B), ~10–12 tok/s (3B).

## Defects found and fixed during testing

1. Retry bookkeeping was discarded by a row refresh, so a failing task was re-claimed in a hot loop. Fixed in
   `Runner._end(**fields)`.
2. **Security:** after an approval, a changed pending payload executed under the old approval. Now a changed
   digest resets the call and requires a new approval (`test_changed_payload_invalidates_approval`).
3. Task deadline was not enforced during a long model call. The remaining time is now passed to the provider and
   re-checked after it returns.
4. DST gap mapping shifted nonexistent local times backwards. Now uses PEP 495 `fold=0` (forward).
5. Demonstration conversion crashed when Enter followed an excluded sensitive step.
6. UI: the focused skip link covered the phone menu button; the activity panel squeezed the chat at mid widths.

## Not tested here

Docker Compose deployment, Linux rlimits/network isolation for the terminal, symlink escape, DNS rebinding,
real-Ollama mid-generation cancellation, upgrade across schema revisions, screen-reader accessibility. See
[CAPABILITIES.md](CAPABILITIES.md).
