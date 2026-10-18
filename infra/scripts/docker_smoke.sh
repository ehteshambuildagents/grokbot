#!/usr/bin/env bash
# Docker Compose smoke test (used by CI): setup flow, worker liveness, computer-service isolation and limits.
set -euo pipefail
DC="docker compose --env-file .env -f infra/docker-compose.yml"

echo "== API setup + bot list"
curl -fs -c /tmp/c.txt -H 'Content-Type: application/json' \
  -d '{"email":"ci@localcrew.test","password":"ci-only-password-123","display_name":"CI"}' http://127.0.0.1:8000/api/setup >/dev/null
curl -fs -b /tmp/c.txt http://127.0.0.1:8000/api/bots | grep -q Atlas

echo "== diagnostics: database, worker heartbeat, computer service"
for i in $(seq 1 30); do
  D=$(curl -fs -b /tmp/c.txt http://127.0.0.1:8000/api/diagnostics)
  echo "$D" | grep -q '"workers":\[{' && break; sleep 2
done
echo "$D"
echo "$D" | grep -q '"database":{"ok":true}'
echo "$D" | grep -q '"workers":\[{'
echo "$D" | grep -q '"computer":{"ok":true'

echo "== containers run unprivileged"
test "$($DC exec -T api id -u)" != "0"
test "$($DC exec -T computer id -u)" != "0"

echo "== no Docker socket or host home mounted"
! $DC exec -T computer test -e /var/run/docker.sock
! $DC exec -T api test -e /var/run/docker.sock

echo "== terminal limits (POSIX rlimits) and egress SSRF guard, called from the api container"
$DC exec -T api python - <<'PY'
import os, httpx
h = {"X-Internal-Token": os.environ["LOCALCREW_COMPUTER_TOKEN"]}
uid = "c" * 32
os.makedirs(f"/data/workspaces/{uid}", exist_ok=True)
r = httpx.post("http://computer:8765/terminal/run", headers=h, timeout=90,
               json={"user_id": uid, "task_id": "ci", "argv": ["python3", "-c", "b=bytearray(1536*1024*1024); print('ALLOC'+'_OK')"]})
print("memory:", r.status_code, r.json()["exit_code"], r.json()["output"][-80:])
assert r.status_code == 200 and r.json()["exit_code"] != 0 and "ALLOC_OK" not in r.json()["output"]
r = httpx.post("http://computer:8765/terminal/run", headers=h, timeout=90,
               json={"user_id": uid, "task_id": "ci", "argv": ["python3", "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS))"]})
print("rlimit:", r.json()["output"].strip())
r = httpx.post("http://computer:8765/browser/open", headers=h, timeout=60,
               json={"user_id": uid, "task_id": "ci2", "url": "http://169.254.169.254/latest/meta-data"})
print("metadata:", r.status_code); assert r.status_code == 400
r = httpx.post("http://computer:8765/browser/open", headers=h, timeout=60,
               json={"user_id": uid, "task_id": "ci2", "url": "https://example.com/"})
print("public page:", r.status_code, r.json().get("title"))
assert r.status_code == 200
print("egress proxy:", httpx.get("http://computer:8765/health").json()["egress_proxy"])
PY
echo "docker smoke OK"
