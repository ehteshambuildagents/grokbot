"""API areas: files, approvals, policy, memory, skills, routines, webhooks, connections, computer, teams,
models, diagnostics, audit."""
import base64
import io
import json
import secrets
import time
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from . import memory as mem
from . import routines as rt
from . import workspace
from .api import bot_access, current_user, team_ids
from .common import NotFound, audit, emit, owned, team_role
from .config import settings
from .connectors import OPTIONAL, REGISTRY, describe
from .db import get_db, utcnow
from .llm import OllamaProvider
from .models import (
    ApprovalRequest, AuditEvent, Bot, BrowserSession, Connection, ExecutionLease, Membership, Memory, PolicyRule,
    Routine, RoutineRun, SecretReference, Skill, SkillVersion, Task, Team, ToolCall, User,
)
from .security import hmac_ok, reveal_secret, store_secret

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- files

ALLOWED_UPLOADS = {".txt": None, ".md": None, ".csv": None, ".json": None, ".png": b"\x89PNG\r\n\x1a\n",
                   ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".gif": b"GIF8", ".webp": b"RIFF", ".pdf": b"%PDF-"}


def validate_upload(name: str, data: bytes):
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_UPLOADS:
        raise HTTPException(415, f"unsupported file type {ext or '(none)'}; allowed: {', '.join(ALLOWED_UPLOADS)}")
    if len(data) > settings().max_upload_mb * 1024 * 1024:
        raise HTTPException(413, "file too large")
    magic = ALLOWED_UPLOADS[ext]
    if magic and not data.startswith(magic):
        raise HTTPException(415, f"content does not match {ext}")
    if data[:4] in (b"PK\x03\x04", b"MZ\x90\x00") or data[:2] == b"MZ" or data[:3] == b"\x1f\x8b\x08":
        raise HTTPException(415, "archives and executables are rejected")
    if magic is None:
        try:
            s = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(415, "text files must be UTF-8") from e
        if ext == ".json":
            try:
                json.loads(s)
            except json.JSONDecodeError as e:
                raise HTTPException(415, f"invalid JSON: {e}") from e
    if ext == ".pdf":
        from pypdf import PdfReader
        try:
            r = PdfReader(io.BytesIO(data))
            if r.is_encrypted:
                raise HTTPException(415, "encrypted PDFs are rejected")
            if len(r.pages) > 500:
                raise HTTPException(415, "PDF has too many pages")
            if "/JavaScript" in str(r.trailer.get("/Root", {})) or "/OpenAction" in str(r.trailer.get("/Root", {})):
                raise HTTPException(415, "PDFs with embedded actions/JavaScript are rejected")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — any parse failure means reject
            raise HTTPException(415, f"unreadable PDF: {type(e).__name__}") from e


def _ws(fn, *a):
    try:
        return fn(*a)
    except workspace.PathError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/files")
def files_list(path: str = "", user: User = Depends(current_user)):
    return _ws(workspace.list_dir, user.id, path)


@router.get("/files/content")
def files_content(path: str, user: User = Depends(current_user)):
    return {"path": path, "content": _ws(workspace.read_text, user.id, path)}


@router.get("/files/download")
def files_download(path: str, user: User = Depends(current_user)):
    p = _ws(workspace.resolve, user.id, path, True)
    if p.is_dir():
        raise HTTPException(400, "is a directory")
    return FileResponse(p, media_type="application/octet-stream", filename=p.name,
                        headers={"Content-Security-Policy": "sandbox; default-src 'none'"})


@router.get("/files/search")
def files_search(q: str, user: User = Depends(current_user)):
    return workspace.search(user.id, q)


@router.post("/files/upload", status_code=201)
async def files_upload(dir: str = "uploads", file: UploadFile = File(...), user: User = Depends(current_user)):
    data = await file.read(settings().max_upload_mb * 1024 * 1024 + 1)
    name = (file.filename or "upload").replace("\\", "/").split("/")[-1]
    validate_upload(name, data)
    return _ws(workspace.write, user.id, f"{dir.strip('/')}/{name}" if dir else name, data)


class FileWrite(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=2_000_000)


@router.put("/files")
def files_write(body: FileWrite, user: User = Depends(current_user)):
    return _ws(workspace.write, user.id, body.path, body.content)


