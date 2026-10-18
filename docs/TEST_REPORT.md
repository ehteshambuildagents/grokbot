# Acceptance test report

Latest: 2026-10-19, verification pass 2 (before first publication). Host: Windows 11 Pro, Intel i7-1265U, 15.6 GB
RAM, no GPU, no Docker/WSL. Python 3.13.15, Node 24.19, PostgreSQL 17.6 (portable), Ollama 0.40.5, Playwright 1.64.

## Summary (final code)

| Check | Command | Result |
|---|---|---|
| Python lint | `ruff check .` | **clean** |
| Backend acceptance (MOCK model where a model is needed) | `pytest --deselect tests/test_realmodel.py` | **87 passed, 1 skipped** in 187 s ([log](test-runs/2026-10-19-pytest-final.txt)) |
| Web lint, typecheck, production build | `npm run lint && npm run build` | **clean** |
| Playwright E2E, desktop + phone | `npx playwright test` | **6 passed** ([log](test-runs/2026-10-19-web-e2e.txt)) |
| Real local-model tests (`-m realmodel`, `qwen2.5:3b`) | `pytest tests/test_realmodel.py` | **2 failed** (see below; [log](test-runs/2026-10-19-pytest-with-realmodel.txt)) |
| Hosted model API (OpenAI-compatible) | `tests/test_remote_provider.py` | **7 passed against a MOCK API server only**; no real provider tested (no key) |
| Linux / Docker | GitHub Actions `ci.yml` | see "CI" below |

Skipped: `test_symlink_escape_rejected`. This Windows account cannot create symlinks. The equivalent junction and
hard-link tests pass here, and the symlink test runs on Linux CI.

No test retries are configured (`retries: 0`; no rerun plugin). Nothing was skipped or weakened to get a pass.
Failures are listed as failures.

## Real-model results (not mocked)

### Final regression with real models: **failed**
Run on the code just before the API-provider addition, under heavy host load (the full run took 2 h 31 min instead
of ~10 min):
- `test_real_ollama_cancellation_stops_generation` (`qwen2.5:3b`) **failed**: cancellation completed after
  6,447 s instead of < 5 s. The same mechanism cancelled `qwen2.5:1.5b` in ~1.5 s, during both prompt evaluation
  and generation, in direct checks earlier the same day. The root cause with 3b under load is not yet established.
- `test_real_model_report_is_correct_or_rejected` (`qwen2.5:3b`) **failed**: the task exceeded its 30-minute
  deadline (status `failed`, "task deadline exceeded"). No wrong report was accepted.

These block "it works well with local models on this laptop". They are BACKLOG item 0.

### Report trials (`tests/eval/report_trials.py`, sales report with a required percentage)
Each round ran on a then-current validator; each round's failures drove a fix. The **final validator was not
re-trialed** (the official last round was stopped when the validator changed again). Raw data:
`docs/eval/history/*.json`.

| Round (validator state) | qwen2.5:3b succeeded | qwen2.5:1.5b succeeded |
|---|---|---|
| 2 – before token cap | 4/5 | 2/5 |
| 3 – before evidence fix | 5/5 | 1/5 |
| 4 – before narrow recomputation | 4/5 | 1/5 |
| 5 – before fenced-Markdown extraction | 3/5 | 1/5 |
| 6 – before "calculate results are not evidence" | 2/5 | 3/5 |

**Offline re-validation:** I re-checked all 50 stored real-model reports from every round against the final validator.
It accepts 11. All 11 state only correct figures (12.5% sales growth, 125% returns growth). One of them
**mislabels** a correct figure ("October to November: 125%" for the returns change). Deterministic checks verify
figures, not their labels. Every wrong report found in the trials is rejected by the final validator: 11.67%,
−20%, 15%, 25%, +50%, 120,000, a Chinese-language report, non-answers, assistant chatter, malformed tables. Each has
a regression test (`tests/test_hardening.py`, `tests/test_deliverable_regressions.py`).

Honest conclusion: on this CPU-only laptop, small local models produce a validated, correct report only some of the
time. Validation prevents wrong numbers from being reported as success, but it does not make the model better.
Use a stronger model (GPU host or a hosted API) for dependable deliverables.

## The 16 required scenarios

