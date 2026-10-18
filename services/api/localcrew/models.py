"""Database schema. Every protected row carries owner_user_id (and team_id where shareable).

Access scopes:
  personal   – owner only (conversations, tasks, artifacts, private notes)
  bot        – owner only, bound to one bot (bot memories)
  workspace  – shared by all bots of one user (files, browser profile)
  team       – team members per role (team bots, team memory)
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Computed, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import new_id, utcnow

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


def pk():
    return mapped_column(String(32), primary_key=True, default=new_id)


def fk(target, nullable=False, ondelete="CASCADE"):
    return mapped_column(String(32), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


def created():
    return mapped_column(TS, default=utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Karachi")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created()


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token)
    user_id: Mapped[str] = fk("users.id")
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = created()


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)
    id: Mapped[str] = pk()
    team_id: Mapped[str] = fk("teams.id")
    user_id: Mapped[str] = fk("users.id")
    role: Mapped[str] = mapped_column(String(16))  # owner|manager|member
    created_at: Mapped[datetime] = created()


class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    team_id: Mapped[str | None] = fk("teams.id", nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    avatar: Mapped[str] = mapped_column(String(16), default="🤖")
    role: Mapped[str] = mapped_column(String(200), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    model_options: Mapped[dict] = mapped_column(JSONB, default=dict)
    tools: Mapped[list] = mapped_column(JSONB, default=list)
    policy_profile: Mapped[str] = mapped_column(String(32), default="standard")
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class BotVersion(Base):
    __tablename__ = "bot_versions"
    __table_args__ = (UniqueConstraint("bot_id", "version"),)
    id: Mapped[str] = pk()
    bot_id: Mapped[str] = fk("bots.id")
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created()


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    kind: Mapped[str] = mapped_column(String(16), default="direct")  # direct|group
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = created()


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("conversation_id", "bot_id"),)
    id: Mapped[str] = pk()
    conversation_id: Mapped[str] = fk("conversations.id")
    bot_id: Mapped[str | None] = fk("bots.id", nullable=True)
    user_id: Mapped[str | None] = fk("users.id", nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = pk()
    conversation_id: Mapped[str] = fk("conversations.id")
    owner_user_id: Mapped[str] = fk("users.id")
    author_user_id: Mapped[str | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    author_bot_id: Mapped[str | None] = fk("bots.id", nullable=True, ondelete="SET NULL")
    content: Mapped[str] = mapped_column(Text)
    mentions: Mapped[list] = mapped_column(JSONB, default=list)
    attachments: Mapped[list] = mapped_column(JSONB, default=list)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = created()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_claim", "status", "next_attempt_at"),)
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    bot_id: Mapped[str] = fk("bots.id")
    conversation_id: Mapped[str | None] = fk("conversations.id", nullable=True)
    parent_task_id: Mapped[str | None] = fk("tasks.id", nullable=True, ondelete="SET NULL")
    routine_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skill_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    objective: Mapped[str] = mapped_column(Text)
    deliverable: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"type":"markdown","name":"report.md"}
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(32), default="planning")
    progress: Mapped[str] = mapped_column(Text, default="")
    blocker: Mapped[dict] = mapped_column(JSONB, default=dict)
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    deadline_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    tool_budget: Mapped[int] = mapped_column(Integer, default=20)
    tool_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    handoff: Mapped[dict] = mapped_column(JSONB, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    redirects: Mapped[list] = mapped_column(JSONB, default=list)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class TaskStep(Base):
    __tablename__ = "task_steps"
    id: Mapped[str] = pk()
    task_id: Mapped[str] = fk("tasks.id")
    idx: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # plan|tool|validate|report|handoff
    status: Mapped[str] = mapped_column(String(16), default="running")
    summary: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = fk("tasks.id", nullable=True)
    owner_user_id: Mapped[str] = fk("users.id")
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created()


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[str] = pk()
    task_id: Mapped[str] = fk("tasks.id")
    owner_user_id: Mapped[str] = fk("users.id")
    seq: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(80))
    args: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    effect: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    task_id: Mapped[str | None] = fk("tasks.id", nullable=True, ondelete="SET NULL")
    bot_id: Mapped[str | None] = fk("bots.id", nullable=True, ondelete="SET NULL")
    name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text)  # relative to data_dir/artifacts
    version: Mapped[int] = mapped_column(Integer, default=1)
    previous_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    scope: Mapped[str] = mapped_column(String(16), default="personal")
    created_at: Mapped[datetime] = created()


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_tsv", "tsv", postgresql_using="gin"),)
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    bot_id: Mapped[str | None] = fk("bots.id", nullable=True)
    team_id: Mapped[str | None] = fk("teams.id", nullable=True)
    scope: Mapped[str] = mapped_column(String(16))  # bot|user|team|private_note
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"kind":"task","id":..} | {"kind":"user"}
    confidence: Mapped[float] = mapped_column(default=0.7)
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list] = mapped_column(JSONB, default=list)
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('english', content)", persisted=True))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    team_id: Mapped[str | None] = fk("teams.id", nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = created()


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version"),)
    id: Mapped[str] = pk()
    skill_id: Mapped[str] = fk("skills.id")
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|approved
    source: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created()


class Routine(Base):
    __tablename__ = "routines"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    bot_id: Mapped[str] = fk("bots.id")
    name: Mapped[str] = mapped_column(String(120))
    prompt: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule|webhook
    schedule_text: Mapped[str] = mapped_column(String(200), default="")
    cron: Mapped[str] = mapped_column(String(120), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Karachi")
    missed_policy: Mapped[str] = mapped_column(String(16), default="run_once")  # skip|run_once
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(TS, nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    webhook_secret_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deliverable: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created()


class RoutineRun(Base):
    __tablename__ = "routine_runs"
    __table_args__ = (UniqueConstraint("routine_id", "dedup_key"),)
    id: Mapped[str] = pk()
    routine_id: Mapped[str] = fk("routines.id")
    owner_user_id: Mapped[str] = fk("users.id")
    dedup_key: Mapped[str] = mapped_column(String(120))
    trigger: Mapped[str] = mapped_column(String(16))  # schedule|manual|webhook|missed
    scheduled_for: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    status: Mapped[str] = mapped_column(String(24))  # started|skipped_overlap|skipped_missed|succeeded|failed
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = created()


class SecretReference(Base):
    __tablename__ = "secret_references"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    team_id: Mapped[str | None] = fk("teams.id", nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = created()
    rotated_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    team_id: Mapped[str | None] = fk("teams.id", nullable=True)
    connector: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    secret_id: Mapped[str | None] = fk("secret_references.id", nullable=True, ondelete="SET NULL")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|revoked|error
    created_at: Mapped[datetime] = created()
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class PolicyRule(Base):
    __tablename__ = "policy_rules"
    id: Mapped[str] = pk()
    level: Mapped[str] = mapped_column(String(8))  # org|user|bot
    owner_user_id: Mapped[str | None] = fk("users.id", nullable=True)
    team_id: Mapped[str | None] = fk("teams.id", nullable=True)
    bot_id: Mapped[str | None] = fk("bots.id", nullable=True)
    tool: Mapped[str] = mapped_column(String(80))  # exact tool name or "*" or "effect:<class>"
    target: Mapped[str] = mapped_column(String(500), default="*")  # exact target or "*"
    decision: Mapped[str] = mapped_column(String(20))  # allow|deny|require_approval
    note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = created()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    task_id: Mapped[str] = fk("tasks.id")
    tool_call_id: Mapped[str] = fk("tool_calls.id")
    bot_id: Mapped[str] = fk("bots.id")
    tool: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    expected_effect: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|denied|expired|consumed
    decision_scope: Mapped[str] = mapped_column(String(16), default="once")  # once|rule
    expires_at: Mapped[datetime] = mapped_column(TS)
    decided_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = created()


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id: Mapped[str] = pk()
    owner_user_id: Mapped[str] = fk("users.id")
    profile: Mapped[str] = mapped_column(String(64), default="default")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|deleted
    sites: Mapped[list] = mapped_column(JSONB, default=list)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class ExecutionLease(Base):
    __tablename__ = "execution_leases"
    resource: Mapped[str] = mapped_column(String(120), primary_key=True)  # browser:<user_id> | worker:<id>
    owner_user_id: Mapped[str | None] = fk("users.id", nullable=True)
    holder_kind: Mapped[str] = mapped_column(String(16))  # task|human|worker
    holder_id: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = created()


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(80))  # user:<id> | bot:<id> | system
    action: Mapped[str] = mapped_column(String(80))
    object_type: Mapped[str] = mapped_column(String(40), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, server_default=text("now()"))
