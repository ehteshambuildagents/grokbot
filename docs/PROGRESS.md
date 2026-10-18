# Progress log and handover

Read with `DECISIONS.md`, `CAPABILITIES.md`, `BACKLOG.md`.

## 2026-10-18: session 1

Done, by slice:
- **A** Host inspection, ADRs, portable PostgreSQL, schema (26 tables) + Alembic, auth (sessions, CSRF), bot CRUD,
  versions, duplicate, hide, pin, deletion impact.
- **B** Ollama provider (streaming, tools, structured output, capabilities), MOCK scripted provider, durable engine
  (claims, leases, heartbeats, recovery, checkpoints, retries, budgets, deadline, cancellation, redirects), SSE replay.
- **C** Tool registry with schema validation; file tools; terminal (argv/shell gated); browser tools in a separate
  computer service; artifacts with provenance; upload validation; SSRF guard.
- **D** Routines (NL → cron with confirmation, DST, missed-run policy, overlap, webhooks), approvals with digest
  binding, policy precedence, human takeover with control lease, password/MFA/CAPTCHA pauses.
- **E** Memory (FTS, revisions, scopes), skills (versions, approval gate, from task, from demonstration recording),
  handoffs with loop limits, QA workflow.
- **F (partial)** Teams (roles, shared bot config, private conversations), sanitized templates. Voice, Office,
  MCP, Slack/X: not started.
- Web UI for all 12 areas, responsive, Playwright E2E (desktop + phone).
- Ops: launcher, backup/restore, key rotation, Postgres setup. Compose files written but unverified.
- Model eval, model selection (qwen2.5:1.5b default), docs.

Local state on this machine (not in git): `.local/pgsql`, `.local/pgdata` (DB on 127.0.0.1:54329),
`.local/data`, `.local/keys/secrets.key`, `.local/dev-credentials.txt` (generated dev-only test account),
`.local/logs`.

## Next steps (suggested order)

1. Run the suite under Docker Compose on a Linux host (BACKLOG #1).
2. SSRF DNS pinning (#2) and the remaining unverified security rows.
3. Measure memory retrieval before considering embeddings.
4. Voice dictation (whisper.cpp) as the first slice-F capability.

## Conventions

- Tests must not use retries. MOCK tests use `scripted:<key>` models; real-model tests are marked `realmodel`.
- Update `CAPABILITIES.md` in the same change that adds, verifies or removes a capability.
- Regenerate `packages/contracts` (`python infra/scripts/export_contracts.py`) after API or tool changes.
