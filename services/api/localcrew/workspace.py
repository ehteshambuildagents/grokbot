"""Per-user shared workspace (all of a user's bots share it). Not a privacy boundary between people:
each user gets a separate directory and the API checks ownership before touching it."""
import mimetypes
import shutil
from pathlib import Path, PurePosixPath

from .config import settings
from .db import utcnow

VERSIONS = ".versions"
MAX_READ = 200_000


class PathError(ValueError):
    pass


def root(user_id: str) -> Path:
    r = settings().workspaces / user_id
    r.mkdir(parents=True, exist_ok=True)
    return r.resolve()


def resolve(user_id: str, rel: str, must_exist: bool = False) -> Path:
    rel = (rel or "").replace("\\", "/").strip()
    p = PurePosixPath(rel)
    if p.is_absolute() or ":" in rel or any(part in ("..",) for part in p.parts) or VERSIONS in p.parts:
        raise PathError(f"path not allowed: {rel!r}")
    base = root(user_id)
    cur = base
    for part in p.parts:
        if part in ("", "."):
            continue
        cur = cur / part
        if cur.is_symlink():
            raise PathError(f"symlinks are not allowed: {rel!r}")
    final = cur.resolve()
    if final != base and base not in final.parents:
        raise PathError(f"path escapes workspace: {rel!r}")
    if must_exist and not final.exists():
        raise PathError(f"not found: {rel!r}")
    return final


def rel_of(user_id: str, p: Path) -> str:
    return p.relative_to(root(user_id)).as_posix()


def list_dir(user_id: str, rel: str = "") -> list[dict]:
    d = resolve(user_id, rel, must_exist=True)
    out = []
    for c in sorted(d.iterdir()):
        if c.name == VERSIONS or c.is_symlink():
            continue
        st = c.stat()
        out.append({"path": rel_of(user_id, c), "name": c.name, "dir": c.is_dir(), "size": st.st_size,
                    "modified": st.st_mtime})
    return out


def read_text(user_id: str, rel: str) -> str:
    p = resolve(user_id, rel, must_exist=True)
    if p.is_dir():
        raise PathError("is a directory")
    data = p.read_bytes()[:MAX_READ]
    return data.decode("utf-8", errors="replace")


def backup(user_id: str, p: Path) -> str | None:
    if not p.exists() or p.is_dir():
        return None
    vdir = root(user_id) / VERSIONS
    vdir.mkdir(exist_ok=True)
    dest = vdir / f"{rel_of(user_id, p).replace('/', '__')}.{utcnow().strftime('%Y%m%dT%H%M%S%f')}"
    shutil.copy2(p, dest)
    return dest.name


def write(user_id: str, rel: str, data: bytes | str) -> dict:
    p = resolve(user_id, rel)
    if p.is_dir():
        raise PathError("is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = backup(user_id, p)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return {"path": rel_of(user_id, p), "size": p.stat().st_size, "previous_version": prev}


def move(user_id: str, src: str, dst: str) -> dict:
    a, b = resolve(user_id, src, must_exist=True), resolve(user_id, dst)
    prev = backup(user_id, b)
    b.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(a, b)
    return {"from": src, "to": rel_of(user_id, b), "previous_version": prev}


def search(user_id: str, query: str, limit: int = 50) -> list[dict]:
    base, q, hits = root(user_id), query.lower(), []
    for p in base.rglob("*"):
        if VERSIONS in p.parts or p.is_symlink() or not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        rel = rel_of(user_id, p)
        if q in rel.lower():
            hits.append({"path": rel, "line": 0, "text": "(name match)"})
            continue
        try:
            for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                if q in line.lower():
                    hits.append({"path": rel, "line": i, "text": line[:200]})
                    break
        except (UnicodeDecodeError, OSError):
            pass
        if len(hits) >= limit:
            break
    return hits


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
