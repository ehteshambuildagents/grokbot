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

## 2026-10-18: session 2 (verification pass before publication)

Found and fixed (each has a regression test):
- **Workspace escape via Windows junction**: file search returned contents of a file outside the workspace.
- **DNS rebinding**: check-then-connect was exploitable (demonstrated in a test). Added pinned resolution for
  server-side HTTP and a pinning egress proxy for all Chromium traffic.
- **Ollama cancellation did not work during prompt evaluation** (took 60 s). Now uses socket shutdown from a
  watchdog; ~1.5 s.
- **Terminal**: large output could deadlock until timeout; there were no memory or process limits on Windows. Added
  Job Objects, output draining and caps.
- **Wrong reports could succeed**: added `calculate`, numeric grounding with narrow recomputation, structure and
  content checks, a repair turn, and Markdown extraction from chatty answers. Real-model trials then found three
  more false acceptances (clock time as evidence, 4 × 12.5 recomputation, "+50%"); fixed and regression-tested.
- Repair turns ignored a corrected final answer when the deliverable already existed.
- SSE endpoints held a DB connection "idle in transaction" for the whole stream.
- Hard-coded default internal token; lint findings (ruff, ESLint react-hooks).
- Default model switched to `qwen2.5:3b` based on trial results (license caveat documented).

Publication: history rewritten before the first push (author → GitHub noreply address, local path scrubbed from a
test log). No credentials were ever committed.

## Next steps (suggested order)

1. Check the GitHub Actions results (Linux + Docker); fix anything Linux-specific (BACKLOG #1).
2. Terminal isolation (separate network-less container), shell-ambiguity and password-field tests (BACKLOG #2–#4).
3. Improve report success rates: an Apache-2.0 model in the 3–4B class (evaluate before switching), and
   skill-defined output contracts for completeness checks.
4. Measure memory retrieval before considering embeddings; voice dictation as the first slice-F capability.

## Conventions

- Tests must not use retries. MOCK tests use `scripted:<key>` models; real-model tests are marked `realmodel`.
- Update `CAPABILITIES.md` in the same change that adds, verifies or removes a capability.
- Regenerate `packages/contracts` (`python infra/scripts/export_contracts.py`) after API or tool changes.
