# Capability matrix

Status as of 2026-10-18 on the development host (Windows 11, i7-1265U, 15.6 GB RAM, no GPU, no Docker).
Statuses:

- **Tested**: implemented and covered by an automated test that passed on this host (test named).
- **Unverified**: implemented but not exercised by an automated test, or only verified on a different platform than
  the one it is meant for.
- **Config**: works only after you configure something (listed).
- **Unavailable**: not implemented. Do not rely on it.

"MOCK" means the test drove the engine with the deterministic scripted model (`scripted:*`). That proves the
engine, policy and tool mechanics, not model quality. Real-model results are in `docs/TEST_REPORT.md`.

## Core platform

| Capability | Status | Evidence / notes |
|---|---|---|
| First-run setup, login, logout, DB-backed sessions (HttpOnly, SameSite=Strict) | Tested | `test_csrf_and_auth_required`, E2E login |
| CSRF header check, Origin check on mutations | Tested | `test_csrf_and_auth_required` |
| Login rate limit (10 failures / 5 min / IP) | Unverified | in-memory, per API process |
| Bind to localhost by default | Tested | launcher + compose bind `127.0.0.1` |
| TLS / remote access | Config | needs a reverse proxy and `LOCALCREW_SECURE_COOKIES=true` (docs/OPERATIONS.md) |
| Bot create/edit/version/pin/hide/duplicate/delete with impact report | Tested | `test_bot_crud_versions_duplicate_delete` |
| Bot persistence across API process restart | Tested | `test_bot_persists_across_api_restart` |
| Duplicate copies config only (no memory, history, credentials) | Tested | same |
| Hiding a bot does not stop its routines | Unverified | routines never read `hidden` (by design); no dedicated test |
| Direct and group conversations, @mentions → tasks | Tested | `test_handoff_creates_child_with_evidence` |
| Ownership checks / cross-user denial (404, no existence leak) | Tested | `test_cross_user_access_denied` |
| UTC storage, display in user's IANA zone (default Asia/Karachi) | Tested | `test_dst_and_timezones`; UI uses `Intl` with user zone |

## Durable task engine

| Capability | Status | Evidence / notes |
|---|---|---|
| Lifecycle queued→planning→running→waiting_*/retry_wait→terminal, transactional | Tested | all engine tests |
| Separate plan / execute / validate / report phases with steps | Tested | `test_task_creates_downloadable_artifact` |
| Transactional claims (`FOR UPDATE SKIP LOCKED`), leases, heartbeats | Tested | `test_worker_crash_recovers_from_checkpoint` |
| Recovery after hard worker kill, resume from checkpoint without re-running steps | Tested | same (real subprocess killed) |
| Bounded retries with exponential backoff (model errors) | Tested | `test_model_error_retries_with_backoff` |
| Task deadline (also interrupts a long model call) | Tested | `test_task_deadline_enforced` |
| Tool-call budget; handoff depth/dedup/budget limits | Tested | `test_handoff_loop_prevention` |
| Per-user and model concurrency limits | Unverified | per-user limit 2 running tasks; model semaphore = 1 |
| Cancellation into terminal subprocess trees and browser navigation | Tested | `test_cancel_running_terminal_command`, `test_cancel_active_browser_navigation` |
| Cancellation of an in-flight Ollama generation | Unverified | stops at next streamed token; long prompt evaluation is not interruptible |
| User redirect of running work | Tested | `test_redirect_running_task` |
| Persisted SSE event stream with replay (`Last-Event-ID`, `after=`) | Tested | `test_sse_replay`; live stream drives the E2E chat test |
| Tool argument schema validation before execution | Tested | `test_invalid_tool_args_rejected_before_execution` |
| Idempotency keys + reconciliation of ambiguous external writes (no blind retry) | Tested | `test_ambiguous_write_reconciled_no_duplicate`, `…after_crash_never_blindly_retried` |
| Interrupted shell command → ask the user (never auto re-run) | Unverified | code path in `engine.handle_call` |
| Context bounding (deterministic compaction keeping objective + user constraints) | Tested | `test_context_compaction_keeps_objective_and_user_constraints` |

## Models

| Capability | Status | Evidence / notes |
|---|---|---|
| Ollama provider: streaming, tool calling, JSON-schema structured output, usage, capability detection | Tested | `tests/eval/model_eval.py`, `test_real_model_report` |
| Real local-model task producing a downloadable report | Tested | `test_real_model_report` (qwen2.5:1.5b); content quality is weak, see TEST_REPORT |
| Unsupported capability detection (no tools → answer without tools; vision flag) | Unverified | both installed models report `tools`; no non-tool model installed |
| Vision / screenshot understanding | Unavailable | no vision model tested; screenshots are never described to the model |
| Optional remote providers | Unavailable | not implemented; nothing leaves the machine |
| Embeddings / vector retrieval | Unavailable | keyword FTS only (ADR-009) |

