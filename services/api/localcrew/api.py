"""LocalCrew HTTP API: auth, bots, conversations, tasks, SSE. Other areas live in routes_*.py."""
import asyncio
import json
import time
from collections import defaultdict

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .common import NotFound, audit, bot_access, emit, owned
from .config import ROOT, settings
from .db import SessionLocal, get_db, utcnow
from .models import (
    Artifact,
    Bot,
    BotVersion,
    Conversation,
    Membership,
    Message,
    Participant,
    Routine,
    Task,
    TaskEvent,
    TaskStep,
    ToolCall,
    User,
)
from .security import create_session, hash_password, load_session, verify_password
from .tools import DEFAULT_TOOLS, TOOLS

COOKIE = "lc_session"
app = FastAPI(title="LocalCrew API", version="0.1.0")
_login_attempts: dict[str, list[float]] = defaultdict(list)


@app.exception_handler(NotFound)
async def _nf(request, exc):
    return JSONResponse({"detail": "not found"}, status_code=404)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
            and not request.url.path.startswith(("/api/hooks/", "/api/auth/login", "/api/setup")):
        origin = request.headers.get("origin")
        if origin and origin not in (settings().public_origin, f"{request.url.scheme}://{request.url.netloc}"):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        token = request.cookies.get(COOKIE)
        if token:
            def check():
                db = SessionLocal()()
                try:
                    s = load_session(db, token)
                    return s is not None and s.csrf == request.headers.get("x-csrf-token")
                finally:
                    db.close()
            if not await run_in_threadpool(check):
                return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
    return resp


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    s = load_session(db, request.cookies.get(COOKIE))
    if not s:
        raise HTTPException(401, "not signed in")
    return db.get(User, s.user_id)


# ---------------------------------------------------------------- auth / setup

class SetupIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = "Asia/Karachi"


SEED_BOTS = [
    {"name": "Atlas", "avatar": "🧭", "role": "Research analyst",
     "instructions": "Research questions carefully, cite the files and pages you used, and write clear reports."},
    {"name": "Quill", "avatar": "🪶", "role": "QA engineer",
     "instructions": "Reproduce defects step by step, capture evidence (screenshots, logs), and write precise bug "
                     "reports. Always ask before submitting anything to an external tracker.",
     "tools": DEFAULT_TOOLS + ["tracker_list_issues", "tracker_create_issue"]},
]


def make_bot(db, user_id, spec) -> Bot:
    bot = Bot(owner_user_id=user_id, name=spec["name"], avatar=spec.get("avatar", "🤖"), role=spec.get("role", ""),
              instructions=spec.get("instructions", ""), model=spec.get("model", ""),
              tools=spec.get("tools", list(DEFAULT_TOOLS)), policy_profile=spec.get("policy_profile", "standard"))
    db.add(bot)
    db.flush()
    db.add(BotVersion(bot_id=bot.id, version=1, config=bot_config(bot)))
    return bot


@app.get("/api/setup")
def setup_state(db: Session = Depends(get_db)):
    return {"needs_setup": db.scalar(select(func.count()).select_from(User)) == 0}


@app.post("/api/setup")
def setup(body: SetupIn, response: Response, db: Session = Depends(get_db)):
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "already set up")
    from zoneinfo import ZoneInfo
    ZoneInfo(body.timezone)
    u = User(email=body.email.lower(), password_hash=hash_password(body.password), display_name=body.display_name,
             timezone=body.timezone, is_admin=True)
    db.add(u)
    db.flush()
    for spec in SEED_BOTS:
        make_bot(db, u.id, spec)
    audit(db, u.id, f"user:{u.id}", "setup", u)
    db.commit()
    return _login_response(db, u, response)


class LoginIn(BaseModel):
    email: str
    password: str


def _login_response(db, u: User, response: Response):
    token, csrf = create_session(db, u.id)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=settings().secure_cookies,
                        max_age=settings().session_hours * 3600, path="/")
    return {"user": user_out(u), "csrf": csrf}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < 300]
    if len(_login_attempts[ip]) >= 10:
        raise HTTPException(429, "too many attempts; wait 5 minutes")
    u = db.scalar(select(User).where(User.email == body.email.lower()))
    if not u or not verify_password(u.password_hash, body.password):
        _login_attempts[ip].append(now)
        raise HTTPException(401, "invalid email or password")
    audit(db, u.id, f"user:{u.id}", "login", u, ip=ip)
    db.commit()
    return _login_response(db, u, response)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    s = load_session(db, request.cookies.get(COOKIE))
    if s:
        db.delete(s)
        db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


