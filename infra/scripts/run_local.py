"""Start LocalCrew natively (no Docker): PostgreSQL (portable, if present) → migrations → computer service →
worker → API (serves the built web app on http://127.0.0.1:8000). Ctrl+C stops everything.

  python infra/scripts/run_local.py            # all services
  python infra/scripts/run_local.py --no-db    # PostgreSQL is managed elsewhere

Logs: .local/logs/<service>.log. Ollama must be running separately (ollama serve / the Ollama app).
Everything stops when this computer sleeps or shuts down; scheduled routines resume (per missed-run policy)
when it is started again.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
PG = LOCAL / "pgsql" / "bin"
PY = sys.executable
EXE = ".exe" if os.name == "nt" else ""


def wait(url, name, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                print(f"  ✓ {name}")
                return
        except httpx.HTTPError:
            time.sleep(0.5)
    sys.exit(f"{name} did not start; see .local/logs")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-db", action="store_true")
    a = ap.parse_args()
    logs = LOCAL / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "services" / "api")}
    procs = []

    def start(name, args, cwd=ROOT):
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=open(logs / f"{name}.log", "ab"), stderr=subprocess.STDOUT)
        procs.append((name, p))
        return p

    if not a.no_db and (PG / f"pg_ctl{EXE}").exists():
        st = subprocess.run([str(PG / "pg_ctl"), "-D", str(LOCAL / "pgdata"), "status"], capture_output=True)
        if st.returncode != 0:
            subprocess.run([str(PG / "pg_ctl"), "-D", str(LOCAL / "pgdata"), "-l", str(logs / "postgres.log"),
                            "-o", "-p 54329 -c listen_addresses=127.0.0.1", "-w", "start"], check=True)
        print("  ✓ PostgreSQL 127.0.0.1:54329")
    subprocess.run([PY, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "services" / "api", env=env, check=True)
    print("  ✓ migrations")
    try:
        httpx.get("http://127.0.0.1:11434/api/version", timeout=2)
        print("  ✓ Ollama reachable")
    except httpx.HTTPError:
        print("  ! Ollama not reachable on 127.0.0.1:11434 — start it, or tasks will wait and retry")
    start("computer", [PY, "services/computer/computer.py"])
    wait("http://127.0.0.1:8765/health", "computer service 127.0.0.1:8765")
    start("worker", [PY, "services/worker/worker.py"])
    print("  ✓ worker")
    start("api", [PY, "-m", "uvicorn", "localcrew.api:app", "--host", "127.0.0.1", "--port", "8000",
                  "--app-dir", "services/api"])
    wait("http://127.0.0.1:8000/api/setup", "API http://127.0.0.1:8000")
    if not (ROOT / "apps" / "web" / "dist" / "index.html").exists():
        print("  ! web app not built: cd apps/web && npm ci && npm run build   (or npm run dev on :5173)")
    print("\nLocalCrew is running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    try:
        while all(p.poll() is None for _, p in procs):
            time.sleep(1)
        dead = [n for n, p in procs if p.poll() is not None]
        print(f"service exited: {dead}; see .local/logs")
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
