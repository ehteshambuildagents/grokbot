# Contracts

Generated — do not edit by hand. Regenerate with `python infra/scripts/export_contracts.py`.

- `openapi.json` — HTTP API (FastAPI/OpenAPI 3.1).
- `tools.json` — every bot tool: effect class (used by policy) and JSON Schema of its arguments
  (validated before execution).

## Task event stream (SSE)

`GET /api/events?after=<id|-1>` and `GET /api/tasks/{id}/events?after=<id>`; reconnects resume with
`Last-Event-ID`. Each event: `{"task_id", "type", "data", "at"}` with `id:` = persisted event id.

| type | data |
|---|---|
| status | `{status, phase?, error?, blocker?}` |
| step | `{kind: plan|tool|validate|report|approval|reconcile|redirect, summary, status}` |
| progress | `{text}` |
| delta | `{text}` (batched model output) |
| approval_requested | `{approval_id, tool, target}` |
| handoff / handoff_completed | handoff spec / `{child_task_id, status, summary}` |
| recovered | `{previous_worker, attempt}` |
| routine, notification, redirect, cancel_requested, usage | see `services/api/localcrew/engine.py` |