def user_out(u: User):
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "timezone": u.timezone, "is_admin": u.is_admin}


@app.get("/api/me")
def me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session(db, request.cookies.get(COOKIE))
    return {"user": user_out(user), "csrf": s.csrf}


class MeIn(BaseModel):
    display_name: str | None = Field(None, max_length=120)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def tz(cls, v):
        if v is not None:
            from zoneinfo import ZoneInfo
            ZoneInfo(v)
        return v


@app.patch("/api/me")
def update_me(body: MeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(user, k, v)
    db.commit()
    return user_out(user)


# ---------------------------------------------------------------- bots

def bot_config(b: Bot) -> dict:
    return {k: getattr(b, k) for k in ("name", "avatar", "role", "instructions", "model", "model_options", "tools",
                                       "policy_profile", "memory_enabled")}


def bot_out(b: Bot):
    return {"id": b.id, **bot_config(b), "pinned": b.pinned, "hidden": b.hidden, "version": b.version,
            "team_id": b.team_id, "owner_user_id": b.owner_user_id, "created_at": b.created_at, "updated_at": b.updated_at}


class BotIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    avatar: str = Field("🤖", max_length=16)
    role: str = Field("", max_length=200)
    instructions: str = Field("", max_length=20000)
    model: str = Field("", max_length=120)
    model_options: dict = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOLS))
    policy_profile: str = Field("standard", pattern="^(standard|cautious|read_only)$")
    memory_enabled: bool = True

    @field_validator("tools")
    @classmethod
    def known(cls, v):
        bad = [t for t in v if t not in TOOLS]
        if bad:
            raise ValueError(f"unknown tools: {bad}")
        return v

    @field_validator("model_options")
    @classmethod
    def opts(cls, v):
        allowed = {"temperature", "num_ctx", "top_p", "num_predict"}
        if set(v) - allowed:
            raise ValueError(f"allowed model options: {sorted(allowed)}")
        return v


class BotPatch(BotIn):
    name: str | None = Field(None, min_length=1, max_length=80)
    tools: list[str] | None = None
    pinned: bool | None = None
    hidden: bool | None = None
    model_options: dict | None = None


def team_ids(db, user_id):
    return list(db.scalars(select(Membership.team_id).where(Membership.user_id == user_id)))


@app.get("/api/bots")
def list_bots(include_hidden: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Bot).where(or_(Bot.owner_user_id == user.id, Bot.team_id.in_(team_ids(db, user.id))))
    if not include_hidden:
        q = q.where(Bot.hidden.is_(False))
    return [bot_out(b) for b in db.scalars(q.order_by(Bot.pinned.desc(), Bot.name))]


@app.get("/api/tools")
def list_tools(user: User = Depends(current_user)):
    return [{"name": t.name, "description": t.description, "effect": t.effect, "default": t.name in DEFAULT_TOOLS}
            for t in TOOLS.values()]


