"""LocalCrew operations: backup, restore, key rotation, user creation.

  python infra/scripts/ops.py backup  <dest-dir> [--include-browser-profiles]
  python infra/scripts/ops.py restore <backup-dir> [--database-url URL] [--data-dir DIR] [--key-file FILE]
  python infra/scripts/ops.py rotate-keys
  python infra/scripts/ops.py create-user <email> <display-name>   (password read from stdin)

Backups contain: PostgreSQL dump (custom format), workspace + artifact files, and the secret key file.
Store the key file separately from the rest of the backup: anyone holding both can decrypt stored secrets.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "api"))


def pg_bin(name: str) -> str:
    for d in [os.environ.get("PG_BIN"), str(ROOT / ".local" / "pgsql" / "bin")]:
        if d and (Path(d) / (name + (".exe" if os.name == "nt" else ""))).exists():
            return str(Path(d) / name)
    found = shutil.which(name)
    if not found:
        sys.exit(f"{name} not found; set PG_BIN")
    return found


def libpq_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(dest: Path, include_browser=False):
    from localcrew.config import settings
    s = settings()
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run([pg_bin("pg_dump"), "-Fc", "--no-owner", "-f", str(dest / "db.dump"), libpq_url(s.database_url)],
                   check=True)
    with tarfile.open(dest / "data.tar.gz", "w:gz") as tar:
        for sub in ["workspaces", "artifacts"] + (["browser-profiles"] if include_browser else []):
            if (s.data_dir / sub).exists():
                tar.add(s.data_dir / sub, arcname=sub)
    if s.secret_key_file.exists():
        shutil.copy2(s.secret_key_file, dest / "secrets.key")
    from sqlalchemy import create_engine, text
    with create_engine(s.database_url).connect() as c:
        rev = c.execute(text("select version_num from alembic_version")).scalar()
    manifest = {"created_at": datetime.now(UTC).isoformat(), "alembic_revision": rev,
                "includes_browser_profiles": include_browser,
                "files": {p.name: sha256_file(p) for p in dest.iterdir() if p.is_file() and p.name != "manifest.json"}}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"backup written to {dest}")
    return manifest


def restore(src: Path, database_url: str | None, data_dir: Path | None, key_file: Path | None):
    from localcrew.config import settings
    s = settings()
    database_url = database_url or s.database_url
    data_dir = data_dir or s.data_dir
    key_file = key_file or s.secret_key_file
    manifest = json.loads((src / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        if sha256_file(src / name) != digest:
            sys.exit(f"checksum mismatch for {name}; backup is corrupt")
    from sqlalchemy import create_engine, text
    with create_engine(database_url).connect() as c:
        n = c.execute(text("select count(*) from information_schema.tables where table_schema='public'")).scalar()
    if n:
        sys.exit("target database is not empty; restore only into a clean database")
    subprocess.run([pg_bin("pg_restore"), "--no-owner", "--exit-on-error", "-d", libpq_url(database_url),
                    str(src / "db.dump")], check=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src / "data.tar.gz") as tar:
        tar.extractall(data_dir, filter="data")  # 'data' filter rejects absolute paths, links outside, devices
    if (src / "secrets.key").exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "secrets.key", key_file)
    env = {**os.environ, "LOCALCREW_DATABASE_URL": database_url, "PYTHONPATH": str(ROOT / "services" / "api")}
    subprocess.run([sys.executable, "-m", "alembic", "-c", str(ROOT / "services/api/alembic.ini"), "upgrade", "head"],
                   cwd=ROOT / "services" / "api", env=env, check=True)
    print(f"restored {src} → {database_url.split('@')[-1]} and {data_dir}")


def rotate_keys():
    from localcrew.db import session_scope
    from localcrew.security import rotate_secret_keys
    with session_scope() as db:
        n = rotate_secret_keys(db)
    print(f"re-encrypted {n} secrets with a new key; old keys remain in the key file until you remove them")


def create_user(email, name):
    from localcrew.db import session_scope
    from localcrew.models import User
    from localcrew.security import hash_password
    pw = sys.stdin.readline().strip()
    if len(pw) < 10:
        sys.exit("password must be at least 10 characters")
    with session_scope() as db:
        db.add(User(email=email.lower(), password_hash=hash_password(pw), display_name=name))
    print("user created")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("dest", type=Path)
    b.add_argument("--include-browser-profiles", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("src", type=Path)
    r.add_argument("--database-url")
    r.add_argument("--data-dir", type=Path)
    r.add_argument("--key-file", type=Path)
    sub.add_parser("rotate-keys")
    u = sub.add_parser("create-user")
    u.add_argument("email")
    u.add_argument("name")
    a = ap.parse_args()
    if a.cmd == "backup":
        backup(a.dest, a.include_browser_profiles)
    elif a.cmd == "restore":
        restore(a.src, a.database_url, a.data_dir, a.key_file)
    elif a.cmd == "rotate-keys":
        rotate_keys()
    else:
        create_user(a.email, a.name)


if __name__ == "__main__":
    main()
