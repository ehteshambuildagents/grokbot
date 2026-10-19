# LocalCrew (repository: grokbot)

**An independent, self-hosted AI teammate platform.** Named bots with their own roles, instructions, models, tools,
policies and memory take on durable tasks that keep running after you close the page. They work in a dedicated
browser, a restricted terminal, shared files and connectors; they hand work to each other, follow reusable skills
and run on schedules. Effectful actions are gated by policy and your approvals. Results are downloadable
deliverables with the evidence behind them, and they are checked before a task is allowed to succeed.

> **Not affiliated with Grok or xAI.** LocalCrew was inspired by publicly described "AI teammate" workflows. It is
> independently built, contains no proprietary code or assets, and does not reproduce any official bot identity,
> routing, search access or account linking. "Grok" appears only in the repository name.

> **Local by default.** Inference runs on your machine through Ollama; no paid API, hosted database or commercial
> service is needed. Optionally, bots can use **any OpenAI-compatible model API** (OpenAI, OpenRouter, Groq, …),
> which is off until you enable it in `.env` and then sends task data to that provider. **When the computer sleeps or shuts down, LocalCrew stops.** Routines catch up on restart
> according to their missed-run policy. For continuous operation, run it on an always-on host.

Status of every feature, honestly labelled: **[docs/CAPABILITIES.md](docs/CAPABILITIES.md)**. Measured results:
**[docs/TEST_REPORT.md](docs/TEST_REPORT.md)**.

## What works (tested)

- Bots: create, edit (versioned), pin, hide, duplicate (config only), delete with an impact report; group chats with
  @mentions.
- Durable tasks in PostgreSQL: leases, heartbeats, crash recovery from checkpoints, retries with backoff, deadlines,
  cancellation (including a running Ollama generation, browser navigation and terminal process trees), and
  redirects. The persisted event stream (SSE) replays after reconnects.
- Tools with schema validation and policy enforced outside the model. Approvals are bound to an exact action digest
  (no replay, changed payloads re-ask). External writes use idempotency keys and are reconciled, never blindly retried.
- Dedicated Chromium per user with persistent sign-ins, live view, human takeover, pauses for passwords/MFA/CAPTCHAs,
  and DNS-rebinding-safe egress.
- Restricted terminal: argv-only, approval by default, Windows Job Objects / POSIX rlimits, output caps, tree kill.
- **Deterministic `calculate` tool and output validation**: every number in a deliverable must come from your
  request or a tool result, or be recomputable from your figures. Malformed tables, wrapped Markdown and non-answers
  are rejected. The model gets one repair turn, then the task fails.
- Memory (editable, scoped, revisioned), skills (versioned, from tasks or recorded demonstrations), routines (natural
  language → cron with confirmation, DST, missed-run policy, webhooks), teams, sanitized templates.
- Responsive web UI for desktop and phone.

## Known issues (open)

- **Local-model reliability on CPU:** in the final regression with `qwen2.5:3b` on a heavily loaded laptop, a
  real-model report task exceeded its 30-minute deadline, and cancelling a generation took 6,447 s (it took ~1.5 s
  with `qwen2.5:1.5b`). Root cause not yet found.
- **Small-model output quality:** validation rejects wrong figures, but correct reports are produced only some of
  the time, and validation cannot catch a correct figure under a wrong label.
- **Hosted model API:** tested only against a mock server; validate it with your own key.

## Not implemented / needs configuration

- **Not implemented:** voice, image/video generation, Office formats, MCP, Slack/X connectors, embeddings, vision
  models, visual (coordinate) computer control, isolation for hostile multi-user code.
- **Needs configuration:** hosted model API (base URL + your API key; tested only against a mock server here), web
  search (self-hosted SearXNG), remote access (TLS reverse proxy or VPN), Docker deployment (Linux; verified by the
  CI smoke test, not by a long-running deployment).
- Full list with reasons: [docs/CAPABILITIES.md](docs/CAPABILITIES.md) and [docs/BACKLOG.md](docs/BACKLOG.md).

## Hardware

| | Minimum (tested) | Notes |
|---|---|---|
| CPU | 4+ cores x86-64 (tested: Intel i7-1265U, 10C/12T) | CPU-only inference works but is slow |
| RAM | 8 GB with `qwen2.5:1.5b`; **16 GB recommended** with `qwen2.5:3b` (default) | services ≈ 0.8 GB, 3B model ≈ 2.4 GB |
| GPU | not required | an 8 GB+ GPU makes larger models practical (untested here) |
| Disk | ~6 GB | PostgreSQL, Chromium, models (1–2 GB each) |

On the tested laptop, a report task with `qwen2.5:3b` typically took 1–2 minutes when it succeeded; small models
often fail validation. Numbers: [docs/TEST_REPORT.md](docs/TEST_REPORT.md).

## Install and run (Windows, tested)

