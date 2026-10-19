# Engineering walkthrough

This document explains how LocalCrew works, why it is built this way, what has been measured, and where it breaks.
It is the starting point for anyone who wants to change core behaviour or review the design.

## 1. The problem

An "AI teammate" must do work that outlives a chat request (minutes to hours), touch the outside world (browser,
terminal, APIs, MCP servers), and stay trustworthy even though the model driving it can be wrong or manipulated.
Three properties follow, and most of the code exists to provide them:

1. **Durability**: work survives closed tabs, crashed workers and restarts, without repeating side effects.
2. **Containment**: the model proposes actions; deterministic code decides whether they happen.
3. **Honest outcomes**: a task succeeds only when its output passes checks, and failures are reported as failures.

## 2. One request, end to end

| Step | What happens | Code |
|---|---|---|
| 1 | User posts a message; one `Task` row per addressed bot is inserted (`queued`) | `api.post_message` |
| 2 | A worker claims it with `SELECT … FOR UPDATE SKIP LOCKED` and takes a lease | `engine.claim` |
| 3 | A heartbeat thread extends the lease and watches for cancellation | `engine.Heartbeat` |
| 4 | **Plan**: one JSON-mode model call produces a short plan (stored, shown in the UI) | `Runner.plan` |
| 5 | **Execute**: tool loop. The model proposes calls → schema validation → `policy.decide()` → optional approval → `ToolCall` row committed **before** the side effect → result committed with the next checkpoint | `Runner.execute`, `Runner.handle_call` |
| 6 | **Validate**: deterministic checks on the answer and deliverables; one repair turn, otherwise `failed` | `Runner.validate`, `validation.py` |
| 7 | **Report**: message with artifact links and tool evidence; status `succeeded` | `Runner.report` |
| 8 | Every transition is a `task_events` row; the UI streams them over SSE and replays after reconnects | `api._sse` |

The browser, terminal and MCP servers run in a separate process (`services/computer`) that has no database
credentials. The worker talks to it over HTTP with an internal token.

## 3. Durability model

- **Queue in PostgreSQL, not Redis/Celery.** The task row *is* the job. Claiming with `SKIP LOCKED` gives
  transactional hand-off without a second datastore, and every state change is in the same transaction as the data
  it describes. Throughput needs (a few tasks per user) are far below where this pattern strains.
- **Leases + heartbeats.** A crashed worker stops heart-beating; after `lease_seconds` another worker re-claims the
  task (`attempts` increments, a `recovered` event is emitted). Every write re-checks lease ownership under a row
  lock, so a slow "zombie" worker cannot overwrite a task someone else recovered.
- **Checkpoints.** The conversation with the model, the pending tool calls and the sequence number are stored on the
  task after each step. Recovery resumes from the last checkpoint; completed steps are never re-run
  (`test_worker_crash_recovers_from_checkpoint` kills a worker process to prove it).
- **At-least-once execution, made safe.** Exactly-once across external systems is not achievable, so each tool call
  gets an idempotency key and is committed as `running` before it executes. After a crash, local idempotent tools
  re-run. External writes are **reconciled** (the connector looks the key up) and never blindly retried. Interrupted
  shell commands become `ambiguous` and wait for a human.

## 4. Containment (security model)

- **Policy outside the model.** Each tool has an effect class (`read`, `workspace_write`, `browser`, `shell`,
  `external_write`, `destructive`). `policy.decide()` combines org, user and bot rules with the bot's profile: deny
  wins, org rules cannot be relaxed, and bot rules can only tighten. Prompt injection can make the model *ask* for
  anything; it cannot make the code *allow* it (`test_prompt_injection_cannot_exfiltrate` uses a mock model that
  obeys the injected instructions).
- **Approvals bound to a digest** of tool + target + payload + bot + user, consumed atomically once. Changing the
  payload after approval produces a new digest and needs a new approval, and a consumed approval cannot be replayed.
- **Secrets broker.** Credentials are Fernet-encrypted with a key file outside the database. They are revealed only
  inside connector/MCP calls and registered with the redactor, so they never appear in prompts, events or results.
- **Egress.** Only globally routable addresses are reachable (private, loopback, link-local, metadata and CGNAT are
  blocked). DNS is resolved once and the connection goes to that exact address (pinned httpx backend; a pinning proxy
  for all Chromium traffic), which closes the DNS-rebinding window.
- **Execution limits.** Terminal commands and stdio MCP servers run under Windows Job Objects or POSIX rlimits, with
  a scrubbed environment and process-tree kill on cancel. This bounds resources; it is **not** filesystem or network
  isolation on a native install (see section 10).

## 5. Model layer

- `llm.provider_for()` routes by model name: Ollama (default), `remote:<id>` (any OpenAI-compatible API, opt-in), and
  `scripted:<key>` (deterministic MOCK for tests, disabled in production).
- Messages are stored in one internal format; `remote.to_openai()` converts them, adds tool-call IDs, and repairs
  orphaned tool results left by context compaction.
- **Cancellation** is socket-level: a watchdog thread shuts the connection down when a task is cancelled or its
  deadline passes. This was necessary because Ollama sends no bytes until the first token, so a "check between
  tokens" design could not cancel a long prompt evaluation.
