# Architecture and trust boundaries

```
 Browser / phone ──HTTPS(proxy)/HTTP localhost──▶  API (FastAPI)  ──SQL──▶  PostgreSQL (source of truth)
   React SPA (apps/web)                         │  auth, CSRF, ownership checks, SSE replay
                                                │  approvals, policy rules, files, memory, routines
                                                ▼
                                           Worker (python)  ──HTTP──▶  Ollama (host, 127.0.0.1:11434)
                                           claims tasks (SKIP LOCKED), leases + heartbeats,
                                           plan → run → validate → report, routines scheduler
                                                │ HTTP + X-Internal-Token
                                                ▼
                                       Computer service (python + Playwright Chromium)
                                       one persistent profile per user, control lease,
                                       human takeover, terminal (argv, timeouts, kill tree)
                                                │
                                                ▼
                                   websites (SSRF-guarded) / fixture app (tests)
```

Processes: `services/api` (HTTP), `services/worker/worker.py`, `services/computer/computer.py`. They share the Python
package `services/api/localcrew` (ADR-003). The web client is optional: tasks and routines live in PostgreSQL
and the worker. **Nothing runs while the host is asleep or powered off.** When it comes back, expired leases are
recovered and missed routine runs follow each routine's missed-run policy.

## Data model (services/api/localcrew/models.py, migration `migrations/versions/*_initial_schema.py`)

Users, AuthSessions, Teams, Memberships, Bots, BotVersions, Conversations, Participants, Messages, Tasks,
TaskSteps, TaskEvents, ToolCalls, Artifacts, Memories (generated `tsvector` + GIN), Skills, SkillVersions,
Routines, RoutineRuns, Connections, SecretReferences, PolicyRules, ApprovalRequests, BrowserSessions,
ExecutionLeases (browser control + worker liveness), AuditEvents.

Ownership: every protected row has `owner_user_id`; shareable rows also have `team_id`. API handlers load objects
through `common.owned()` / `common.bot_access()`, which return 404 for other users' objects. Scopes:

| Scope | Who sees it |
|---|---|
| personal (conversations, tasks, artifacts, approvals, connections, private notes) | the owner only |
| bot memory | the owner, and only that bot's prompts |
| user memory / workspace / browser profile | the owner and all of the owner's bots |
| team bot configuration, team memory | team members by role; conversations with team bots stay per-person |

The shared execution filesystem is **not** a privacy boundary between people. Each user has a separate workspace
directory, and every API access checks ownership first. Real isolation between people needs separate
execution environments (see "Multi-user isolation").

## Task engine (services/api/localcrew/engine.py)

- **Claim**: `SELECT … FOR UPDATE SKIP LOCKED` over `queued`/due `retry_wait` tasks, plus `planning`/`running`
  tasks whose lease expired (crash recovery: `attempts` increments, `recovered` event). Per-user cap: 2 running.
- **Heartbeat** thread extends the lease every `heartbeat_seconds`. It sees `cancel_requested` and tells the
  computer service to kill that task's processes and abort its page. If the lease was lost, it stops the run.
- **Every write** goes through `Runner.save()`, which re-checks lease ownership under a row lock. A zombie worker
  cannot overwrite a task that someone else recovered.
- **Tool calls**: model output → schema validation → policy decision → (approval) → `ToolCall` row committed as
  `running` with an idempotency key → side effect → result, next message and checkpoint committed together.
  On recovery, local/idempotent tools re-run. External writes are reconciled through the connector (lookup by
  idempotency key). Interrupted shell commands become `ambiguous` and wait for the user.
- **Validation** is deterministic: non-empty answer, required deliverable exists (written from the final answer if
  the model did not create it).
- **Events** are rows in `task_events`; SSE replays from any id.

## Trust boundaries

1. **Browser ↔ API**: cookie session (HttpOnly, SameSite=Strict, Secure behind TLS), CSRF token header on
   every mutation, Origin check, strict CSP, `nosniff`, `X-Frame-Options: DENY`. UI renders user, bot and tool text
   as text nodes only. Only `[name](/api/...)` links become anchors.
2. **API/worker ↔ model**: the model gets instructions, conversation, memories and tool results. It never gets
   secrets: connectors fetch credentials through `security.reveal_secret` inside the call. Tool output from pages,
   files and connectors is wrapped in `<untrusted_tool_output>` and redacted.
3. **Model ↔ effects**: `policy.decide()` in code decides every effectful tool call. A model, a page or a skill
   cannot grant itself permission. Approvals bind to a SHA-256 digest of (tool, target, payload, bot, user) and
   are consumed atomically once.
4. **Worker ↔ computer service**: shared internal token; the computer service has no DB credentials or key file.
   In Compose it sits on an internal network plus an egress network. It never mounts the Docker socket or the
   host home directory.
5. **Computer service ↔ web**: SSRF guard on navigation and all sub-requests (private, loopback, link-local and
   metadata ranges blocked, except an explicit fixture allowlist). Password/MFA/CAPTCHA pages pause automation.
6. **Secrets at rest**: Fernet ciphertext in PostgreSQL. Keys live in a separate file (`LOCALCREW_SECRET_KEY_FILE`),
   rotatable with `ops.py rotate-keys`. Back up the key file separately.

Known gaps (also in CAPABILITIES/BACKLOG): DNS-rebinding TOCTOU in the SSRF guard, no per-process network policy
for native terminals, an in-memory login rate limit, and a single shared internal token.

## Browser concurrency (ADR-008)

Exactly one Chromium persistent context per user profile directory, owned by the computer service process. An
in-process lock serializes operations. A control lease (task or human) decides who may act. Another task gets
`423 busy` until the lease is released (at task end) or expires (120 s). Human takeover waits for the in-flight
step, then revokes automation. Release re-queues tasks blocked on the browser.

## Multi-user isolation

The deployment is **single-user (or trusted team)**. A container is not enough isolation for hostile code from
multiple people. Before enabling untrusted multi-user execution you need:

- one computer service per person, each in its own VM or microVM (Firecracker / Kata / gVisor at minimum), with
  separate storage and network policy;
- per-user internal tokens or mTLS between worker and computer service;
- egress proxy with allowlists and DNS pinning;
- quotas on CPU, memory, disk and process counts enforced by the VM.
