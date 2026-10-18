"""Isolated stack for Playwright E2E: fresh `localcrew_e2e` database, temp data dir, MOCK scripted model allowed.
Starts fixture app, computer service, worker and API (serving apps/web/dist) on 127.0.0.1:18100.
Writes credentials for a generated test user to tests/e2e/.e2e-user.json (gitignored via .local? no: deleted on exit).
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "api"))
TMP = Path(tempfile.mkdtemp(prefix="localcrew-e2e-"))
BASE = os.environ.get("LOCALCREW_TEST_DATABASE_URL",
                      "postgresql+psycopg://localcrew:localcrew-dev-only@127.0.0.1:54329/localcrew_test").rsplit("/", 1)[0]
ENV = {**os.environ, "LOCALCREW_DATABASE_URL": f"{BASE}/localcrew_e2e", "LOCALCREW_DATA_DIR": str(TMP / "data"),
       "LOCALCREW_SECRET_KEY_FILE": str(TMP / "keys/secrets.key"), "LOCALCREW_ALLOW_SCRIPTED_MODEL": "1",
       "LOCALCREW_COMPUTER_URL": "http://127.0.0.1:18766", "LOCALCREW_COMPUTER_TOKEN": secrets.token_hex(16),
       "LOCALCREW_PUBLIC_ORIGIN": "http://127.0.0.1:18100", "LOCALCREW_SCRIPT_FILE": str(TMP / "scripts.json"),
       "PYTHONPATH": str(ROOT / "services" / "api"), "COMPUTER_PORT": "18766"}
os.environ.update(ENV)
CRED = ROOT / "tests" / "e2e" / ".e2e-user.json"


def main():
    from sqlalchemy import create_engine, text
    admin = create_engine(f"{BASE}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("drop database if exists localcrew_e2e with (force)"))
        c.execute(text("create database localcrew_e2e"))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "services/api", env=ENV, check=True)
    (TMP / "scripts.json").write_text(json.dumps({"e2e": [
        {"tool_calls": [{"name": "create_artifact", "arguments": {"name": "e2e-note.md", "content": "# E2E\nhello"}}]},
        {"content": "Created e2e-note.md for you."}]}))
    from localcrew.db import session_scope
    from localcrew.models import User
    from localcrew.security import hash_password
    pw = "e2e-" + secrets.token_urlsafe(12)
    with session_scope() as db:
        db.add(User(email="e2e@localcrew.test", password_hash=hash_password(pw), display_name="E2E", timezone="Asia/Karachi"))
    CRED.write_text(json.dumps({"email": "e2e@localcrew.test", "password": pw}))
    procs = [subprocess.Popen([sys.executable, *a], cwd=ROOT, env=ENV) for a in (
        ["tests/fixtures/fixture_app.py"], ["services/computer/computer.py"], ["services/worker/worker.py"],
        ["-m", "uvicorn", "localcrew.api:app", "--port", "18100", "--app-dir", "services/api", "--log-level", "warning"])]
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    finally:
        for p in procs:
            p.kill()
        CRED.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