- **Context bounding** is deterministic: the objective, user instructions and recent turns are kept verbatim, and older
  tool output collapses to short references.

## 6. Output validation: why it exists and what it can't do

Small local models produced confident, wrong reports (11.67% instead of 12.5%, invented "120,000", a −20% "decrease").
An LLM judge would share the same failure modes, so validation is deterministic (`validation.py`):

1. **Structure**: Markdown tables, code fences, CSV columns, JSON parsing.
2. **Content**: no clarification requests or non-answers, uses the figures the user supplied, no assistant
   commentary, same script as the request.
3. **Numeric grounding**: every number must appear in the evidence (the request, data returned by tools) or be
   recomputable in one step from the user's figures (difference, sum, percent change, share, total, mean). A figure
   written as a percentage must recompute as a percentage.

Real-model trials found six ways this was too lenient, each now a regression test. Two are worth remembering:
- **`calculate` results are not evidence.** A calculator proves the arithmetic, not that the formula was right. A
  model computed the wrong expression and reported −20%.
- **The current time in the system prompt grounded any number from 0 to 59**, until dates and times were excluded.

**Limits.** Validation verifies figures, not labels (a correct 125% can still be attached to the wrong metric), and
it does not check completeness. It reduces wrong successes; it does not make a weak model good.

## 7. MCP integration

MCP servers are just another kind of connection, so they inherit everything above. The hub lives in the computer
service (execution boundary), keeps one session per server in a dedicated task (anyio requires a transport to be
closed by the task that opened it), and restarts the session when its configuration changes. Tool annotations
decide the effect class conservatively: only an explicit `readOnlyHint` makes a tool free to run, and anything
unannotated is treated as an external write. Arguments are validated against the server's own JSON Schema before
anything is sent. Details: `docs/ARCHITECTURE.md` (MCP section) and ADR-015.

## 8. Testing strategy

| Layer | What it proves | Where |
|---|---|---|
| Acceptance tests on real infrastructure | PostgreSQL, Chromium, fixture web app, MCP fixture server, worker subprocesses (including hard kills) | `tests/test_*.py` |
| MOCK model | Engine, policy and validation behaviour, deterministically. The scripted reply is chosen by the number of assistant turns, so it behaves identically after a crash and resume | `llm.ScriptedProvider` |
| Real-model tests and trials | Model quality and real cancellation, reported with failures | `tests/test_realmodel.py`, `tests/eval/` |
| Browser E2E | UI flows on desktop and phone viewports | `apps/web/e2e` |
| CI | Linux tests, E2E and a Docker Compose smoke test on every push | `.github/workflows/ci.yml` |

No test retries: a flaky test is treated as a bug. The first Linux CI run found two defects that Windows could not
show (backups containing symlinks; a wrong container image tag), which is why CI exists.

## 9. Measured characteristics (reference laptop, CPU only)

Services use about 0.8 GB at idle. `qwen2.5:3b` needs about 2.4 GB and generates 10–12 tokens/s; a report task takes
1–2 minutes when it succeeds. Under heavy load, real-model runs exceeded their deadlines and one cancellation took far
longer than designed; that open issue is first in the backlog. Full numbers: `docs/TEST_REPORT.md`.

## 10. Known limitations and next steps

- **Isolation**: native installs run approved commands and stdio MCP servers as the user. The next step is a
  network-less container per execution, and per-user microVMs for multi-tenant use.
- **Model quality on CPU**: validation catches wrong numbers but cannot produce right ones. Use a stronger model (GPU
  or hosted API), and evaluate an Apache-licensed 3–4B model.
- **Completeness checks**: skill-defined output contracts that validation can enforce.
- **Real-model regression with `qwen2.5:3b` under load**: investigate the slow cancellation path.
- See `docs/BACKLOG.md` for the ordered list.

## 11. Extending the system

| You want to add | Do this |
|---|---|
| An external tool server | Add it as an MCP server (no code); set a rule if its tools should run without approval |
| A built-in tool | `@tool(...)` in `tools.py` with a pydantic schema, effect class and target; add a test |
| A connector with reconciliation | Subclass `Connector` (capabilities, `reconcile` by idempotency key) |
| A model provider | Implement `chat/capabilities/health`, route it in `provider_for`, add a fake-server test |
| A validation rule | Add it to `validation.py` with a regression test built from the real failing output |

## 12. Design review questions (with short answers)

- *Why not let the model decide what is safe?* It can be prompt-injected; code-level policy cannot.
- *What happens if a worker dies right after calling an external API?* The call was committed as `running` with an
  idempotency key; recovery reconciles via the connector and never resends blindly.
- *How is approval replay prevented?* The approval stores a digest of the exact action and is consumed by an atomic
  conditional update; a second consume affects 0 rows.
- *How does DNS rebinding work, and how is it stopped?* A host resolves to a public IP for the check and to 127.0.0.1
  for the connection. Resolving once and connecting to the checked address removes the gap.
- *Why is a calculator result not trusted as evidence?* It proves arithmetic, not that the right quantity was computed.
- *Why PostgreSQL as the queue?* One transactional store, `SKIP LOCKED` claims, and replayable events, at the scale
  this system runs.
- *How do you test an agent deterministically?* A scripted model whose reply depends only on conversation state,
  plus real infrastructure for everything else, and separate real-model evaluations for quality.
