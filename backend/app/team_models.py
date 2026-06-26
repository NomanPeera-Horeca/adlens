"""Workspace + team member models."""
from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field


class Workspace(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_user_id: int = Field(index=True, unique=True, foreign_key="user.id")
    name: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeamMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspace.id")
    name: str = ""
    email: str = ""
    role: str = "member"
    allowed_accounts_json: str = "[]"
    invite_token: str = Field(index=True, unique=True)
    accepted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: Optional[datetime] = None
