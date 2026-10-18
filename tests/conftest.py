"""Acceptance-test harness.

Real components: PostgreSQL (test database), Alembic migrations, FastAPI app, computer service with real
headless Chromium (subprocess), fixture web app (subprocess), worker engine.
MOCK component: the 'scripted:*' model provider (deterministic). Real-model tests live in tests/eval and
are marked `realmodel`.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="localcrew-test-"))
COMPUTER_PORT, FIXTURE_PORT = 18765, 8899
ENV = {
    "LOCALCREW_DATABASE_URL": os.environ.get(
        "LOCALCREW_TEST_DATABASE_URL", "postgresql+psycopg://localcrew:localcrew-dev-only@127.0.0.1:54329/localcrew_test"),
    "LOCALCREW_DATA_DIR": str(TMP / "data"),
    "LOCALCREW_SECRET_KEY_FILE": str(TMP / "keys" / "secrets.key"),
    "LOCALCREW_ALLOW_SCRIPTED_MODEL": "1",
    "LOCALCREW_COMPUTER_URL": f"http://127.0.0.1:{COMPUTER_PORT}",
    "LOCALCREW_COMPUTER_TOKEN": "test-internal-token",
    "LOCALCREW_PRIVATE_HOST_ALLOWLIST": f"127.0.0.1:{FIXTURE_PORT},localhost:{FIXTURE_PORT}",
    "LOCALCREW_LEASE_SECONDS": "4",
    "LOCALCREW_HEARTBEAT_SECONDS": "1",
    "LOCALCREW_PUBLIC_ORIGIN": "http://testserver",
    "LOCALCREW_SCRIPT_FILE": str(TMP / "scripts.json"),
}
os.environ.update(ENV)
sys.path.insert(0, str(ROOT / "services" / "api"))
FIXTURE = f"http://127.0.0.1:{FIXTURE_PORT}"
PY = sys.executable


def wait_http(url, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


class Procs:
    def __init__(self):
        self.p = {}

    def start(self, name, args, health=None, env=None):
        log = open(TMP / f"{name}.log", "ab")
        self.p[name] = subprocess.Popen([PY, *args], cwd=ROOT, env={**os.environ, **(env or {})},
                                        stdout=log, stderr=subprocess.STDOUT)
        if health:
            wait_http(health)
        return self.p[name]

    def kill(self, name):
        p = self.p.pop(name, None)
        if p and p.poll() is None:
            p.kill()  # hard kill: simulates a crash (no cleanup handlers run)
            p.wait(10)

    def stop_all(self):
        for n in list(self.p):
            self.kill(n)


@pytest.fixture(scope="session")
def procs():
    from sqlalchemy import create_engine, text
    eng = create_engine(ENV["LOCALCREW_DATABASE_URL"])
    with eng.begin() as c:
        c.execute(text("drop schema public cascade; create schema public"))
    eng.dispose()
    subprocess.run([PY, "-m", "alembic", "-c", str(ROOT / "services/api/alembic.ini"), "upgrade", "head"],
                   cwd=ROOT / "services/api", check=True, env={**os.environ, "PYTHONPATH": str(ROOT / "services/api")})
    ps = Procs()
    ps.start("fixture", ["tests/fixtures/fixture_app.py"], f"{FIXTURE}/health")
    start_computer(ps)
    yield ps
    ps.stop_all()


def start_computer(ps):
    ps.start("computer", ["services/computer/computer.py"], f"http://127.0.0.1:{COMPUTER_PORT}/health",
             env={"COMPUTER_PORT": str(COMPUTER_PORT)})


@pytest.fixture(autouse=True)
def _reset_fixture(procs):
    httpx.post(f"{FIXTURE}/admin/reset")
    set_scripts({})
    yield


def set_scripts(scripts: dict):
    from localcrew.llm import ScriptedProvider
    ScriptedProvider.scripts = scripts
    Path(ENV["LOCALCREW_SCRIPT_FILE"]).write_text(json.dumps(scripts))


_users = iter(range(1, 10_000))


class Client:
    """Signed-in API client (in-process ASGI) with CSRF header handling."""

    def __init__(self, email=None):
        from fastapi.testclient import TestClient
        from localcrew.api import app
        from localcrew.db import session_scope
        from localcrew.models import User
        from localcrew.security import hash_password
        from localcrew.api import SEED_BOTS, make_bot
        self.c = TestClient(app)
        n = next(_users)
        self.email = email or f"user{n}-{time.time_ns()}@example.test"
        with session_scope() as db:
            u = User(email=self.email, password_hash=hash_password("correct horse battery"), display_name=f"U{n}",
                     timezone="Asia/Karachi")
            db.add(u)
            db.flush()
            for spec in SEED_BOTS:  # MOCK model by default; real-model tests opt in explicitly
                make_bot(db, u.id, {**spec, "model": "scripted:default"})
            self.user_id = u.id
        r = self.c.post("/api/auth/login", json={"email": self.email, "password": "correct horse battery"})
        r.raise_for_status()
        self.csrf = r.json()["csrf"]

    def __getattr__(self, method):
        def call(url, **kw):
            kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
            return getattr(self.c, method)(url, **kw)
        return call

    def ok(self, method, url, status=None, **kw):
        r = getattr(self, method)(url, **kw)
        assert r.status_code < 400 if status is None else r.status_code == status, (r.status_code, r.text)
        return r.json()

    def bot(self, name="Atlas", **patch):
        b = next(b for b in self.ok("get", "/api/bots") if b["name"] == name)
        if patch:
            b = self.ok("patch", f"/api/bots/{b['id']}", json=patch)
        return b

    def say(self, bot, content, **kw):
        conv = self.ok("get", f"/api/bots/{bot['id']}/conversation")
        r = self.ok("post", f"/api/conversations/{conv['id']}/messages", json={"content": content, **kw})
        return r["task_ids"][0], conv["id"]

    def task(self, tid):
        return self.ok("get", f"/api/tasks/{tid}")


@pytest.fixture
def client(procs):
    return Client()


def drain(max_tasks=20, worker="test-worker"):
    """Run queued tasks in-process until none are claimable (deterministic worker)."""
    from localcrew import engine
    n = 0
    while n < max_tasks:
        tid = engine.claim(worker)
        if not tid:
            return n
        engine.run_claimed(tid, worker)
        n += 1
    return n


def call(tool_name, **arguments):
    return {"name": tool_name, "arguments": arguments}