## Browser and computer

| Capability | Status | Evidence / notes |
|---|---|---|
| Dedicated Chromium (Playwright) in a separate service; one persistent profile per user | Tested | `test_login_persistence_expiry_and_takeover` |
| Sign-in persistence across computer-service restart | Tested | same |
| Navigation, page text + interactive elements, semantic click/type, wait, screenshot | Tested | browser tests, QA workflow |
| Upload and download via workspace | Tested | `test_browser_download_and_upload` |
| Console-log capture as evidence | Tested | `test_qa_workflow_reproduce_report_and_ask_before_submit` |
| Session-expired detection; password / MFA / CAPTCHA pause | Tested | `test_login_persistence…`, `test_bot_refuses_password_fields_and_captcha` |
| Bots refuse to type into password / one-time-code fields | Unverified | enforced in `computer.b_type`; the test's page had no password field to exercise it |
| Live view (screenshot polling ~1.5 s) and human takeover (click/type/keys/goto) | Tested | takeover tests via API; UI manually checked |
| Takeover revokes the automation lease mid-task; resume only after release | Tested | `test_takeover_revokes_automation_mid_task` |
| Sensitive human input excluded from transcripts, events, audit, recordings | Tested | DB scan in `test_login_persistence…`; `test_demonstration_recording_excludes_password` |
| Serialized shared-profile access (lease + lock; never two processes on one profile) | Tested | `test_concurrent_browser_access_serialized` |
| SSRF guard for navigation and fetch (private/link-local/metadata blocked) | Tested | `test_ssrf_blocked`, injection test |
| SSRF guard on browser sub-resources / redirects (route interception) | Unverified | implemented in `computer.browser_for`; DNS-rebinding not handled |
| WebSocket live streaming | Unavailable | screenshot polling instead (adequate for takeover; not video-smooth) |
| Visual (coordinate) computer control by bots | Unavailable | requires a tested vision model (ADR-010) |
| Multi-user hostile-code isolation | Unavailable | single-user deployment only; needs per-user VMs (docs/ARCHITECTURE.md) |

## Terminal and files

| Capability | Status | Evidence / notes |
|---|---|---|
| argv-only `run_command`; separately gated `run_shell` (off by default) | Tested | `test_terminal_timeout_env_scrub_and_cwd`, `test_shell_requires_approval_by_default` |
| Timeouts, process-tree kill, scrubbed environment, workspace cwd | Tested | same + cancellation test |
| CPU/memory/file-size/process rlimits | Unverified | Linux container only (`preexec_fn`); not available on native Windows |
| Network control for terminal | Config | Docker `exec` network is internal; native runs have normal network access |
| Workspace list/read/write/move/search/soft-delete; versioned overwrites | Tested | file API tests, crash test |
| Path traversal rejection | Tested | `test_path_traversal_rejected` |
| Symlink escape rejection | Unverified | test skipped: Windows here cannot create symlinks without developer mode |
| Upload validation (type, magic bytes, size, archives/executables, encrypted/JS PDFs) | Tested | `test_unsafe_uploads_rejected` |
| Downloadable artifacts with provenance, versions, owner-only access, `attachment` + sandbox CSP | Tested | `test_task_creates_downloadable_artifact` |
| Office formats (docx/xlsx/pptx) | Unavailable | backlog |

## Connectors, policy, secrets

