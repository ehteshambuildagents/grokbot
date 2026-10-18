import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import Memory
from .security import looks_like_credential


class MemoryRejected(ValueError):
    pass


def visible_filter(user_id: str, bot_id: str | None, team_ids: list[str] = ()):
    """Bot memories are private to that bot; user-scope memories are visible to all the user's bots;
    private notes are never injected into bot context."""
    conds = [(Memory.owner_user_id == user_id) & (Memory.scope == "user")]
    if bot_id:
        conds.append((Memory.owner_user_id == user_id) & (Memory.scope == "bot") & (Memory.bot_id == bot_id))
    if team_ids:
        conds.append((Memory.scope == "team") & Memory.team_id.in_(list(team_ids)))
    return or_(*conds)


def search(db: Session, user_id: str, bot_id: str | None, query: str, k: int = 5, team_ids=()) -> list[Memory]:
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", query.lower())][:20]
    if not words:
        return []
    tsq = func.to_tsquery("english", " | ".join(words))
    now = utcnow()
    return list(db.scalars(
        select(Memory)
        .where(visible_filter(user_id, bot_id, team_ids), Memory.enabled.is_(True),
               or_(Memory.expires_at.is_(None), Memory.expires_at > now), Memory.tsv.op("@@")(tsq))
        .order_by(func.ts_rank(Memory.tsv, tsq).desc(), Memory.updated_at.desc()).limit(k)))


def create(db: Session, user_id: str, content: str, scope: str, bot_id=None, team_id=None, source=None,
           confidence=0.7, expires_at=None) -> Memory:
    if looks_like_credential(content):
        raise MemoryRejected("memories may not contain credentials or secrets")
    m = Memory(owner_user_id=user_id, bot_id=bot_id if scope == "bot" else None, team_id=team_id, scope=scope,
               content=content.strip()[:4000], source=source or {"kind": "user"}, confidence=confidence,
               expires_at=expires_at)
    db.add(m)
    db.flush()
    return m


def revise(db: Session, m: Memory, content: str | None = None, **fields):
    if content is not None:
        if looks_like_credential(content):
            raise MemoryRejected("memories may not contain credentials or secrets")
        m.history = [*m.history, {"revision": m.revision, "content": m.content, "at": utcnow().isoformat()}]
        m.content = content
        m.revision += 1
    for k, v in fields.items():
        setattr(m, k, v)
    m.updated_at = utcnow()