@app.post("/api/bots", status_code=201)
def create_bot(body: BotIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = make_bot(db, user.id, body.model_dump())
    audit(db, user.id, f"user:{user.id}", "bot.create", b)
    db.commit()
    return bot_out(b)


@app.get("/api/bots/{bot_id}")
def get_bot(bot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return bot_out(bot_access(db, bot_id, user.id))


@app.patch("/api/bots/{bot_id}")
def update_bot(bot_id: str, body: BotPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = bot_access(db, bot_id, user.id, write=True)
    data = body.model_dump(exclude_unset=True)
    config_changed = any(k in data and data[k] != getattr(b, k) for k in bot_config(b))
    for k, v in data.items():
        if v is not None:
            setattr(b, k, v)
    if config_changed:
        b.version += 1
        db.add(BotVersion(bot_id=b.id, version=b.version, config=bot_config(b)))
    audit(db, user.id, f"user:{user.id}", "bot.update", b, fields=list(data))
    db.commit()
    return bot_out(b)


@app.get("/api/bots/{bot_id}/versions")
def bot_versions(bot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    bot_access(db, bot_id, user.id)
    return [{"version": v.version, "config": v.config, "created_at": v.created_at}
            for v in db.scalars(select(BotVersion).where(BotVersion.bot_id == bot_id).order_by(BotVersion.version.desc()))]


@app.post("/api/bots/{bot_id}/duplicate", status_code=201)
def duplicate_bot(bot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Copies configuration only: no conversations, memories, credentials or routines."""
    src = bot_access(db, bot_id, user.id)
    spec = bot_config(src) | {"name": f"{src.name} (copy)"[:80]}
    b = make_bot(db, user.id, spec)
    audit(db, user.id, f"user:{user.id}", "bot.duplicate", b, source=src.id)
    db.commit()
    return bot_out(b)


@app.get("/api/bots/{bot_id}/deletion-impact")
def deletion_impact(bot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = bot_access(db, bot_id, user.id, write=True)
    count = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w))  # noqa: E731
    from .models import Memory
    return {
        "routines_deleted": count(Routine, Routine.bot_id == b.id),
        "active_tasks_cancelled": count(Task, Task.bot_id == b.id, Task.status.not_in(["succeeded", "failed", "cancelled"])),
        "tasks_and_history_deleted": count(Task, Task.bot_id == b.id),
        "bot_memories_deleted": count(Memory, Memory.bot_id == b.id),
        "messages_kept_with_author_removed": count(Message, Message.author_bot_id == b.id),
        "artifacts_kept": count(Artifact, Artifact.bot_id == b.id),
        "shared_workspace_files": "kept (workspace belongs to you, not the bot)",
        "browser_sign_ins": "kept (shared by all your bots)",
    }


@app.delete("/api/bots/{bot_id}")
def delete_bot(bot_id: str, confirm: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not confirm:
        raise HTTPException(400, "pass confirm=true after reviewing /deletion-impact")
    b = bot_access(db, bot_id, user.id, write=True)
    impact = deletion_impact(bot_id, user, db)
    from .models import Memory
    for t in db.scalars(select(Task).where(Task.bot_id == b.id)):
        t.cancel_requested = True
    db.query(Memory).filter(Memory.bot_id == b.id).delete()
    db.query(Routine).filter(Routine.bot_id == b.id).delete()
    db.query(Task).filter(Task.bot_id == b.id).delete()
    db.delete(b)
    audit(db, user.id, f"user:{user.id}", "bot.delete", None, bot_id=bot_id, impact=impact)
    db.commit()
    return {"deleted": True, "impact": impact}


# ---------------------------------------------------------------- templates (sanitized)

@app.get("/api/bots/{bot_id}/template")
def export_template(bot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = bot_access(db, bot_id, user.id)
    return {"localcrew_template": 1, "bot": bot_config(b)}  # no history, memories, sessions or secrets


class TemplateIn(BaseModel):
    localcrew_template: int
    bot: BotIn


@app.post("/api/bots/import", status_code=201)
def import_template(body: TemplateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = make_bot(db, user.id, body.bot.model_dump())
    db.commit()
    return bot_out(b)


# ---------------------------------------------------------------- conversations

def conv_out(db, c: Conversation):
    bots = db.scalars(select(Participant.bot_id).where(Participant.conversation_id == c.id, Participant.bot_id.is_not(None))).all()
    return {"id": c.id, "kind": c.kind, "title": c.title, "bot_ids": list(bots), "created_at": c.created_at}


class ConvIn(BaseModel):
    bot_ids: list[str] = Field(min_length=1, max_length=8)
    title: str = Field("", max_length=200)


@app.get("/api/conversations")
def list_conversations(kind: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Conversation).where(Conversation.owner_user_id == user.id)
    if kind:
        q = q.where(Conversation.kind == kind)
    return [conv_out(db, c) for c in db.scalars(q.order_by(Conversation.created_at.desc()))]


@app.post("/api/conversations", status_code=201)
def create_conversation(body: ConvIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for bid in body.bot_ids:
        bot_access(db, bid, user.id)
    c = Conversation(owner_user_id=user.id, kind="group" if len(body.bot_ids) > 1 else "direct",
                     title=body.title or "Group")
    db.add(c)
    db.flush()
    for bid in dict.fromkeys(body.bot_ids):
        db.add(Participant(conversation_id=c.id, bot_id=bid))
    db.add(Participant(conversation_id=c.id, user_id=user.id))
    db.commit()
    return conv_out(db, c)


@app.get("/api/bots/{bot_id}/conversation")
def direct_conversation(bot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Each person has a private direct conversation with each bot (also for shared team bots)."""
    b = bot_access(db, bot_id, user.id)
    c = db.scalar(select(Conversation).join(Participant, Participant.conversation_id == Conversation.id)
                  .where(Conversation.owner_user_id == user.id, Conversation.kind == "direct",
                         Participant.bot_id == b.id, Conversation.title == ""))
    if c is None:
        c = Conversation(owner_user_id=user.id, kind="direct", title="")
        db.add(c)
        db.flush()
        db.add(Participant(conversation_id=c.id, bot_id=b.id))
        db.add(Participant(conversation_id=c.id, user_id=user.id))
        db.commit()
    return conv_out(db, c)


def msg_out(m: Message):
    return {"id": m.id, "content": m.content, "author_user_id": m.author_user_id, "author_bot_id": m.author_bot_id,
            "mentions": m.mentions, "attachments": m.attachments, "task_id": m.task_id, "created_at": m.created_at}


@app.get("/api/conversations/{conv_id}/messages")
def list_messages(conv_id: str, before: str | None = None, limit: int = 100, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    owned(db, Conversation, conv_id, user.id)
    q = select(Message).where(Message.conversation_id == conv_id)
    if before:
        ref = db.get(Message, before)
        if ref:
            q = q.where(Message.created_at < ref.created_at)
    rows = db.scalars(q.order_by(Message.created_at.desc()).limit(min(limit, 500))).all()
    return [msg_out(m) for m in reversed(rows)]


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    attachments: list[str] = Field(default_factory=list, max_length=10, description="workspace paths")
    skill_id: str | None = None
    deliverable: str | None = Field(None, pattern=r"^[\w\-. ]{1,120}\.(md|txt|csv|json)$")
    model: str | None = None


@app.post("/api/conversations/{conv_id}/messages", status_code=201)
def post_message(conv_id: str, body: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Post a user message. Direct conversation → its bot gets a task. Group → each @mentioned bot gets a
    task (no mention → the first bot). Tasks run in the worker, independent of this client."""
    from . import workspace
    from .models import Skill, SkillVersion
    c = owned(db, Conversation, conv_id, user.id)
    bots = db.scalars(select(Bot).join(Participant, Participant.bot_id == Bot.id)
                      .where(Participant.conversation_id == c.id)).all()
    for p in body.attachments:
        workspace.resolve(user.id, p, must_exist=True)
    mentioned = [b for b in bots if f"@{b.name.lower()}" in body.content.lower()]
    targets = mentioned or bots[:1]
    msg = Message(conversation_id=c.id, owner_user_id=user.id, author_user_id=user.id, content=body.content,
                  mentions=[b.id for b in mentioned], attachments=[{"path": p} for p in body.attachments])
    db.add(msg)
    skill_version_id = None
    if body.skill_id:
        sk = owned(db, Skill, body.skill_id, user.id)
        sv = db.scalar(select(SkillVersion).where(SkillVersion.skill_id == sk.id, SkillVersion.status == "approved")
                       .order_by(SkillVersion.version.desc()))
        if not sv:
            raise HTTPException(400, "skill has no approved version")
        skill_version_id = sv.id
    history = db.scalars(select(Message).where(Message.conversation_id == c.id)
                         .order_by(Message.created_at.desc()).limit(8)).all()
    context = "\n".join(f"{'User' if m.author_user_id else 'Bot'}: {m.content[:800]}" for m in reversed(history))
    tasks = []
    for b in targets:
        objective = body.content
        if context:
            objective = f"Recent conversation (for context):\n{context}\n\nCurrent request:\n{body.content}"
        if body.attachments:
            objective += "\n\nAttached workspace files: " + ", ".join(body.attachments)
        t = Task(owner_user_id=user.id, bot_id=b.id, conversation_id=c.id, objective=objective,
                 deliverable={"name": body.deliverable} if body.deliverable else {}, skill_version_id=skill_version_id,
                 tool_budget=settings().default_tool_budget)
        db.add(t)
        db.flush()
        emit(db, user.id, "status", {"status": "queued"}, t.id)
        tasks.append(t.id)
    msg.task_id = tasks[0] if tasks else None
    db.commit()
    return {"message": msg_out(msg), "task_ids": tasks}


# ---------------------------------------------------------------- tasks

def task_out(t: Task, full=False):
    d = {"id": t.id, "bot_id": t.bot_id, "conversation_id": t.conversation_id, "parent_task_id": t.parent_task_id,
         "status": t.status, "phase": t.phase, "progress": t.progress, "blocker": t.blocker, "error": t.error,
         "objective": t.objective[-500:], "attempts": t.attempts, "tool_calls_used": t.tool_calls_used,
         "tool_budget": t.tool_budget, "depth": t.depth, "handoff": t.handoff, "routine_run_id": t.routine_run_id,
         "created_at": t.created_at, "updated_at": t.updated_at, "finished_at": t.finished_at,
         "cancel_requested": t.cancel_requested}
    if full:
        d |= {"plan": t.plan, "result": t.result, "deliverable": t.deliverable, "redirects": t.redirects}
    return d


@app.get("/api/tasks")
def list_tasks(status: str | None = None, bot_id: str | None = None, limit: int = 100,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Task).where(Task.owner_user_id == user.id)
    if status:
        q = q.where(Task.status.in_(status.split(",")))
    if bot_id:
        q = q.where(Task.bot_id == bot_id)
    return [task_out(t) for t in db.scalars(q.order_by(Task.created_at.desc()).limit(min(limit, 500)))]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = owned(db, Task, task_id, user.id)
    steps = db.scalars(select(TaskStep).where(TaskStep.task_id == t.id).order_by(TaskStep.started_at, TaskStep.idx)).all()
    calls = db.scalars(select(ToolCall).where(ToolCall.task_id == t.id).order_by(ToolCall.seq)).all()
    arts = db.scalars(select(Artifact).where(Artifact.task_id == t.id)).all()
    children = db.scalars(select(Task).where(Task.parent_task_id == t.id)).all()
    return task_out(t, full=True) | {
        "steps": [{"idx": s.idx, "kind": s.kind, "status": s.status, "summary": s.summary, "data": s.data,
                   "at": s.started_at} for s in steps],
        "tool_calls": [{"id": c.id, "seq": c.seq, "tool": c.tool, "args": c.args, "status": c.status, "effect": c.effect,
                        "result": c.result, "idempotency_key": c.idempotency_key} for c in calls],
        "artifacts": [artifact_out(a) for a in arts],
        "children": [task_out(c) for c in children]}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id).with_for_update())
    if t is None:
        raise NotFound("tasks")
    if t.status in ("succeeded", "failed", "cancelled"):
        return task_out(t)
    t.cancel_requested = True
    if t.status in ("queued", "retry_wait", "waiting_for_user", "waiting_for_approval"):
        from .engine import _finish
        _finish(db, t, "cancelled", "cancelled by user")
    else:
        emit(db, user.id, "cancel_requested", {}, t.id)
    db.commit()
    return task_out(t)


class RedirectIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/tasks/{task_id}/redirect")
def redirect_task(task_id: str, body: RedirectIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id).with_for_update())
    if t is None:
        raise NotFound("tasks")
    if t.status in ("succeeded", "failed", "cancelled"):
        raise HTTPException(409, "task already finished")
    t.redirects = [*(t.redirects or []), {"text": body.text, "at": utcnow().isoformat()}]
    emit(db, user.id, "redirect", {"text": body.text}, t.id)
    if t.status == "waiting_for_user":
        t.status, t.blocker = "queued", {}
    db.commit()
    return task_out(t)


class ResumeIn(BaseModel):
    resolution: str | None = Field(None, pattern="^(assume_done|retry|skip)$")


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str, body: ResumeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id).with_for_update())
    if t is None:
        raise NotFound("tasks")
    if t.status not in ("waiting_for_user", "failed"):
        raise HTTPException(409, f"task is {t.status}")
    tcid = (t.blocker or {}).get("tool_call_id")
    if tcid:
        tc = db.get(ToolCall, tcid)
        if tc and tc.status in ("ambiguous", "blocked"):
            if tc.status == "ambiguous" and not body.resolution:
                raise HTTPException(400, "this task has an ambiguous action: choose assume_done, retry or skip")
            tc.status = {"assume_done": "succeeded", "skip": "skipped", "retry": "retry"}.get(body.resolution, "retry")
            if tc.status != "retry":
                tc.result = {"note": f"resolved by user: {body.resolution}"}
            audit(db, user.id, f"user:{user.id}", "toolcall.resolve", tc, resolution=body.resolution)
    if t.status == "failed":
        t.attempts = 0
    t.status, t.blocker, t.error, t.next_attempt_at = "queued", {}, "", utcnow()
    emit(db, user.id, "status", {"status": "queued", "resumed": True}, t.id)
    db.commit()
    return task_out(t)


# ---------------------------------------------------------------- SSE

def _events_after(user_id: str, after: int, task_id: str | None):
    db = SessionLocal()()
    try:
        q = select(TaskEvent).where(TaskEvent.owner_user_id == user_id, TaskEvent.id > after)
        if task_id:
            q = q.where(TaskEvent.task_id == task_id)
        rows = db.scalars(q.order_by(TaskEvent.id).limit(500)).all()
        return [(e.id, e.task_id, e.type, e.data, e.created_at.isoformat()) for e in rows]
    finally:
        db.close()


async def _sse(request: Request, user_id: str, task_id: str | None, after: int):
    last = after
    idle = 0
    yield "retry: 2000\n\n"
    while not await request.is_disconnected():
        rows = await run_in_threadpool(_events_after, user_id, last, task_id)
        for eid, tid, typ, data, at in rows:
            last = eid
            yield f"id: {eid}\nevent: {typ}\ndata: {json.dumps({'task_id': tid, 'type': typ, 'data': data, 'at': at})}\n\n"
        idle = 0 if rows else idle + 1
        if idle and idle % 30 == 0:
            yield ": keepalive\n\n"
        await asyncio.sleep(0.5)


def _short_session_user(request: Request) -> User:
    """Auth for long-lived streams: must not hold a pooled DB session for the stream's lifetime (a Depends(get_db)
    session stays open, idle in transaction, until the streaming response ends)."""
    with SessionLocal()() as db:
        s = load_session(db, request.cookies.get(COOKIE))
        if not s:
            raise HTTPException(401, "not signed in")
        return db.get(User, s.user_id)


@app.get("/api/events")
def stream_all(request: Request, after: int = 0, user: User = Depends(_short_session_user)):
    """after=-1 starts at the newest event; reconnects resume from Last-Event-ID (replay)."""
    after = int(request.headers.get("last-event-id") or after)
    if after < 0:
        with SessionLocal()() as db:
            after = db.scalar(select(func.max(TaskEvent.id)).where(TaskEvent.owner_user_id == user.id)) or 0
    return StreamingResponse(_sse(request, user.id, None, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/tasks/{task_id}/events")
def stream_task(task_id: str, request: Request, after: int = 0, user: User = Depends(_short_session_user)):
    with SessionLocal()() as db:
        owned(db, Task, task_id, user.id)
    after = int(request.headers.get("last-event-id") or after)
    return StreamingResponse(_sse(request, user.id, task_id, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/tasks/{task_id}/events.json")
def task_events_json(task_id: str, after: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    owned(db, Task, task_id, user.id)
    return [{"id": e[0], "type": e[2], "data": e[3], "at": e[4]} for e in _events_after(user.id, after, task_id)]


# ---------------------------------------------------------------- artifacts

def artifact_out(a: Artifact):
    return {"id": a.id, "name": a.name, "mime": a.mime, "size": a.size, "sha256": a.sha256, "version": a.version,
            "previous_id": a.previous_id, "task_id": a.task_id, "bot_id": a.bot_id, "provenance": a.provenance,
            "created_at": a.created_at, "url": f"/api/artifacts/{a.id}/download"}


@app.get("/api/artifacts")
def list_artifacts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [artifact_out(a) for a in db.scalars(select(Artifact).where(Artifact.owner_user_id == user.id)
                                                 .order_by(Artifact.created_at.desc()).limit(500))]


@app.get("/api/artifacts/{art_id}/download")
def download_artifact(art_id: str, inline: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = owned(db, Artifact, art_id, user.id)
    path = (settings().artifacts / a.storage_path).resolve()
    if settings().artifacts.resolve() not in path.parents or not path.exists():
        raise NotFound("artifacts")
    safe_inline = inline and a.mime in ("image/png", "image/jpeg", "text/plain", "application/pdf")
    return FileResponse(path, media_type=a.mime if safe_inline else "application/octet-stream", filename=a.name,
                        content_disposition_type="inline" if safe_inline else "attachment",
                        headers={"Content-Security-Policy": "sandbox; default-src 'none'"})


# ---------------------------------------------------------------- other routers + static web app

from . import routes_more  # noqa: E402

app.include_router(routes_more.router)

_dist = ROOT / "apps" / "web" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (_dist / path).resolve()
        if path and _dist.resolve() in f.parents and f.is_file():
            return FileResponse(f)
        return FileResponse(_dist / "index.html")


def run():
    import uvicorn
    uvicorn.run("localcrew.api:app", host=settings().bind_host, port=int(__import__("os").environ.get("PORT", "8000")))


if __name__ == "__main__":
    run()