Requirements: Python 3.13, Node 24, git. Run from the repository root.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -r services/api/requirements.in -c services/api/requirements.lock
```

```bash
.venv/Scripts/python -m playwright install chromium
```

Portable PostgreSQL 17 (no admin rights; downloads ~315 MB from EnterpriseDB, verifies SHA-256, listens on
127.0.0.1:54329 only):

```bash
.venv/Scripts/python infra/scripts/setup_postgres.py
```

Ollama and the default local model (skip this if you use a model API, see below; `qwen2.5:3b` is under the **Qwen Research License, non-commercial**; for an
Apache-2.0 model use `qwen2.5:1.5b` and set `LOCALCREW_DEFAULT_MODEL=qwen2.5:1.5b`, see "Models" below):

```bash
winget install --id Ollama.Ollama -e --scope user
```

```bash
ollama pull qwen2.5:3b
```

Build the web app:

```bash
cd apps/web && npm ci && npm run build
```

Start PostgreSQL, run migrations, then start the computer service, worker and API (the API also serves the UI):

```bash
.venv/Scripts/python infra/scripts/run_local.py
```

Open http://127.0.0.1:8000 and create the first account. Two bots are seeded: Atlas (research) and Quill (QA).
Settings live in `.env` (copy `.env.example`; it contains no credentials).

**Linux/macOS (native):** replace `.venv/Scripts/` with `.venv/bin/`, provide PostgreSQL 17 yourself
(`LOCALCREW_DATABASE_URL`), and install Ollama from ollama.com. A GitHub Actions workflow runs the test suite on
Ubuntu 24.04; see the Actions tab for current results.

**Docker Compose (Linux):** `cp .env.example .env`, set `POSTGRES_PASSWORD` and `LOCALCREW_COMPUTER_TOKEN`, then
`docker compose --env-file .env -f infra/docker-compose.yml up -d --build`. Ollama stays on the host. See
[docs/MODELS.md](docs/MODELS.md#containers-reaching-host-ollama). Docker could not be run on the development
machine; the compose file is exercised by the CI smoke test.

## Using a model API instead of a local model

No GPU or slow laptop? Put this in `.env` (any OpenAI-compatible provider works) and restart:

```
LOCALCREW_REMOTE_PROVIDERS_ENABLED=true
LOCALCREW_REMOTE_BASE_URL=https://api.openai.com/v1
LOCALCREW_REMOTE_API_KEY=<your key>
LOCALCREW_REMOTE_MODELS=gpt-4o-mini
LOCALCREW_DEFAULT_MODEL=remote:gpt-4o-mini
```

You can then skip installing Ollama. Task data (instructions, conversation, memories, tool results) is sent to the
provider and you pay per token. Details and other providers: [docs/MODELS.md](docs/MODELS.md#hosted-model-apis-optional).

## Models

| Model | License | Report trials on the test laptop | Use |
|---|---|---|---|
| `qwen2.5:3b` (default) | Qwen Research License (non-commercial) | see [TEST_REPORT](docs/TEST_REPORT.md) | best measured results here |
| `qwen2.5:1.5b` | Apache-2.0 | see [TEST_REPORT](docs/TEST_REPORT.md) | low memory; outputs often rejected by validation |

Small local models make mistakes. LocalCrew's validation rejects wrong or unverifiable numbers instead of passing
them through, but it cannot judge prose quality or completeness. Review deliverables before relying on them.

## Tests

```bash
.venv/Scripts/ruff check .
```

```bash
.venv/Scripts/python -m pytest
```

```bash
cd apps/web && npm run lint && npm run build && npx playwright test
```

The pytest suite uses real PostgreSQL, headless Chromium, a local fixture web app and real worker subprocesses,
including a hard kill. Deterministic engine tests use a clearly labelled **MOCK** scripted model; real-model tests
(`pytest -m realmodel`) use Ollama and run when the model is installed. Real-model report trials:
`python tests/eval/report_trials.py 5 qwen2.5:3b`.

## Repository layout

| Path | Contents |
|---|---|
| `apps/web` | React 19 + TypeScript + Vite + Tailwind client |
| `services/api` | FastAPI app and the shared `localcrew` package (models, engine, policy, validation, tools, connectors), Alembic migrations |
| `services/worker` | Durable task and routine worker |
| `services/computer` | Browser (Playwright/Chromium), egress proxy, terminal sandbox |
| `packages/contracts` | Generated OpenAPI, tool schemas, event types |
| `skills` | Skill templates |
| `tests` | Acceptance tests, fixture app, model evals, E2E server |
| `infra` | Docker Compose, Dockerfiles, ops scripts (launcher, backup/restore, key rotation, Postgres setup, Docker smoke) |
| `docs` | Architecture, decisions, capabilities, backlog, models, operations, test report, progress |

## Security model in one paragraph

Services bind to 127.0.0.1. Sessions use HttpOnly SameSite=Strict cookies with CSRF tokens. Secrets are encrypted
with a key file kept outside the database and are never shown to models. Page, file and connector content is
treated as untrusted data. Effectful actions pass a policy engine (deny wins) and, by default, need your approval.
**Natively, an approved terminal command runs as your user and can read what you can.** Strong isolation needs the
container deployment, and hostile multi-user use needs per-user VMs. Details:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Costs

None for the core workflow. You download open-source packages, Chromium, PostgreSQL binaries, Ollama and model
weights. Optional integrations (Slack, X, …) are not implemented and would need their own accounts and, for some,
paid API access.

## License

Apache-2.0 (see [LICENSE](LICENSE)). Dependency licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Model
weights are not included and carry their own licenses.
