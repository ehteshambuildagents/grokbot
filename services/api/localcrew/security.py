"""Passwords, sessions, secret encryption, redaction."""
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
from datetime import timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .models import AuthSession, SecretReference

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(h: str, pw: str) -> bool:
    try:
        return _ph.verify(h, pw)
    except VerificationError:
        return False


def sha256(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def canonical_digest(obj) -> str:
    return sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


# ---------- sessions ----------

def create_session(db: Session, user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    db.add(AuthSession(id=sha256(token), user_id=user_id, csrf=csrf,
                       expires_at=utcnow() + timedelta(hours=settings().session_hours)))
    db.commit()
    return token, csrf


def load_session(db: Session, token: str | None) -> AuthSession | None:
    if not token:
        return None
    s = db.get(AuthSession, sha256(token))
    if not s or s.expires_at < utcnow():
        return None
    return s


# ---------- secrets (key kept outside the DB) ----------
# Key file format: one Fernet key per line, "<key_id>:<key>"; first line is the active key.

def _load_keys() -> list[tuple[str, str]]:
    path = settings().secret_key_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"k{secrets.token_hex(4)}:{Fernet.generate_key().decode()}\n")
    return [tuple(line.split(":", 1)) for line in path.read_text().split() if ":" in line]


def _fernet() -> tuple[str, MultiFernet]:
    keys = _load_keys()
    return keys[0][0], MultiFernet([Fernet(k.encode()) for _, k in keys])


def store_secret(db: Session, owner_user_id: str, name: str, value: str) -> SecretReference:
    key_id, f = _fernet()
    ref = SecretReference(owner_user_id=owner_user_id, name=name, ciphertext=f.encrypt(value.encode()), key_id=key_id)
    db.add(ref)
    db.flush()
    return ref


def reveal_secret(db: Session, secret_id: str, owner_user_id: str) -> str:
    """Only the credential broker (connectors, webhook verification) may call this. Never the model."""
    ref = db.get(SecretReference, secret_id)
    if not ref or ref.owner_user_id != owner_user_id:
        raise PermissionError("secret not accessible")
    try:
        value = _fernet()[1].decrypt(ref.ciphertext).decode()
    except InvalidToken as e:
        raise PermissionError("secret key unavailable; restore secrets.key") from e
    Redactor.register(value)
    return value


def rotate_secret_keys(db: Session) -> int:
    """Prepend a new key, re-encrypt every secret, keep old keys until the operator removes them."""
    path = settings().secret_key_file
    old = path.read_text()
    new_id = f"k{secrets.token_hex(4)}"
    path.write_text(f"{new_id}:{Fernet.generate_key().decode()}\n{old}")
    _, f = _fernet()
    n = 0
    for ref in db.scalars(select(SecretReference)):
        ref.ciphertext = f.rotate(ref.ciphertext)
        ref.key_id, ref.rotated_at = new_id, utcnow()
        n += 1
    db.commit()
    return n


# ---------- redaction ----------

_PATTERNS = [
    re.compile(r"(?i)(authorization|x-api-key|api[_-]?key|token|password|passwd|secret)(\"?\s*[:=]\s*\"?)([^\s\"',;&]{4,})"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


class Redactor:
    _known: set[str] = set()

    @classmethod
    def register(cls, value: str):
        if value and len(value) >= 4:
            cls._known.add(value)

    @classmethod
    def text(cls, s: str) -> str:
        if not isinstance(s, str):
            return s
        for v in cls._known:
            s = s.replace(v, "[REDACTED]")
        for p in _PATTERNS:
            s = p.sub(lambda m: (m.group(1) + m.group(2) + "[REDACTED]") if m.lastindex and m.lastindex >= 3 else "[REDACTED]", s)
        return s

    @classmethod
    def obj(cls, o):
        if isinstance(o, str):
            return cls.text(o)
        if isinstance(o, dict):
            return {k: ("[REDACTED]" if re.search(r"(?i)password|secret|token|api_?key", str(k)) else cls.obj(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [cls.obj(v) for v in o]
        return o


def looks_like_credential(s: str) -> bool:
    return any(p.search(s) for p in _PATTERNS) or bool(re.search(r"(?i)\b(password|passcode|pin)\s+(is|=|:)\s*\S+", s))


# ---------- SSRF guard ----------

def check_url(url: str) -> str:
    """Return normalized URL or raise ValueError when it targets a disallowed address."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http(s) URLs are allowed")
    hostport = f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"
    allow = {h.strip() for h in settings().private_host_allowlist.split(",") if h.strip()}
    if hostport in allow:
        return url
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"blocked private/internal address {ip} for {u.hostname}")
    return url


def hmac_ok(secret: str, msg: bytes, sig: str) -> bool:
    return hmac.compare_digest(hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest(), sig or "")
