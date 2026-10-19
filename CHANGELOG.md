# Changelog

All notable changes to LocalCrew. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may change behaviour).

## [0.3.0] - 2026-10-19

### Added
- **MCP servers as first-class integrations.** Connect any number of Model Context Protocol servers (stdio command or
  Streamable HTTP URL) from Connections → MCP servers or the API. Tools are discovered automatically and exposed to
  bots as `mcp__<server>__<tool>`, individually or per server (`mcp:<connection_id>`).
- MCP tool calls go through the same pipeline as built-in tools: JSON-Schema argument validation, policy decision
  derived from tool annotations (read-only → read; destructive → destructive; otherwise external write, which needs
  approval by default), idempotency keys, untrusted-output wrapping, redaction and cancellation.
- Stdio MCP servers run inside the computer service under the sandbox limits (Windows Job Objects / POSIX rlimits)
  with a scrubbed environment. One secret per server, injected as an env var or HTTP header by the broker; never
  visible to models.
- Wildcard policy rules (`mcp__github__*`) to allow or deny a whole server's tools.
- `pyproject.toml`, contribution and security guides, issue/PR templates, and an engineering walkthrough
  (`docs/ENGINEERING.md`).

### Fixed
- Linux: per-user `RLIMIT_NPROC` prevented stdio MCP servers (and threaded commands) from starting; process counts
  are now capped by the container's `pids_limit`. MCP startup errors report the root cause and server stderr.

## [0.2.0] - 2026-10-19

### Added
- Optional OpenAI-compatible model API provider (opt-in, data-egress disclosure in Settings). Tested against a mock
  server only.
- Deterministic `calculate` tool and deliverable validation: structure, non-answers, assistant commentary, language
  mismatch, and numeric grounding with narrow recomputation. One repair turn, then the task fails.
- GitHub Actions: Linux test suite, Playwright E2E, Docker Compose smoke test. ruff and ESLint.

### Fixed
- Workspace escape: file search followed Windows junctions out of the workspace.
- DNS rebinding: server-side HTTP now pins the validated address; Chromium traffic goes through a pinning egress proxy.
- Terminal: Job Object / rlimit limits, continuous output draining (large output could deadlock until timeout).
- Ollama cancellation now works before the first token (socket shutdown from a watchdog).
- Wrong reports could succeed because a file existed; several false acceptances found in real-model trials.
- A repair turn ignored the corrected final answer when the deliverable already existed.
- SSE endpoints held a database connection for the lifetime of the stream.
- A misconfigured model provider crashed the worker thread instead of failing the task.
- Backups archived links, which made restores fail; the Compose migrate step used a wrong config path.

### Known issues
- Local-model reliability on a loaded CPU-only host: see `docs/TEST_REPORT.md` (real-model regression failures with
  `qwen2.5:3b`).

## [0.1.0] - 2026-10-18

### Added
- Initial platform: persistent bots, durable PostgreSQL task engine (leases, heartbeats, checkpoints, recovery,
  retries, cancellation, redirects), Ollama provider, browser and terminal tools in a separate computer service,
  human takeover, approvals bound to action digests, idempotent external writes with reconciliation, routines
  (natural-language schedules, DST, missed-run policy, webhooks), memory, skills (including demonstrations),
  handoffs, teams, sanitized templates, React UI, backup/restore, acceptance test suite.

[0.3.0]: https://github.com/ehteshambuildagents/grokbot/compare/41e8967...main
[0.2.0]: https://github.com/ehteshambuildagents/grokbot/compare/fe25e4d...41e8967
[0.1.0]: https://github.com/ehteshambuildagents/grokbot/commit/fe25e4d
