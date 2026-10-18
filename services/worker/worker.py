"""LocalCrew worker: claims durable tasks, runs routines, expires approvals.

Runs independently of the web client. Stops when the host sleeps or shuts down; on restart it recovers
expired leases and catches up missed routine runs per each routine's missed-run policy."""
import logging
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from localcrew import engine, routines  # noqa: E402
from localcrew.config import settings  # noqa: E402
from localcrew.db import session_scope  # noqa: E402
from localcrew.policy import expire_stale  # noqa: E402

log = logging.getLogger("localcrew.worker")


def task_loop(worker_id: str, stop: threading.Event):
    while not stop.is_set():
        try:
            tid = engine.claim(worker_id)
        except Exception:  # noqa: BLE001 — DB outage: back off and retry
            log.exception("claim failed")
            stop.wait(3)
            continue
        if tid is None:
            stop.wait(0.5)
            continue
        log.info("running task %s", tid)
        engine.run_claimed(tid, worker_id)


def scheduler_loop(stop: threading.Event, base: str):
    from datetime import timedelta

    from localcrew.db import utcnow
    from localcrew.models import ExecutionLease
    while not stop.is_set():
        try:
            with session_scope() as db:  # liveness record shown in Diagnostics
                db.merge(ExecutionLease(resource=f"worker:{base}", holder_kind="worker", holder_id=base,
                                        expires_at=utcnow() + timedelta(seconds=10)))
            n = routines.tick()
            if n:
                log.info("fired %d routine run(s)", n)
            with session_scope() as db:
                expire_stale(db)
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        stop.wait(2)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    base = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    stop = threading.Event()
    threads = [threading.Thread(target=scheduler_loop, args=(stop, base), daemon=True, name="scheduler")]
    for i in range(settings().worker_concurrency):
        threads.append(threading.Thread(target=task_loop, args=(f"{base}-{i}", stop), daemon=True, name=f"task-{i}"))
    for t in threads:
        t.start()
    log.info("worker %s started with %d task threads", base, settings().worker_concurrency)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
