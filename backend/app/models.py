"""
Data model. One row per signed-up user (multi-tenant: each user only ever
sees their own Meta data, scoped by the token stored against their row).

Roadmap: add an Organization/Team table so multiple seats share one account.
"""
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    fb_user_id: str = Field(index=True, unique=True)
    name: str = ""
    email: str = ""

    # Encrypted long-lived Meta token (Fernet). NEVER store plaintext.
    fb_token_enc: str = ""
    token_expires_at: Optional[datetime] = None

    # Billing
    plan: str = "free"                     # free | pro
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: datetime = Field(default_factory=datetime.utcnow)


class SyncRun(SQLModel, table=True):
    """Cached insights pull for one user + ad account + date range."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    account_id: str = Field(index=True)          # numeric id, no act_ prefix
    date_range: str = Field(index=True)          # last_30d, last_90d, etc.

    status: str = "pending"                      # pending | success | error
    ads_json: str = "[]"                         # scored creatives payload
    error_message: str = ""
    synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