| Capability | Status | Evidence / notes |
|---|---|---|
| Connector interface (capabilities, schemas, auth, scopes, rate limit, read/write class, health, revoke) | Tested | `test_secrets_encrypted_at_rest_and_redacted` |
| Local-files connector | Tested | `test_local_files_connector` |
| Local fixture issue-tracker connector (idempotency + reconcile) | Tested | approval + ambiguous-write tests |
| MCP adapter, Slack, X | Unavailable | see docs/BACKLOG.md |
| Policy outside the model: allow / deny / require approval; deny wins; org not relaxable; bot rules only tighten | Tested | `test_persistent_rule_is_narrow_and_org_deny_wins` |
| Approval cards (target, identity, payload, effect, expiry, scope) bound to action digest | Tested | `test_approval_card_and_allow_once` |
| Denial, expiry, replay rejection, changed-payload rejection | Tested | `test_approval_denied/expired`, `…_card_and_allow_once`, `test_changed_payload_invalidates_approval` |
| "Always allow this exact target" narrow persistent rule | Tested | `test_persistent_rule_is_narrow…` |
| Secrets encrypted (Fernet) with key file outside DB; key rotation; broker injection | Tested | `test_secrets_encrypted…`, `test_key_rotation_keeps_secrets_readable` |
| Redaction of known secret values and token patterns in tool output, events, approvals | Tested | injection + secrets tests |
| Prompt-injection containment (untrusted-content wrapping + enforcement outside model) | Tested | `test_prompt_injection_cannot_exfiltrate` (MOCK model that obeys the injection) |
| Stored-XSS safety in UI (text-only rendering) | Tested | E2E `rejects stored XSS` |

## Memory, collaboration, skills, routines

| Capability | Status | Evidence / notes |
|---|---|---|
| Memory with scope, source, timestamps, confidence, expiry, revisions; edit/disable/delete | Tested | `test_memory_correct_disable_delete` |
| Bot-private vs shared memories; private notes never injected | Tested | `test_bot_private_memory_not_shared` |
| Credentials refused as memory | Tested | same |
| Keyword (PostgreSQL FTS) retrieval injected with memory ids | Tested | memory tests |
| Answers cite actual sources | Unverified | tool evidence is recorded; whether the model cites it depends on the model |
| Handoffs (objective, owner, refs, deliverable, criteria, budget, deadline, permissions) persisted + timeline | Tested | `test_handoff_creates_child_with_evidence` |
| Loop prevention (depth, budget, dedup, accountable parent) | Tested | `test_handoff_loop_prevention` |
| Parallel bot work within limits | Unverified | 2 worker threads, 1 model slot |
| Versioned skills; draft → approve gate; skill from completed task | Tested | `test_skill_from_task_and_approval_gate` |
| Opt-in demonstration recording → parameterized draft skill (semantic locators) | Tested | `test_demonstration_recording_excludes_password` |
| Replaying a recorded skill reliably | Unverified | one recording is not proof of generalization; requires review + validation run |
| NL schedule → cron with explicit confirmation, IANA tz, DST, next-run preview | Tested | `test_dst_and_timezones`, `test_scheduled_routine_runs_with_frontend_closed` |
| Enable/pause/edit/delete/run-now/history, missed-run policies, overlap prevention | Tested | `test_missed_runs_policy`, `test_overlap_prevented_and_pause` |
| Routines run with the frontend and API closed | Tested | `test_scheduled_routine_runs_with_frontend_closed` (only worker running) |
| Authenticated webhooks (HMAC + timestamp) with delivery dedup | Tested | `test_webhook_auth_and_dedup` |
| Routine failure notification | Tested | `test_routine_failure_notification` |
| Operation while host sleeps / is shut down | Unavailable | local execution stops; catch-up per missed-run policy on next start |

## Extensions

| Capability | Status | Evidence / notes |
|---|---|---|
| Team roles (owner/manager/member), shared bot config, private per-person conversations | Tested | `test_team_shared_bot_private_conversations` |
| Sanitized bot template export/import | Tested | `test_template_export_import_is_sanitized` |
| Per-person OAuth identities / team service credentials | Unavailable | no OAuth connectors exist yet |
| Web search adapter (SearXNG) | Config | set `LOCALCREW_SEARXNG_URL`; without it the tool reports "unavailable" (tested) |
| Voice (dictation, TTS, live voice) | Unavailable | backlog; no latency claims |
| Image / video generation | Unavailable | optional, not planned for this host |

## Deployment and operations

| Capability | Status | Evidence / notes |
|---|---|---|
| Native launcher (`infra/scripts/run_local.py`), portable PostgreSQL setup | Tested | used throughout this session |
| Docker Compose with health checks, unprivileged containers, no Docker socket / home mounts | Unverified | written for Linux; Docker unavailable on the dev host |
| Alembic migrations | Tested | every test run migrates a fresh schema |
| Backup / restore into a clean DB + data dir + key file; checksum verification | Tested | `test_backup_restore_into_clean_environment` |
| Upgrade path (restore then `alembic upgrade head`) | Unverified | only one schema revision exists |
| Responsive UI (desktop + phone), keyboard skip link, no horizontal overflow | Tested | Playwright E2E `desktop` + `phone` projects |
| Full accessibility audit (screen readers, contrast) | Unverified | native elements + labels used; no audit run |
