# Contributing

Start with [docs/ENGINEERING.md](docs/ENGINEERING.md) (how the system works and why), then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DECISIONS.md](docs/DECISIONS.md).

## Development setup

Follow "Install and run" in the [README](README.md). Day-to-day commands (Windows paths shown; use `.venv/bin/` on
Linux/macOS):

| Task | Command |
|---|---|
| Lint Python | `.venv/Scripts/ruff check .` |
| Backend tests | `.venv/Scripts/python -m pytest` |
| Real-model tests (needs Ollama + model) | `.venv/Scripts/python -m pytest -m realmodel` |
| Web lint / typecheck / build | `cd apps/web && npm run lint && npm run build` |
| Browser E2E | `cd apps/web && npx playwright test` |
| Regenerate API/tool contracts | `.venv/Scripts/python infra/scripts/export_contracts.py` |
| New DB migration | `cd services/api && ../../.venv/Scripts/python -m alembic revision --autogenerate -m "..."` |

## Engineering rules

- **Behaviour changes ship with a test.** Prefer a test against real infrastructure (PostgreSQL, Chromium, fixture
  app, MCP fixture server) over mocks. Model decisions in deterministic tests come from the `scripted:` MOCK
  provider, labelled as such. Real-model behaviour belongs in `-m realmodel` tests or `tests/eval/`.
- **No retries to hide flakiness.** A failing test is a bug in the code or the test; fix one of them.
- **Policy is enforced in code, never by the model.** Anything with side effects gets an effect class and goes
  through `policy.decide()`.
- **Untrusted input stays data.** Web pages, files, connector and MCP output are wrapped as untrusted and redacted.
- **Keep docs honest.** Update `docs/CAPABILITIES.md` in the same change that adds, verifies or removes a capability,
  and add a `CHANGELOG.md` entry under "Unreleased".

## Extending

- **Built-in tool:** add a function with `@tool(...)` in `services/api/localcrew/tools.py` (pydantic argument model,
  effect class, target), then a test.
- **Connector:** subclass `Connector` in `connectors.py` (capabilities, schemas, read/write class, health, reconcile).
- **MCP server:** no code needed. Add it under Connections → MCP servers (or `POST /api/connections` with
  `connector: "mcp"`). For servers that need Node/uv inside Docker, extend `infra/docker/computer.Dockerfile`.
- **Model provider:** implement `chat()`/`capabilities()`/`health()` like `remote.py` and route it in
  `llm.provider_for()`.

## Commits and pull requests

Small, focused commits with an imperative subject line and a body that says *why*. PRs need green CI (lint, tests,
E2E, Docker smoke) and must fill in the PR template's test and risk sections.
