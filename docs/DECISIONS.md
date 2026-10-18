# Engineering decisions (ADR log)

Newest last. Each entry: context → decision → consequence. Another session should read this,
`docs/PROGRESS.md`, and `docs/CAPABILITIES.md` before changing anything.

## ADR-001 Host inspection (2026-10-18)

Inspected host:

| Item | Finding |
|---|---|
| OS | Windows 11 Pro 10.0.26200 |
| CPU | Intel i7-1265U, 10 cores / 12 threads (15 W laptop part) |
| RAM | 15.6 GB total, ~5.4 GB free at inspection |
| GPU | Intel UHD (integrated, 2 GB shared). No CUDA/ROCm. |
| Disk | 364 GB free on C: |
| Runtimes | Python 3.13.15, Node 24.19.0, npm 11.17.0, git 2.55 |
| Docker / WSL | **Not installed.** WSL not enabled. |
| PostgreSQL | Not installed |
| Ollama | Not installed (installed during this session with user approval, per-user, via winget) |

## ADR-002 Keep the specified architecture; run it natively on this host

Context: no Docker/WSL. Enabling them needs admin rights and a reboot.
Decision: keep FastAPI + SQLAlchemy + Alembic + PostgreSQL + Playwright + Ollama + React/Vite.
Development and acceptance tests run **natively** on Windows:
- PostgreSQL 17.6 official EDB portable binaries in `.local/pgsql`, data in `.local/pgdata`,
  listening on `127.0.0.1:54329` only (`infra/scripts/setup_postgres.py`, started by `infra/scripts/run_local.py`).
- Ollama on the host (`127.0.0.1:11434`).
- Docker Compose files are provided in `infra/` for Linux hosts but are **not verified on this machine**
  (capability matrix says so).
Consequence: OS-level sandboxing (rlimits, network namespaces) for the terminal is only effective in the
Linux container. On native Windows the terminal tool still enforces argv-only execution, timeouts,
process-tree kill, scrubbed env and workspace cwd, but **not** memory/network limits.

## ADR-003 One Python package, three processes

`services/api/localcrew` is the shared package (models, policy, engine, tools).
- `services/api` → FastAPI HTTP API (`localcrew.api`).
- `services/worker` → durable task/routine worker (`python services/worker/worker.py`).
- `services/computer` → browser + terminal execution service. It never receives DB credentials;
  the worker calls it over HTTP with an internal token. This is the trust boundary between
  model-driven execution and the control plane.
Rationale: avoid duplicating models in three packages while keeping process isolation.

## ADR-004 Queue = PostgreSQL rows

Tasks are claimed with `SELECT … FOR UPDATE SKIP LOCKED`, leased (`lease_owner`, `lease_expires_at`),
heart-beaten every few seconds, and re-claimed after lease expiry (crash recovery). No Redis.
Event stream = `task_events` table (bigserial id) replayed over SSE with `Last-Event-ID`.
`ponytail:` SSE polls the table every 0.5 s; switch to LISTEN/NOTIFY if many clients connect.

## ADR-005 Models chosen by measurement on this CPU-only host

See `docs/MODELS.md`. Only models actually pulled and evaluated are listed as tested.

## ADR-006 Deterministic "scripted" model for correctness tests

Engine correctness (recovery, approvals, injection resistance) is tested with a scripted provider that
replays predefined tool calls. It is **only** selectable when `LOCALCREW_ALLOW_SCRIPTED_MODEL=1`
and is labelled `MOCK` in test names and reports. Real-model quality is evaluated separately
(`tests/eval`).

## ADR-007 Policy outside the model

Every tool declares an effect class (`read`, `workspace_write`, `external_write`, `destructive`, `shell`,
`browser`). `localcrew.policy.decide()` evaluates org → user → bot rules; deny wins; bot-level rules can
only tighten; org `require_approval` cannot be relaxed by user rules. Approvals are bound to a SHA-256
digest of the canonical action (tool, target, payload, acting bot, user) and consumed atomically once.

## ADR-008 Browser profile access is serialized

One persistent Chromium context per user profile directory, owned by the computer service process.
A control lease (`execution_leases` row `browser:<user>`) decides who may act: a task or a human.
Human takeover revokes the automation lease; automation calls then fail with `control_revoked` and the
task moves to `waiting_for_user`. No second process ever opens the same profile directory.

## ADR-009 No vector DB, no embeddings yet

Memory retrieval uses PostgreSQL full-text search. Embeddings are in the backlog until a measured
retrieval improvement justifies them.

## ADR-010 Visual computer control disabled

No tested vision model fits this host. Bots use DOM/semantic browser tools only. Screenshots are shown
to humans; they are never described to a non-vision model as "understood".

## ADR-011 Default model = qwen2.5:1.5b (measured)

Context: CPU-only host. Eval (docs/eval/model_eval_results.json): 1.5b and 3b both 5/6. 1.5b is about 2x faster
(~20 vs ~11 tok/s), uses 1.4 vs 2.4 GB, and is Apache-2.0; 3b is under the non-commercial Qwen Research License.
Decision: default `qwen2.5:1.5b`; 3b optional per bot. Consequence: weak arithmetic and formatting (see TEST_REPORT);
numeric work should go through tools.

## ADR-012 Live view by screenshot polling

The computer service returns JPEG screenshots that the UI polls every 1.5 s, and human input goes through REST
(click at image coordinates, type, keys). This avoids a WebSocket/CDP screencast stack for v1. Upgrade path:
CDP `Page.startScreencast` over WebSocket (BACKLOG #13).

## ADR-013 Deterministic schedule parsing

Natural-language schedules map to cron through a small deterministic parser (`routines.parse_schedule`). The user
must confirm the preview (`confirmed=true`). Unknown phrases are rejected, not guessed by a model.
