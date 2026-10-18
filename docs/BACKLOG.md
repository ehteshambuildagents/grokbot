# Implementation backlog

Ordered roughly by value. Each item lists what "done" means.

## Hardening (do first)
1. **Docker Compose verification on a Linux host**: `docker compose up`, run the pytest suite against it, record
   results in TEST_REPORT, then flip the CAPABILITIES rows to Tested.
2. **SSRF DNS pinning**: resolve once and connect to the checked IP (httpx transport + Chromium host-resolver
   rules) to close the DNS-rebinding window.
3. **Symlink-escape test on Linux/macOS** (skipped on this Windows host).
4. **Terminal resource limits on native hosts** (Windows Job Objects) or document "container only".
5. **Persistent login rate limiting** in PostgreSQL; optional TOTP MFA for remote access.
6. **Per-tenant internal tokens / mTLS** between worker and computer service.
7. **Shell-ambiguity test**: an automated test for the `ambiguous_command` resume path.
8. **Password-field refusal test**: the bot-typing guard needs a fixture page where the label resolves to a
   password field.

## Product
9. **Embeddings for memory**, only if a retrieval eval (to be written: queries + expected memory ids) shows FTS
   misses. Use Ollama `nomic-embed-text` + pgvector.
10. **LLM-based context summaries** for very long tasks, replacing the deterministic compaction once measured.
11. **MCP adapter** as a connector type (stdio servers in the computer sandbox), with per-tool effect classes.
12. **Office formats**: docx/xlsx read/write via python-docx/openpyxl in the computer service (isolated parsing).
13. **WebSocket live view** (CDP screencast) instead of 1.5 s screenshot polling.
14. **Task wait-for-handoff**: let a parent task block until children finish and merge their deliverables.
15. **Skill validation runs**: a "dry run" mode in which effectful tools need approval regardless of rules.
16. **Notifications**: optional email/ntfy push for routine failures and approvals (would require an explicit
   external-messaging connector and approval policy).

## Later phases
17. **Voice**: dictation (whisper.cpp / faster-whisper) and playable replies (Piper). Verify licenses and record
   measured latency before any live-voice work.
18. **Vision**: test a local vision model (e.g. qwen2.5-vl / llava class) with a coordinate-validation fixture
   before enabling screenshot understanding or visual control.
19. **Third-party connectors** (Slack, GitHub, email), each needing its own app registration, scopes, per-person
   OAuth binding, and cost/availability notes.
20. **Remote model adapters** (opt-in, disabled by default, with a data-egress disclosure screen).
21. **Multi-user hostile-code isolation** via per-user microVMs (see ARCHITECTURE.md).
