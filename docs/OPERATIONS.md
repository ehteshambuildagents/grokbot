# Operations: run, back up, restore, upgrade, troubleshoot

## Always-on requirement

LocalCrew runs on this computer. **When it sleeps, hibernates or shuts down, nothing runs**: no tasks, no
routines, no webhooks. On restart, expired task leases are recovered automatically. Each routine's missed runs
follow its policy: `run_once` (one catch-up run) or `skip` (recorded as `skipped_missed`). For continuous
operation use an always-on machine (mini-PC, home server, VPS with enough RAM for the model) and disable sleep.

## Start / stop (native)

```bash
.venv/Scripts/python infra/scripts/run_local.py
```

This starts PostgreSQL (portable install), runs migrations, then starts the computer service, worker and API.
Open http://127.0.0.1:8000. Ctrl+C stops the services (PostgreSQL keeps running; stop it with
`.local/pgsql/bin/pg_ctl -D .local/pgdata stop`). Logs: `.local/logs/`.

## Backup

```bash
.venv/Scripts/python infra/scripts/ops.py backup D:/localcrew-backups/2026-10-18
```

This writes `db.dump` (pg_dump custom format), `data.tar.gz` (workspaces and artifacts), `secrets.key` and
`manifest.json` (SHA-256 of each file and the Alembic revision). Add `--include-browser-profiles` to include
sign-ins. **Move `secrets.key` to a different medium** than the rest: whoever holds both can decrypt stored
connector tokens. Without the key, connections must be re-entered; everything else restores fine.

## Restore (into a clean environment)

1. Create an empty database (and stop the services).
2. Run the restore:

```bash
.venv/Scripts/python infra/scripts/ops.py restore D:/localcrew-backups/2026-10-18 --database-url postgresql+psycopg://localcrew:PASSWORD@127.0.0.1:54329/localcrew_new --data-dir .local/data-new --key-file .local/keys-new/secrets.key
```

The restore verifies checksums, refuses a non-empty target database, extracts files with the safe `data` tar filter,
and runs `alembic upgrade head`. Point `LOCALCREW_DATABASE_URL`, `LOCALCREW_DATA_DIR` and
`LOCALCREW_SECRET_KEY_FILE` at the restored locations. Tested by `tests/test_ops.py::test_backup_restore_into_clean_environment`.

## Upgrade

1. Back up (above).
2. `git pull`, then `pip install -r services/api/requirements.in -c services/api/requirements.lock` and
   `cd apps/web && npm ci && npm run build`.
3. `cd services/api && ../../.venv/Scripts/python -m alembic upgrade head` (the launcher also does this).
4. Restart. Rollback: restore the backup into a clean database and check out the previous release.

## Internal service token

The API/worker authenticate to the computer service with a shared token. Natively, if `LOCALCREW_COMPUTER_TOKEN` is
not set, a random token is created at `.local/keys/computer.token` on first use. All three processes read it. In
Compose, set `LOCALCREW_COMPUTER_TOKEN` in `.env`. Rotate by changing it and restarting all services.

## Secret-key rotation

```bash
.venv/Scripts/python infra/scripts/ops.py rotate-keys
```

This prepends a new key to the key file and re-encrypts every secret. Old keys stay in the file, so older backups
still decrypt. Remove old lines only once no backup you might restore depends on them. Back up the key file
after rotating.

## Remote access (phone outside the LAN)

Not enabled by default. Services bind to 127.0.0.1. To allow it:
1. Put a TLS reverse proxy in front of `127.0.0.1:8000` (e.g. Caddy with a real certificate, or a private mesh
   VPN such as Tailscale/WireGuard; a VPN is safer than exposing a port).
2. Set `LOCALCREW_SECURE_COOKIES=true` and `LOCALCREW_PUBLIC_ORIGIN=https://your.host`.
3. Use a strong password. Login is rate-limited per IP, but there is no MFA yet.
4. Never expose Ollama (11434), PostgreSQL (54329) or the computer service (8765).

## Troubleshooting

| Symptom | Check |
|---|---|
| Tasks stay `queued` | Diagnostics → Worker. No heartbeat means the worker is not running (`.local/logs/worker.log`). |
| Tasks go `retry_wait` with "ollama unreachable" / 404 | `ollama serve` running? Model pulled (`ollama list`)? Bot model name correct? |
| Task `waiting_for_user` "session_expired"/"human_required" | Computer → Take control, sign in, Release. Blocked tasks resume automatically. |
| Task `waiting_for_user` "ambiguous_external_write" | Check the external system, then choose "It happened" / "Retry once" / "Skip" on the task card. |
| "computer service unavailable" | `.local/logs/computer.log`; Chromium installed (`python -m playwright install chromium`)? |
| Browser tool says "busy" | Another task holds the browser lease; it frees at task end or after 120 s. |
| `CSRF token missing or invalid` | Reload the page (new session token), or the session expired: sign in again. |
| Slow answers | CPU inference: ~20 tok/s for 1.5B on this laptop. Smaller prompts, fewer tools, or a GPU host. |
| Restore says "target database is not empty" | Restore only into a freshly created database. |
| PostgreSQL won't start | `.local/logs/postgres.log`; a stale `postmaster.pid` after a crash can be removed once no `postgres.exe` runs. |