| # | Scenario | Test(s) | Result |
|---|---|---|---|
| 1 | Bot creation and persistence after restart | `test_bot_crud_versions_duplicate_delete`, `test_bot_persists_across_api_restart` | pass |
| 2 | Real local-model task → downloadable report | `test_real_model_report_is_correct_or_rejected`, report trials | **fail in final regression** (deadline); succeeded in earlier trials |
| 3 | Browser login persistence (fixture account) | `test_login_persistence_expiry_and_takeover` | pass |
| 4 | Session expiry and human takeover | same + `test_takeover_revokes_automation_mid_task` | pass |
| 5 | Bot handoff with evidence | `test_handoff_creates_child_with_evidence`, `test_handoff_loop_prevention` | pass (MOCK) |
| 6 | Scheduled run with frontend closed | `test_scheduled_routine_runs_with_frontend_closed` | pass |
| 7 | Worker failure and checkpoint recovery | `test_worker_crash_recovers_from_checkpoint` (hard kill) | pass |
| 8 | Ambiguous external write, no duplicates | `test_ambiguous_write_*` | pass |
| 9 | Cancellation of browser and terminal work | `test_cancel_active_browser_navigation`, `test_cancel_running_terminal_command` | pass (Ollama cancellation: see above) |
| 10 | Approval denial, expiry, replay, changed payload | `test_approval_*`, `test_changed_payload_invalidates_approval` | pass |
| 11 | Concurrent shared-browser access | `test_concurrent_browser_access_serialized` | pass |
| 12 | Memory correction and deletion | `test_memory_correct_disable_delete` | pass |
| 13 | Cross-user access denial | `test_cross_user_access_denied` | pass |
| 14 | Malicious page instructions | `test_prompt_injection_cannot_exfiltrate` | pass (MOCK model obeys the injection; enforcement blocks it) |
| 15 | Path traversal, SSRF, unsafe uploads | traversal/SSRF/upload tests + junction, hard-link, DNS-rebinding tests | pass (symlink case: Linux CI) |
| 16 | Backup restore into a clean environment | `test_backup_restore_into_clean_environment` | pass |

QA workflow (`test_qa_workflow_reproduce_report_and_ask_before_submit`): pass (MOCK).

## Defects found and fixed in this pass (each has a regression test)

1. **Junction escape**: workspace search returned the contents of a file outside the workspace.
2. **DNS rebinding**: check-then-connect was exploitable. Now pinned resolution for server-side HTTP, plus a pinning
   egress proxy for all Chromium traffic.
3. **Ollama cancellation during prompt evaluation** took 60 s (no response headers until the first token). Now
   socket shutdown from a watchdog. See the open 3b failure above.
4. **Terminal**: large output could deadlock until timeout, and Windows had no memory or process limits. Now Job
   Objects, continuous draining and output caps.
5. **Wrong reports could succeed** (a file existing was enough). Now: a `calculate` tool; deterministic structure,
   content and numeric-grounding validation; one repair turn; failure otherwise. Real trials then exposed six more
   false acceptances, all fixed: clock time as evidence, 4 × 12.5 recomputation, a wrong formula via `calculate`,
   "15%" vs the difference, commentary and foreign-language output, URLs counted as figures.
6. A repair turn ignored the corrected answer when the deliverable already existed.
7. SSE endpoints held a DB connection "idle in transaction" for the whole stream.
8. A misconfigured model provider crashed the worker thread instead of failing the task.
9. Hard-coded default internal token; ESLint react-hooks errors; ruff findings.

## Added in this pass

Optional **hosted model API** (any OpenAI-compatible provider): opt-in via `.env`, data-egress disclosure in
Settings, key never stored in the DB or sent to the model, retries on 429/5xx, no retry on 4xx, cancellation before
the first byte. **Tested only against a local MOCK server.**

## CI

`.github/workflows/ci.yml` runs on GitHub-hosted Ubuntu 24.04: ruff, the full pytest suite with PostgreSQL 17
(real-model tests auto-skip without Ollama), web lint/build, Playwright E2E, and a Docker Compose build + smoke test
(`infra/scripts/docker_smoke.sh`). Its first results appear on the repository's Actions tab after publication. They
were not available when this report was written.

## Measured resource use (dev host)

API ~111 MB, worker ~98 MB, computer service ~90 MB, Playwright Chromium ~176 MB, PostgreSQL ~291 MB. Ollama with
`qwen2.5:1.5b` loaded: 1.37 GB; with `qwen2.5:3b`: 2.42 GB. Generation: ~20–24 tok/s (1.5B) and ~10–12 tok/s (3B)
when the host is not loaded.
