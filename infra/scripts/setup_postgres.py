"""Install a portable PostgreSQL for native development (Windows x64; no admin rights, no service).

  python infra/scripts/setup_postgres.py

Downloads the official EnterpriseDB PostgreSQL 17.6 binaries zip (~315 MB), verifies its SHA-256, extracts
to .local/pgsql, initialises .local/pgdata (scram-sha-256 auth), starts it on 127.0.0.1:54329 and creates the
`localcrew` and `localcrew_test` databases. Remove everything by deleting .local/pgsql and .local/pgdata.
On Linux/macOS use your package manager or Docker instead and set LOCALCREW_DATABASE_URL.

The default password `localcrew-dev-only` matches the default LOCALCREW_DATABASE_URL and is acceptable only
because the server listens on 127.0.0.1. Override with LOCALCREW_DB_PASSWORD (and update the URL in .env).
"""
import hashlib
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
URL = "https://get.enterprisedb.com/postgresql/postgresql-17.6-1-windows-x64-binaries.zip"
SHA256 = "d378882abd001a186735acd6f6ba716bca6ccd192e800412d4fd15ed25376b3e"  # verified 2026-10-18
PW = os.environ.get("LOCALCREW_DB_PASSWORD", "localcrew-dev-only")


def main():
    if os.name != "nt":
        sys.exit("This helper targets Windows. Use your OS package manager or Docker for PostgreSQL 17.")
    bin_ = LOCAL / "pgsql" / "bin"
    if not (bin_ / "pg_ctl.exe").exists():
        dl = LOCAL / "dl" / "pg.zip"
        dl.parent.mkdir(parents=True, exist_ok=True)
        print("downloading", URL)
        urllib.request.urlretrieve(URL, dl)
        h = hashlib.sha256(dl.read_bytes()).hexdigest()
        if h != SHA256:
            dl.unlink()
            sys.exit(f"checksum mismatch ({h}); refusing to install")
        with zipfile.ZipFile(dl) as z:
            z.extractall(LOCAL)
        dl.unlink()
    data = LOCAL / "pgdata"
    if not data.exists():
        pwfile = LOCAL / "pwfile.tmp"
        pwfile.write_text(PW)
        try:
            subprocess.run([str(bin_ / "initdb"), "-D", str(data), "-U", "localcrew", "--auth=scram-sha-256",
                            f"--pwfile={pwfile}", "-E", "UTF8", "--locale=C"], check=True, capture_output=True)
        finally:
            pwfile.unlink()
    st = subprocess.run([str(bin_ / "pg_ctl"), "-D", str(data), "status"], capture_output=True)
    if st.returncode != 0:
        (LOCAL / "logs").mkdir(parents=True, exist_ok=True)
        subprocess.run([str(bin_ / "pg_ctl"), "-D", str(data), "-l", str(LOCAL / "logs" / "postgres.log"),
                        "-o", "-p 54329 -c listen_addresses=127.0.0.1", "-w", "start"], check=True)
    env = {**os.environ, "PGPASSWORD": PW}
    for db in ("localcrew", "localcrew_test"):
        exists = subprocess.run([str(bin_ / "psql"), "-h", "127.0.0.1", "-p", "54329", "-U", "localcrew", "-d", "postgres",
                                 "-tAc", f"select 1 from pg_database where datname='{db}'"], env=env, capture_output=True, text=True)
        if exists.stdout.strip() != "1":
            subprocess.run([str(bin_ / "psql"), "-h", "127.0.0.1", "-p", "54329", "-U", "localcrew", "-d", "postgres",
                            "-c", f"create database {db}"], env=env, check=True)
    print("PostgreSQL 17.6 running on 127.0.0.1:54329 with databases localcrew, localcrew_test")


if __name__ == "__main__":
    main()