class FileMove(BaseModel):
    src: str
    dst: str


@router.post("/files/move")
def files_move(body: FileMove, user: User = Depends(current_user)):
    return _ws(workspace.move, user.id, body.src, body.dst)


@router.delete("/files")
def files_delete(path: str, user: User = Depends(current_user)):
    p = _ws(workspace.resolve, user.id, path, True)
    if p.is_dir():
        raise HTTPException(400, "directory deletion is not supported")
    kept = workspace.backup(user.id, p)
    p.unlink()
    return {"deleted": path, "recoverable_copy": kept}


# ---------------------------------------------------------------- approvals & policy

def approval_out(a: ApprovalRequest, db):
    bot = db.get(Bot, a.bot_id)
    return {"id": a.id, "task_id": a.task_id, "tool_call_id": a.tool_call_id, "status": a.status,
            "acting_identity": {"bot_id": a.bot_id, "bot_name": bot.name if bot else None, "on_behalf_of": a.owner_user_id},
            "operation": a.tool, "target": a.target, "payload": a.payload, "expected_effect": a.expected_effect,
            "digest": a.digest, "scope": a.decision_scope, "expires_at": a.expires_at, "created_at": a.created_at,
            "decided_at": a.decided_at}


@router.get("/approvals")
def approvals(status: str = "pending", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(ApprovalRequest).where(ApprovalRequest.owner_user_id == user.id)
    if status != "all":
        q = q.where(ApprovalRequest.status == status)
    return [approval_out(a, db) for a in db.scalars(q.order_by(ApprovalRequest.created_at.desc()).limit(200))]


class Decide(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")
    scope: str = Field("once", pattern="^(once|rule)$")
    digest: str = Field(min_length=64, max_length=64, description="digest shown on the card; must still match")


@router.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: Decide, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id, ApprovalRequest.owner_user_id == user.id)
                  .with_for_update())
    if a is None:
        raise NotFound("approvals")
    if a.status != "pending":
        raise HTTPException(409, f"approval is {a.status}")
    if a.expires_at <= utcnow():
        a.status = "expired"
        db.commit()
        raise HTTPException(409, "approval expired")
    if body.digest != a.digest:
        raise HTTPException(409, "the action changed since this card was shown; review it again")
    a.status = "approved" if body.decision == "approve" else "denied"
    a.decision_scope, a.decided_at = body.scope, utcnow()
    if body.decision == "approve" and body.scope == "rule":
        db.add(PolicyRule(level="user", owner_user_id=user.id, bot_id=a.bot_id, tool=a.tool, target=a.target,
                          decision="allow", note=f"from approval {a.id}"))
    t = db.get(Task, a.task_id)
    if t and t.status == "waiting_for_approval":
        t.status, t.blocker, t.next_attempt_at = "queued", {}, utcnow()
        emit(db, user.id, "status", {"status": "queued", "approval": a.status}, t.id)
    audit(db, user.id, f"user:{user.id}", f"approval.{a.status}", a, scope=body.scope, digest=a.digest)
    db.commit()
    return approval_out(a, db)


class RuleIn(BaseModel):
    tool: str = Field(max_length=80)
    target: str = Field("*", max_length=500)
    decision: str = Field(pattern="^(allow|deny|require_approval)$")
    bot_id: str | None = None
    level: str = Field("user", pattern="^(user|org)$")
    note: str = ""
    expires_in_days: int | None = Field(None, ge=1, le=365)


