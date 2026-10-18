# LocalCrew

Self-hosted AI teammates that run on your own computer. Each named bot has its own role, instructions, model,
tools, policy profile and memory. Bots take on durable tasks that keep running after you close the page. They use
a dedicated browser, a restricted terminal, shared files and connectors; they hand work to each other, follow
reusable skills and run on schedules. Effectful actions are gated by policy and your approvals. Results are
downloadable deliverables with the evidence behind them.

LocalCrew is an independent project inspired by publicly documented "AI teammate" workflows. It is **not
affiliated with Grok or xAI** and contains no proprietary code or assets.

> **Local means local.** Inference uses Ollama on this machine. No paid API, hosted database or commercial service
> is needed. **When this computer sleeps or shuts down, LocalCrew stops.** Routines catch up on restart according
> to their missed-run policy. For continuous operation, run it on an always-on host.

What works, what is untested, and what is missing: **[docs/CAPABILITIES.md](docs/CAPABILITIES.md)**.
Measured test results: **[docs/TEST_REPORT.md](docs/TEST_REPORT.md)**.

## Requirements (as tested)

- Windows 11 x64 (tested). Linux/macOS should work natively with a system PostgreSQL 17, but that is untested.
- Python 3.13, Node 24, ~4 GB free RAM for the 1.5B model plus services, ~5 GB disk.
- [Ollama](https://ollama.com) 0.40+ and at least one tool-capable model.

## Install (native, tested on the dev host)

Run from the repository root.

1. Python environment and browser:

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -r services/api/requirements.in -c services/api/requirements.lock
```

```bash
.venv/Scripts/python -m playwright install chromium
```

2. PostgreSQL 17 (portable, no admin rights; downloads ~315 MB from EnterpriseDB and verifies its SHA-256):

```bash
.venv/Scripts/python infra/scripts/setup_postgres.py
```

3. Model (Ollama, per-user install):

```bash
winget install --id Ollama.Ollama -e --scope user
```

```bash
ollama pull qwen2.5:1.5b
```

4. Web app:

```bash
cd apps/web && npm ci && npm run build
```

5. Start everything (PostgreSQL, migrations, computer service, worker, API + web UI):

```bash
.venv/Scripts/python infra/scripts/run_local.py
```

Open http://127.0.0.1:8000 and create the first account (default timezone Asia/Karachi). Two bots are seeded:
**Atlas** (research) and **Quill** (QA). For UI development with hot reload, run `npm run dev` in `apps/web` and
open http://127.0.0.1:5173 (it proxies `/api` to :8000).

On Linux/macOS replace `.venv/Scripts/` with `.venv/bin/` and provide PostgreSQL yourself
(`LOCALCREW_DATABASE_URL`, see `.env.example`).

## Docker Compose (Linux; **not verified** — Docker was unavailable on the dev host)

```bash
cp .env.example .env   # set POSTGRES_PASSWORD and LOCALCREW_COMPUTER_TOKEN
```

```bash
docker compose -f infra/docker-compose.yml up -d --build
```

Ollama stays on the host for GPU access; see [docs/MODELS.md](docs/MODELS.md#containers-reaching-host-ollama).

## Tests

```bash
.venv/Scripts/python -m pytest
```

The suite uses real PostgreSQL, real headless Chromium, a local fixture web app, real worker subprocesses (including
a hard kill), and the deterministic **MOCK** scripted model. The real-model test (`tests/test_realmodel.py`, marked
`realmodel`) runs when the Ollama model is present. Run it alone with `pytest -m realmodel`. Model evaluation:

```bash
.venv/Scripts/python tests/eval/model_eval.py qwen2.5:1.5b
```

Browser E2E (Playwright, desktop and phone viewports; starts its own isolated stack):

```bash
cd apps/web && npx playwright test
```

## Repository layout

| Path | Contents |
|---|---|
| `apps/web` | React 19 + TypeScript + Vite + Tailwind client (Bots, Groups, Tasks, Computer, Files, Skills, Routines, Connections, Approvals, Memory, Settings, Diagnostics) |
| `services/api` | FastAPI app and the shared `localcrew` package (models, engine, policy, tools, connectors), Alembic migrations |
| `services/worker` | Durable task + routine worker |
| `services/computer` | Browser (Playwright/Chromium) + terminal execution service |
| `packages/contracts` | Generated OpenAPI, tool schemas, event types |
| `skills` | Skill templates |
| `tests` | Pytest acceptance tests, fixture app, model eval, E2E server |
| `infra` | Compose, Dockerfiles, ops scripts (launcher, backup/restore, key rotation, Postgres setup) |
| `docs` | Architecture, decisions, capabilities, backlog, models, operations, test report, progress |

## Documentation

- [Architecture & trust boundaries](docs/ARCHITECTURE.md)
- [Engineering decisions](docs/DECISIONS.md)
- [Capability matrix](docs/CAPABILITIES.md) and [backlog](docs/BACKLOG.md)
- [Models & hardware](docs/MODELS.md)
- [Operations: backup, restore, upgrade, troubleshooting](docs/OPERATIONS.md)
- [Acceptance test report](docs/TEST_REPORT.md)
- [Progress log / handover](docs/PROGRESS.md)

## Costs and external dependencies

None for the core workflow. Downloads: Python/npm packages, Playwright Chromium (~118 MB), PostgreSQL binaries
(~315 MB), Ollama and models (1–2.5 GB each). Optional: a self-hosted SearXNG for web search. Third-party
integrations (Slack, X, …) are not implemented. When they are, each will need its own account, app approval and
possibly paid API access.

## License

Project code: choose a license before publishing (none declared yet). Model weights carry their own licenses:
`qwen2.5:1.5b` is Apache-2.0, `qwen2.5:3b` is under the Qwen Research License (non-commercial).
