"""Events, audit, and ownership checks shared by API and worker."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, Bot, Membership, TaskEvent
from .security import Redactor


def emit(db: Session, owner_user_id: str, type_: str, data: dict | None = None, task_id: str | None = None):
    db.add(TaskEvent(owner_user_id=owner_user_id, task_id=task_id, type=type_, data=Redactor.obj(data or {})))


def audit(db: Session, owner_user_id: str | None, actor: str, action: str, obj=None, **data):
    db.add(AuditEvent(owner_user_id=owner_user_id, actor=actor, action=action,
                      object_type=type(obj).__tablename__ if obj is not None else "",
                      object_id=getattr(obj, "id", "") or "", data=Redactor.obj(data)))


class NotFound(Exception):
    pass


def team_role(db: Session, user_id: str, team_id: str | None) -> str | None:
    if not team_id:
        return None
    return db.scalar(select(Membership.role).where(Membership.team_id == team_id, Membership.user_id == user_id))


def owned(db: Session, model, obj_id: str, user_id: str):
    """Fetch a personal object; anything not owned by the user is reported as not found (no existence leak)."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "owner_user_id", None) != user_id:
        raise NotFound(model.__tablename__)
    return obj


def bot_access(db: Session, bot_id: str, user_id: str, write: bool = False) -> Bot:
    """Personal bots: owner only. Team bots: members may use, owner/manager may edit."""
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise NotFound("bots")
    if bot.owner_user_id == user_id:
        return bot
    role = team_role(db, user_id, bot.team_id)
    if role and (not write or role in ("owner", "manager")):
        return bot
    raise NotFound("bots")
