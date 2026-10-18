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

import httpcore
import httpx
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
# Destinations must be globally routable unless "host:port" is explicitly allowlisted (test fixtures).
# DNS-rebinding defence: resolve once, validate, and connect to exactly the validated address
# (pinned httpx backend below; the computer service routes Chromium through an equivalent pinning proxy).

def _allowlist() -> set[str]:
    return {h.strip().lower() for h in settings().private_host_allowlist.split(",") if h.strip()}


def _blocked(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def resolve_checked(host: str, port: int) -> list[str]:
    """Resolve once; return addresses to connect to, or raise ValueError if any address is internal."""
    host = host.strip("[]").lower()
    if f"{host}:{port}" in _allowlist():
        return [host]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {host}") from e
    ips = list(dict.fromkeys(info[4][0] for info in infos))
    for ip in ips:
        if _blocked(ipaddress.ip_address(ip.split("%")[0])):
            raise ValueError(f"blocked private/internal address {ip} for {host}")
    return ips


def check_url(url: str) -> str:
    """Return the URL or raise ValueError when it targets a disallowed scheme or address."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http(s) URLs are allowed")
    resolve_checked(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    return url


class _PinnedBackend(httpcore.SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            ips = resolve_checked(host, port)
        except ValueError as e:
            raise httpcore.ConnectError(str(e)) from e
        # TLS server name / certificate checks still use the original host name (httpcore passes it separately).
        return super().connect_tcp(ips[0], port, timeout, local_address, socket_options)


def safe_http_client(**kw) -> httpx.Client:
    """httpx client that can only reach validated addresses, re-checked on every connection (incl. redirects)."""
    transport = httpx.HTTPTransport()
    transport._pool._network_backend = _PinnedBackend()  # httpcore 1.0.x; covered by test_dns_rebinding_*
    return httpx.Client(transport=transport, **kw)


def hmac_ok(secret: str, msg: bytes, sig: str) -> bool:
    return hmac.compare_digest(hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest(), sig or "")
