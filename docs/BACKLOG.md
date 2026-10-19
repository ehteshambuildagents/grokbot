# Implementation backlog

Ordered roughly by value. Each item lists what "done" means.

## Hardening (do first)
0. **Release-blocking for local-model users: real-model failures in the final regression.** Investigate the
   `qwen2.5:3b` cancellation that took 6,447 s (`test_real_ollama_cancellation_stops_generation`) and the 30-minute
   deadline overrun (`test_real_model_report_is_correct_or_rejected`) on an otherwise idle host; rerun
   `tests/eval/report_trials.py` on the final validator. Validate the hosted-API path with a real key.
1. **Docker/Linux verification on a real host** beyond the CI smoke test: long-running Compose deployment, upgrade,
   backup/restore inside containers.
2. **Terminal isolation**: run commands in a separate network-less container (or gVisor/microVM) instead of the
   browser's container; on Windows, a low-integrity or AppContainer token for filesystem isolation.
3. **Shell-ambiguity test** for the `ambiguous_command` resume path.
4. **Password-field refusal test**: a fixture page where a label resolves to a password input.
5. **Persistent login rate limiting** in PostgreSQL; optional TOTP MFA for remote access.
6. **Per-tenant internal tokens / mTLS** between worker and computer service.
7. **Completeness checks** for deliverables (requested sections present), e.g. a skill-defined output contract that
   validation enforces.
8. **Recomputation scope**: the one-step derivation check can accept a wrong number that happens to equal some
   combination of the inputs; consider requiring `calculate` provenance for every derived figure in strict mode.

Done in v0.2.0 (see CHANGELOG): DNS pinning + browser egress proxy; junction/hard-link escapes; Windows
Job Object limits and output draining; real Ollama cancellation; deterministic calculation and output validation;
lint (ruff, ESLint); CI on Linux with a Docker Compose smoke test.

## Product
9. **Embeddings for memory**, only if a retrieval eval (to be written: queries + expected memory ids) shows FTS
   misses. Use Ollama `nomic-embed-text` + pgvector.
10. **LLM-based context summaries** for very long tasks, replacing the deterministic compaction once measured.
11. **MCP beyond tools**: resources and prompts, OAuth 2.1 authorization for remote servers, per-server network
    policy for stdio servers.
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
