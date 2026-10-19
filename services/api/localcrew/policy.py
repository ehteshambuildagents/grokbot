"""Authorization for effectful actions. Enforced in code, never delegated to a model.

decide() precedence:
  1. any matching deny (org, user or bot level)          -> deny
  2. org require_approval                                 -> require_approval (user rules cannot relax)
  3. user-level allow (e.g. "always allow" from an approval) -> allow
  4. any matching require_approval (user or bot)          -> require_approval
  5. bot-profile default for the effect class
Bot-level rules can only tighten (their 'allow' is ignored).
"""
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .db import utcnow
from .models import ApprovalRequest, PolicyRule
from .security import canonical_digest

PROFILES = {
    # effect -> decision
    "standard": {"read": "allow", "workspace_write": "allow", "browser": "allow", "shell": "require_approval",
                 "external_write": "require_approval", "destructive": "require_approval"},
    "cautious": {"read": "allow", "workspace_write": "require_approval", "browser": "require_approval",
                 "shell": "deny", "external_write": "require_approval", "destructive": "deny"},
    "read_only": {"read": "allow", "workspace_write": "deny", "browser": "allow", "shell": "deny",
                  "external_write": "deny", "destructive": "deny"},
}
APPROVAL_TTL = timedelta(minutes=30)


@dataclass
class Action:
    tool: str
    effect: str
    target: str
    payload: dict
    user_id: str
    bot_id: str

    def digest(self) -> str:
        return canonical_digest({"tool": self.tool, "target": self.target, "payload": self.payload,
                                 "user": self.user_id, "bot": self.bot_id})


def _matches(rule: PolicyRule, a: Action) -> bool:
    tool_ok = rule.tool in ("*", a.tool, f"effect:{a.effect}") or (
        rule.tool.endswith("*") and len(rule.tool) > 1 and a.tool.startswith(rule.tool[:-1]))  # e.g. mcp__github__*
    return tool_ok and rule.target in ("*", a.target) and rule.bot_id in (None, a.bot_id)


def decide(db: Session, a: Action, profile: str, team_id: str | None = None) -> tuple[str, str]:
    now = utcnow()
    rules = db.scalars(select(PolicyRule).where(
        or_(PolicyRule.expires_at.is_(None), PolicyRule.expires_at > now),
        or_(PolicyRule.level == "org",
            (PolicyRule.level == "user") & (PolicyRule.owner_user_id == a.user_id),
            (PolicyRule.level == "bot") & (PolicyRule.bot_id == a.bot_id)),
    )).all()
    rules = [r for r in rules if _matches(r, a) and (r.level != "org" or r.team_id in (None, team_id))]
    if any(r.decision == "deny" for r in rules):
        r = next(r for r in rules if r.decision == "deny")
        return "deny", f"{r.level} rule {r.id}"
    if any(r.level == "org" and r.decision == "require_approval" for r in rules):
        return "require_approval", "organization rule"
    if any(r.level == "user" and r.decision == "allow" for r in rules):
        return "allow", "user rule"
    if any(r.decision == "require_approval" for r in rules):
        return "require_approval", "rule"
    return PROFILES.get(profile, PROFILES["cautious"]).get(a.effect, "require_approval"), f"profile {profile}"


def find_or_create_approval(db: Session, a: Action, task_id: str, tool_call_id: str, expected_effect: str) -> ApprovalRequest:
    digest = a.digest()
    existing = db.scalar(select(ApprovalRequest).where(ApprovalRequest.tool_call_id == tool_call_id)
                         .order_by(ApprovalRequest.created_at.desc()))
    if existing and existing.digest == digest and existing.status in ("pending", "approved"):
        return existing
    req = ApprovalRequest(owner_user_id=a.user_id, task_id=task_id, tool_call_id=tool_call_id, bot_id=a.bot_id,
                          tool=a.tool, target=a.target, payload=a.payload, digest=digest,
                          expected_effect=expected_effect, expires_at=utcnow() + APPROVAL_TTL)
    db.add(req)
    db.flush()
    return req


def consume_approval(db: Session, approval_id: str, a: Action) -> bool:
    """Atomically consume an approved, unexpired approval whose digest matches the action exactly.
    Returns False for replay, expiry, denial, or any change in tool/target/payload/identity."""
    res = db.execute(update(ApprovalRequest)
                     .where(ApprovalRequest.id == approval_id, ApprovalRequest.status == "approved",
                            ApprovalRequest.digest == a.digest(), ApprovalRequest.expires_at > utcnow())
                     .values(status="consumed", consumed_at=utcnow()))
    return res.rowcount == 1


def expire_stale(db: Session) -> int:
    res = db.execute(update(ApprovalRequest)
                     .where(ApprovalRequest.status.in_(["pending", "approved"]), ApprovalRequest.expires_at <= utcnow())
                     .values(status="expired"))
    return res.rowcount
