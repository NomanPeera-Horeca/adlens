"""Team workspaces — owner connects Meta; members see selected ad accounts."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlmodel import Session, select

from .config import settings
from .models import User
from .team_models import TeamMember, Workspace


def norm_account(account_id: str) -> str:
    return str(account_id or "").replace("act_", "")


def parse_accounts(raw: str) -> list[str]:
    try:
        ids = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [norm_account(x) for x in ids if x]


def ensure_workspace(session: Session, user: User) -> Workspace:
    ws = session.exec(select(Workspace).where(Workspace.owner_user_id == user.id)).first()
    if ws:
        return ws
    ws = Workspace(
        owner_user_id=user.id,
        name=(user.name or "My team").split()[0] + "'s workspace" if user.name else "AdLens workspace",
    )
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return ws


@dataclass
class Actor:
    kind: str  # owner | member
    user: User
    member: TeamMember | None = None
    workspace: Workspace | None = None

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"

    @property
    def display_name(self) -> str:
        if self.member and self.member.name:
            return self.member.name
        return self.user.name or "User"

    @property
    def allowed_accounts(self) -> list[str] | None:
        """None = all ad accounts (owner)."""
        if self.is_owner:
            return None
        if not self.member:
            return []
        return parse_accounts(self.member.allowed_accounts_json)

    @property
    def owner_user_id(self) -> int:
        return self.user.id


def get_member(session: Session, member_id: int) -> TeamMember | None:
    member = session.get(TeamMember, member_id)
    if not member or not member.accepted_at:
        return None
    return member


def resolve_actor_from_member(session: Session, member: TeamMember) -> Actor | None:
    ws = session.get(Workspace, member.workspace_id)
    if not ws:
        return None
    owner = session.get(User, ws.owner_user_id)
    if not owner or not owner.fb_token_enc:
        return None
    member.last_active_at = datetime.utcnow()
    session.add(member)
    session.commit()
    return Actor(kind="member", user=owner, member=member, workspace=ws)


def filter_accounts(accounts: list[dict], actor: Actor) -> list[dict]:
    allowed = actor.allowed_accounts
    if allowed is None:
        return accounts
    allow = set(allowed)
    return [a for a in accounts if norm_account(a.get("account_id", "")) in allow]


def assert_account_access(actor: Actor, account_id: str) -> None:
    allowed = actor.allowed_accounts
    if allowed is None:
        return
    if norm_account(account_id) not in set(allowed):
        raise HTTPException(403, "You don't have access to this ad account.")


def member_to_dict(member: TeamMember) -> dict:
    base = settings.BASE_URL.rstrip("/")
    return {
        "id": member.id,
        "name": member.name,
        "email": member.email,
        "role": member.role,
        "accounts": parse_accounts(member.allowed_accounts_json),
        "accepted": bool(member.accepted_at),
        "created_at": member.created_at.isoformat() + "Z" if member.created_at else None,
        "invite_url": f"{base}/?invite={member.invite_token}",
    }


def create_invite(
    session: Session,
    workspace: Workspace,
    *,
    name: str,
    email: str,
    account_ids: list[str],
) -> TeamMember:
    if not account_ids:
        raise HTTPException(400, "Pick at least one ad account.")
    accounts = [norm_account(a) for a in account_ids if a]
    if not accounts:
        raise HTTPException(400, "Pick at least one ad account.")
    token = secrets.token_urlsafe(32)
    member = TeamMember(
        workspace_id=workspace.id,
        name=name.strip() or "Teammate",
        email=(email or "").strip().lower(),
        allowed_accounts_json=json.dumps(accounts),
        invite_token=token,
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    return member


def accept_invite(session: Session, token: str, name: str = "") -> TeamMember:
    member = session.exec(select(TeamMember).where(TeamMember.invite_token == token)).first()
    if not member:
        raise HTTPException(404, "Invite link invalid or expired.")
    ws = session.get(Workspace, member.workspace_id)
    if not ws:
        raise HTTPException(404, "Workspace not found.")
    owner = session.get(User, ws.owner_user_id)
    if not owner or not owner.fb_token_enc:
        raise HTTPException(503, "Workspace owner has not connected Facebook yet.")
    if name.strip():
        member.name = name.strip()
    member.accepted_at = datetime.utcnow()
    member.last_active_at = datetime.utcnow()
    session.add(member)
    session.commit()
    session.refresh(member)
    return member


def invite_preview(session: Session, token: str) -> dict:
    member = session.exec(select(TeamMember).where(TeamMember.invite_token == token)).first()
    if not member:
        raise HTTPException(404, "Invite not found.")
    ws = session.get(Workspace, member.workspace_id)
    owner = session.get(User, ws.owner_user_id) if ws else None
    return {
        "name": member.name,
        "owner_name": owner.name if owner else "Admin",
        "workspace": ws.name if ws else "AdLens",
        "accounts_count": len(parse_accounts(member.allowed_accounts_json)),
        "already_accepted": bool(member.accepted_at),
    }