@router.get("/policy-rules")
def rules(user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(PolicyRule).where((PolicyRule.owner_user_id == user.id) | (PolicyRule.level == "org"))
    return [{"id": r.id, "level": r.level, "tool": r.tool, "target": r.target, "decision": r.decision, "bot_id": r.bot_id,
             "note": r.note, "expires_at": r.expires_at} for r in db.scalars(q)]


@router.post("/policy-rules", status_code=201)
def add_rule(body: RuleIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.level == "org" and not user.is_admin:
        raise HTTPException(403, "only administrators set organization rules")
    if body.bot_id:
        bot_access(db, body.bot_id, user.id, write=True)
    r = PolicyRule(level=body.level, owner_user_id=user.id if body.level == "user" else None, bot_id=body.bot_id,
                   tool=body.tool, target=body.target, decision=body.decision, note=body.note,
                   expires_at=utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None)
    db.add(r)
    audit(db, user.id, f"user:{user.id}", "policy.add", r, rule=body.model_dump())
    db.commit()
    return {"id": r.id}


@router.delete("/policy-rules/{rule_id}")
def del_rule(rule_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(PolicyRule, rule_id)
    if r is None or (r.level == "org" and not user.is_admin) or (r.level != "org" and r.owner_user_id != user.id):
        raise NotFound("policy_rules")
    db.delete(r)
    audit(db, user.id, f"user:{user.id}", "policy.delete", None, rule_id=rule_id)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------- memory

def memory_out(m: Memory):
    return {"id": m.id, "scope": m.scope, "bot_id": m.bot_id, "team_id": m.team_id, "content": m.content,
            "source": m.source, "confidence": m.confidence, "expires_at": m.expires_at, "enabled": m.enabled,
            "revision": m.revision, "history": m.history, "created_at": m.created_at, "updated_at": m.updated_at}


@router.get("/memories")
def memories(bot_id: str | None = None, q: str | None = None, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    if q:
        return [memory_out(m) for m in mem.search(db, user.id, bot_id, q, k=50, team_ids=team_ids(db, user.id))]
    query = select(Memory).where((Memory.owner_user_id == user.id) |
                                 ((Memory.scope == "team") & Memory.team_id.in_(team_ids(db, user.id))))
    if bot_id:
        query = query.where(Memory.bot_id == bot_id)
    return [memory_out(m) for m in db.scalars(query.order_by(Memory.updated_at.desc()).limit(500))]


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    scope: str = Field("user", pattern="^(user|bot|team|private_note)$")
    bot_id: str | None = None
    team_id: str | None = None
    confidence: float = Field(0.9, ge=0, le=1)
    expires_at: datetime | None = None


@router.post("/memories", status_code=201)
def add_memory(body: MemoryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.scope == "bot":
        if not body.bot_id:
            raise HTTPException(400, "bot_id required for bot scope")
        bot_access(db, body.bot_id, user.id)
    if body.scope == "team" and team_role(db, user.id, body.team_id) not in ("owner", "manager"):
        raise HTTPException(403, "only team owners/managers write team memory")
    try:
        m = mem.create(db, user.id, body.content, body.scope, bot_id=body.bot_id, team_id=body.team_id,
                       confidence=body.confidence, expires_at=body.expires_at, source={"kind": "user", "id": user.id})
    except mem.MemoryRejected as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return memory_out(m)


class MemoryPatch(BaseModel):
    content: str | None = Field(None, min_length=1, max_length=4000)
    enabled: bool | None = None
    confidence: float | None = Field(None, ge=0, le=1)
    expires_at: datetime | None = None


@router.patch("/memories/{mem_id}")
def patch_memory(mem_id: str, body: MemoryPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = owned(db, Memory, mem_id, user.id)
    data = body.model_dump(exclude_unset=True)
    try:
        mem.revise(db, m, data.pop("content", None), **data)
    except mem.MemoryRejected as e:
        raise HTTPException(400, str(e)) from e
    audit(db, user.id, f"user:{user.id}", "memory.update", m, fields=list(body.model_dump(exclude_unset=True)))
    db.commit()
    return memory_out(m)


@router.delete("/memories/{mem_id}")
def delete_memory(mem_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = owned(db, Memory, mem_id, user.id)
    db.delete(m)
    audit(db, user.id, f"user:{user.id}", "memory.delete", None, memory_id=mem_id)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------- skills

class SkillSpec(BaseModel):
    purpose: str = Field(min_length=3, max_length=2000)
    trigger_conditions: list[str] = Field(default_factory=list)
    inputs: list[dict] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list, description="tools the skill needs")
    steps: list[dict | str] = Field(min_length=1)
    validation: list[str] = Field(default_factory=list)
    failure_handling: list[str] = Field(default_factory=list)
    output_contract: str = ""


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    spec: SkillSpec


def skill_out(db, s: Skill):
    vs = db.scalars(select(SkillVersion).where(SkillVersion.skill_id == s.id).order_by(SkillVersion.version.desc())).all()
    return {"id": s.id, "name": s.name, "enabled": s.enabled, "current_version": s.current_version,
            "versions": [{"id": v.id, "version": v.version, "status": v.status, "spec": v.spec, "source": v.source,
                          "created_at": v.created_at} for v in vs]}


def new_skill(db, user_id, name, spec: dict, source: dict) -> Skill:
    s = Skill(owner_user_id=user_id, name=name)
    db.add(s)
    db.flush()
    db.add(SkillVersion(skill_id=s.id, version=1, spec=spec, status="draft", source=source))
    return s


@router.get("/skills")
def skills(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [skill_out(db, s) for s in db.scalars(select(Skill).where(Skill.owner_user_id == user.id).order_by(Skill.name))]


@router.post("/skills", status_code=201)
def create_skill(body: SkillIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = new_skill(db, user.id, body.name, body.spec.model_dump(), {"kind": "manual"})
    db.commit()
    return skill_out(db, s)


@router.post("/skills/{skill_id}/versions", status_code=201)
def new_version(skill_id: str, body: SkillSpec, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = owned(db, Skill, skill_id, user.id)
    last = db.scalar(select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == s.id)) or 0
    db.add(SkillVersion(skill_id=s.id, version=last + 1, spec=body.model_dump(), status="draft", source={"kind": "edit"}))
    db.commit()
    return skill_out(db, s)


@router.post("/skills/{skill_id}/versions/{version}/approve")
def approve_version(skill_id: str, version: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = owned(db, Skill, skill_id, user.id)
    v = db.scalar(select(SkillVersion).where(SkillVersion.skill_id == s.id, SkillVersion.version == version))
    if v is None:
        raise NotFound("skill_versions")
    v.status, s.current_version = "approved", version
    audit(db, user.id, f"user:{user.id}", "skill.approve", v)
    db.commit()
    return skill_out(db, s)


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(owned(db, Skill, skill_id, user.id))
    db.commit()
    return {"deleted": True}


@router.post("/skills/from-task/{task_id}", status_code=201)
def skill_from_task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Draft a skill from a succeeded task's plan and the tool calls that actually succeeded. Needs review."""
    t = owned(db, Task, task_id, user.id)
    if t.status != "succeeded":
        raise HTTPException(409, "only succeeded tasks can become skills")
    calls = db.scalars(select(ToolCall).where(ToolCall.task_id == t.id, ToolCall.status == "succeeded")
                       .order_by(ToolCall.seq)).all()
    spec = SkillSpec(purpose=(t.plan or {}).get("summary") or t.objective[:300],
                     trigger_conditions=[f"Requests similar to: {t.objective[-200:]}"],
                     permissions=sorted({c.tool for c in calls}),
                     steps=[{"tool": c.tool, "args_example": c.args} for c in calls] or (t.plan or {}).get("steps", ["answer"]),
                     validation=["Deliverable exists and answers the objective"],
                     failure_handling=["Stop and ask the user when a step fails or needs sign-in"],
                     output_contract=json.dumps(t.deliverable) if t.deliverable else "final answer")
    s = new_skill(db, user.id, f"From task {t.id[:8]}", spec.model_dump(), {"kind": "task", "task_id": t.id})
    db.commit()
    return skill_out(db, s)


class DemoIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    steps: list[dict] = Field(min_length=1, max_length=200)


def demo_to_spec(steps: list[dict]) -> dict:
    """Recorded human steps → proposed parameterized skill using semantic locators (role + accessible name).
    Typed values become inputs; sensitive input was never recorded."""
    inputs, out = [], []
    for s in steps:
        a = s.get("action")
        if a == "open":
            out.append({"tool": "browser_open", "args": {"url": s["url"]}})
        elif a == "click":
            out.append({"tool": "browser_click", "args": {"role": s.get("role", ""), "name": s.get("name", "")}})
        elif a == "type":
            inputs.append({"name": s["param"], "label": s.get("label", ""), "example": s.get("example", "")})
            out.append({"tool": "browser_type", "args": {"label": s.get("label", ""), "text": "{{" + s["param"] + "}}"}})
        elif a == "press":
            if out and out[-1].get("tool") == "browser_type":
                out[-1]["args"]["submit"] = True
            else:
                out.append({"human_step": "Press Enter to submit."})
        elif a == "sensitive_input_excluded":
            out.append({"human_step": "A human must enter a sensitive value here (not recorded)."})
    return SkillSpec(purpose="Replay a recorded browser workflow", inputs=inputs,
                     permissions=sorted({x["tool"] for x in out if "tool" in x}), steps=out,
                     validation=["Run once under review before relying on it"],
                     failure_handling=["If an element is not found, stop and report; do not guess coordinates"],
                     output_contract="final page state summary").model_dump()


@router.post("/skills/from-demonstration", status_code=201)
def skill_from_demo(body: DemoIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = new_skill(db, user.id, body.name, demo_to_spec(body.steps), {"kind": "demonstration", "steps": len(body.steps)})
    db.commit()
    return skill_out(db, s)


# ---------------------------------------------------------------- routines

class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    timezone: str | None = None


@router.post("/routines/parse")
def parse_routine(body: ParseIn, user: User = Depends(current_user)):
    try:
        cron = rt.parse_schedule(body.text)
        return {"text": body.text, **rt.describe(cron, body.timezone or user.timezone)}
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e


class RoutineIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    bot_id: str
    prompt: str = Field(min_length=1, max_length=10000)
    trigger: str = Field("schedule", pattern="^(schedule|webhook)$")
    schedule_text: str = ""
    timezone: str | None = None
    missed_policy: str = Field("run_once", pattern="^(skip|run_once)$")
    deliverable: str | None = Field(None, pattern=r"^[\w\-. ]{1,120}\.(md|txt|csv|json)$")
    confirmed: bool = Field(False, description="must be true: the user confirmed the parsed schedule preview")


def routine_out(r: Routine):
    return {"id": r.id, "name": r.name, "bot_id": r.bot_id, "prompt": r.prompt, "trigger": r.trigger,
            "schedule_text": r.schedule_text, "cron": r.cron, "timezone": r.timezone, "missed_policy": r.missed_policy,
            "enabled": r.enabled, "next_run_at": r.next_run_at, "last_run_at": r.last_run_at,
            "webhook_url": f"/api/hooks/routines/{r.id}" if r.trigger == "webhook" else None,
            "deliverable": r.deliverable, "created_at": r.created_at}


@router.get("/routines")
def list_routines(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [routine_out(r) for r in db.scalars(select(Routine).where(Routine.owner_user_id == user.id).order_by(Routine.name))]


@router.post("/routines", status_code=201)
def create_routine(body: RoutineIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    bot_access(db, body.bot_id, user.id)
    tz = body.timezone or user.timezone
    r = Routine(owner_user_id=user.id, bot_id=body.bot_id, name=body.name, prompt=body.prompt, trigger=body.trigger,
                timezone=tz, missed_policy=body.missed_policy,
                deliverable={"name": body.deliverable} if body.deliverable else {})
    secret_value = None
    if body.trigger == "schedule":
        if not body.confirmed:
            raise HTTPException(400, "confirm the parsed schedule (confirmed=true) after reviewing the preview")
        try:
            r.cron = rt.parse_schedule(body.schedule_text)
            r.next_run_at = rt.next_runs(r.cron, tz, utcnow(), 1)[0]
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        r.schedule_text = body.schedule_text
    else:
        secret_value = secrets.token_urlsafe(32)
        r.webhook_secret_id = store_secret(db, user.id, f"webhook:{body.name}", secret_value).id
    db.add(r)
    audit(db, user.id, f"user:{user.id}", "routine.create", r)
    db.commit()
    return routine_out(r) | ({"webhook_secret_shown_once": secret_value} if secret_value else {})


class RoutinePatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    enabled: bool | None = None
    schedule_text: str | None = None
    timezone: str | None = None
    missed_policy: str | None = Field(None, pattern="^(skip|run_once)$")
    confirmed: bool = False


@router.patch("/routines/{rid}")
def patch_routine(rid: str, body: RoutinePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = owned(db, Routine, rid, user.id)
    data = body.model_dump(exclude_unset=True, exclude={"confirmed"})
    if ("schedule_text" in data or "timezone" in data) and r.trigger == "schedule":
        if not body.confirmed:
            raise HTTPException(400, "confirm the new schedule preview (confirmed=true)")
        try:
            r.cron = rt.parse_schedule(data.get("schedule_text", r.schedule_text))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    for k, v in data.items():
        if v is not None:
            setattr(r, k, v)
    if r.trigger == "schedule" and r.enabled:
        r.next_run_at = rt.next_runs(r.cron, r.timezone, utcnow(), 1)[0]
    db.commit()
    return routine_out(r)


@router.delete("/routines/{rid}")
def delete_routine(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(owned(db, Routine, rid, user.id))
    db.commit()
    return {"deleted": True}


@router.post("/routines/{rid}/run-now")
def run_now(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = owned(db, Routine, rid, user.id)
    run = rt.fire(db, r, "manual", f"manual:{utcnow().isoformat()}")
    db.commit()
    return {"run_id": run.id, "status": run.status, "task_id": run.task_id}


@router.get("/routines/{rid}/runs")
def routine_runs(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = owned(db, Routine, rid, user.id)
    runs = db.scalars(select(RoutineRun).where(RoutineRun.routine_id == r.id).order_by(RoutineRun.created_at.desc()).limit(100))
    out = []
    for x in runs:
        t = db.get(Task, x.task_id) if x.task_id else None
        out.append({"id": x.id, "trigger": x.trigger, "status": t.status if t else x.status, "task_id": x.task_id,
                    "scheduled_for": x.scheduled_for, "created_at": x.created_at})
    return out


@router.post("/hooks/routines/{rid}")
async def webhook(rid: str, request: Request, x_localcrew_timestamp: str = Header(""),
                  x_localcrew_signature: str = Header(""), x_localcrew_delivery: str = Header("")):
    """Authenticated webhook trigger: HMAC-SHA256(secret, f"{timestamp}.{body}") hex, timestamp within 5 minutes,
    delivery id deduplicated per routine."""
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "payload too large")

    def handle():
        from .db import SessionLocal
        db = SessionLocal()()
        try:
            r = db.get(Routine, rid)
            if r is None or r.trigger != "webhook" or not r.enabled:
                raise HTTPException(404, "not found")
            try:
                ts = int(x_localcrew_timestamp)
            except ValueError as e:
                raise HTTPException(401, "bad signature") from e
            if abs(time.time() - ts) > 300 or not x_localcrew_delivery:
                raise HTTPException(401, "stale or missing delivery headers")
            secret = reveal_secret(db, r.webhook_secret_id, r.owner_user_id)
            if not hmac_ok(secret, f"{ts}.".encode() + body, x_localcrew_signature):
                raise HTTPException(401, "bad signature")
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = {"raw": body.decode("utf-8", "replace")[:2000]}
            run = rt.fire(db, r, "webhook", f"delivery:{x_localcrew_delivery[:100]}", payload=payload)
            db.commit()
            if run is None:
                return {"duplicate": True}
            return {"run_id": run.id, "status": run.status, "task_id": run.task_id}
        finally:
            db.close()
    return await run_in_threadpool(handle)


# ---------------------------------------------------------------- connections

@router.get("/connectors")
def connectors(user: User = Depends(current_user)):
    return {"available": [describe(c) for c in REGISTRY.values()], "optional": OPTIONAL}


class ConnIn(BaseModel):
    connector: str
    name: str = Field(min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)
    secret: str | None = Field(None, max_length=4000)
    scopes: list[str] = Field(default_factory=list)


def conn_out(c: Connection):
    return {"id": c.id, "connector": c.connector, "name": c.name, "config": c.config, "scopes": c.scopes,
            "status": c.status, "has_secret": bool(c.secret_id), "created_at": c.created_at, "revoked_at": c.revoked_at}


@router.get("/connections")
def list_connections(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [conn_out(c) for c in db.scalars(select(Connection).where(Connection.owner_user_id == user.id))]


@router.post("/connections", status_code=201)
def add_connection(body: ConnIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.connector not in REGISTRY:
        raise HTTPException(400, "unknown or unavailable connector")
    if "base_url" in body.config:
        from .security import check_url
        try:
            check_url(body.config["base_url"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    c = Connection(owner_user_id=user.id, connector=body.connector, name=body.name, config=body.config,
                   scopes=body.scopes or [s for cap in REGISTRY[body.connector].capabilities.values() for s in cap.scopes])
    if body.secret:
        c.secret_id = store_secret(db, user.id, f"connection:{body.name}", body.secret).id
    db.add(c)
    audit(db, user.id, f"user:{user.id}", "connection.add", c, connector=body.connector)
    db.commit()
    return conn_out(c)


@router.get("/connections/{cid}/health")
def connection_health(cid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = owned(db, Connection, cid, user.id)
    if c.status != "active":
        return {"ok": False, "status": c.status}
    return REGISTRY[c.connector](db, c).health()


@router.post("/connections/{cid}/revoke")
def revoke_connection(cid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = owned(db, Connection, cid, user.id)
    c.status, c.revoked_at = "revoked", utcnow()
    if c.secret_id:
        db.execute(update(Connection).where(Connection.id == c.id).values(secret_id=None))
        db.query(SecretReference).filter(SecretReference.id == c.secret_id).delete()
    audit(db, user.id, f"user:{user.id}", "connection.revoke", c)
    db.commit()
    return conn_out(db.get(Connection, cid))


# ---------------------------------------------------------------- computer (live view & takeover)

def computer(path: str, body: dict, timeout=30):
    s = settings()
    try:
        r = httpx.post(f"{s.computer_url}{path}", json=body, headers={"X-Internal-Token": s.computer_token}, timeout=timeout)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"computer service unavailable: {type(e).__name__}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise HTTPException(r.status_code if r.status_code < 500 else 502, detail)
    return r.json()


@router.get("/computer/status")
def computer_status(user: User = Depends(current_user)):
    return computer("/control/status", {"user_id": user.id})


@router.post("/computer/takeover")
def takeover(user: User = Depends(current_user), db: Session = Depends(get_db)):
    res = computer("/control/takeover", {"user_id": user.id, "human_id": user.id}, timeout=120)
    lease = db.get(ExecutionLease, f"browser:{user.id}") or ExecutionLease(resource=f"browser:{user.id}")
    lease.owner_user_id, lease.holder_kind, lease.holder_id = user.id, "human", user.id
    lease.expires_at = utcnow() + timedelta(days=1)
    db.merge(lease)
    if not db.scalar(select(BrowserSession).where(BrowserSession.owner_user_id == user.id)):
        db.add(BrowserSession(owner_user_id=user.id))
    audit(db, user.id, f"user:{user.id}", "computer.takeover", None, revoked_task=res.get("revoked_task"))
    db.commit()
    return res


@router.post("/computer/release")
def release(user: User = Depends(current_user), db: Session = Depends(get_db)):
    res = computer("/control/release", {"user_id": user.id})
    db.query(ExecutionLease).filter(ExecutionLease.resource == f"browser:{user.id}").delete()
    resumed = []
    for t in db.scalars(select(Task).where(Task.owner_user_id == user.id, Task.status == "waiting_for_user")):
        if (t.blocker or {}).get("kind") in ("control_revoked", "human_required", "session_expired"):
            if t.blocker.get("tool_call_id"):
                tc = db.get(ToolCall, t.blocker["tool_call_id"])
                if tc and tc.status == "blocked":
                    tc.status = "retry"
            t.status, t.blocker, t.next_attempt_at = "queued", {}, utcnow()
            emit(db, user.id, "status", {"status": "queued", "reason": "browser control released"}, t.id)
            resumed.append(t.id)
    audit(db, user.id, f"user:{user.id}", "computer.release", None, resumed=resumed)
    db.commit()
    return res | {"resumed_tasks": resumed}


@router.get("/computer/screenshot")
def screenshot(user: User = Depends(current_user)):
    return computer("/human/screenshot", {"user_id": user.id})


class HumanAct(BaseModel):
    kind: str = Field(pattern="^(click|type|key|goto)$")
    x: float | None = None
    y: float | None = None
    text: str | None = Field(None, max_length=5000)
    key: str | None = None
    url: str | None = None
    sensitive: bool = False


@router.post("/computer/act")
def human_act(body: HumanAct, user: User = Depends(current_user)):
    """Human input during takeover. Typed text is never stored or logged."""
    base = {"user_id": user.id, "human_id": user.id}
    if body.kind == "click":
        return computer("/human/click", base | {"x": body.x, "y": body.y})
    if body.kind == "type":
        return computer("/human/type", base | {"text": body.text or "", "sensitive": body.sensitive})
    if body.kind == "key":
        return computer("/human/key", base | {"key": body.key})
    return computer("/human/goto", base | {"url": body.url}, timeout=60)


@router.post("/computer/record/{action}")
def record(action: str, user: User = Depends(current_user)):
    if action not in ("start", "stop"):
        raise HTTPException(404)
    return computer(f"/record/{action}", {"user_id": user.id})


@router.delete("/computer/session")
def delete_browser_session(user: User = Depends(current_user), db: Session = Depends(get_db)):
    res = computer("/session/delete", {"user_id": user.id})
    db.query(BrowserSession).filter(BrowserSession.owner_user_id == user.id).delete()
    audit(db, user.id, f"user:{user.id}", "computer.session_delete", None)
    db.commit()
    return res


# ---------------------------------------------------------------- teams

class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.post("/teams", status_code=201)
def create_team(body: TeamIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = Team(name=body.name)
    db.add(t)
    db.flush()
    db.add(Membership(team_id=t.id, user_id=user.id, role="owner"))
    db.commit()
    return {"id": t.id, "name": t.name}


@router.get("/teams")
def list_teams(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(Team, Membership.role).join(Membership, Membership.team_id == Team.id)
                      .where(Membership.user_id == user.id)).all()
    return [{"id": t.id, "name": t.name, "role": role} for t, role in rows]


class MemberIn(BaseModel):
    email: str
    role: str = Field("member", pattern="^(owner|manager|member)$")


@router.post("/teams/{team_id}/members", status_code=201)
def add_member(team_id: str, body: MemberIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if team_role(db, user.id, team_id) not in ("owner", "manager"):
        raise NotFound("teams")
    if body.role == "owner" and team_role(db, user.id, team_id) != "owner":
        raise HTTPException(403, "only owners add owners")
    u = db.scalar(select(User).where(User.email == body.email.lower()))
    if u is None:
        raise HTTPException(404, "no such user")
    db.merge(Membership(team_id=team_id, user_id=u.id, role=body.role))
    db.commit()
    return {"ok": True}


@router.post("/bots/{bot_id}/share/{team_id}")
def share_bot(bot_id: str, team_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Share bot configuration with a team. Conversations stay private per person; memories are not moved."""
    b = bot_access(db, bot_id, user.id, write=True)
    if team_role(db, user.id, team_id) not in ("owner", "manager"):
        raise HTTPException(403, "only team owners/managers share bots")
    b.team_id = team_id
    audit(db, user.id, f"user:{user.id}", "bot.share", b, team_id=team_id)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- models, diagnostics, audit

@router.get("/models")
def models(user: User = Depends(current_user)):
    p = OllamaProvider()
    h = p.health()
    out = []
    for m in h.get("models", []):
        try:
            c = p.capabilities(m)
            out.append({"name": m, "tools": c.tools, "vision": c.vision, "audio": c.audio, "context_length": c.context_length})
        except Exception as e:  # noqa: BLE001
            out.append({"name": m, "error": str(e)})
    return {"ollama": h, "models": out, "default": settings().default_model,
            "remote_providers": {"enabled": settings().remote_providers_enabled, "implemented": False,
                                 "data_leaving_machine": "none: all inference is local"}}


@router.get("/diagnostics")
def diagnostics(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db_ok = db.scalar(text("select 1")) == 1
    workers = db.scalars(select(ExecutionLease).where(ExecutionLease.holder_kind == "worker",
                                                      ExecutionLease.expires_at > utcnow())).all()
    try:
        comp = httpx.get(f"{settings().computer_url}/health", timeout=3).json()
    except httpx.HTTPError as e:
        comp = {"ok": False, "error": type(e).__name__}
    counts = dict(db.execute(select(Task.status, func.count()).where(Task.owner_user_id == user.id).group_by(Task.status)).all())
    return {"database": {"ok": db_ok}, "ollama": OllamaProvider().health(), "computer": comp,
            "workers": [{"id": w.holder_id, "last_seen_until": w.expires_at} for w in workers],
            "tasks_by_status": counts, "host_note": "Local execution stops when this computer sleeps or shuts down.",
            "time_utc": utcnow()}


@router.get("/audit")
def audit_log(limit: int = 200, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": a.id, "actor": a.actor, "action": a.action, "object_type": a.object_type, "object_id": a.object_id,
             "data": a.data, "at": a.created_at}
            for a in db.scalars(select(AuditEvent).where(AuditEvent.owner_user_id == user.id)
                                .order_by(AuditEvent.id.desc()).limit(min(limit, 1000)))]
