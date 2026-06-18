"""
Data model. One row per signed-up user (multi-tenant: each user only ever
sees their own Meta data, scoped by the token stored against their row).

Roadmap: add an Organization/Team table so multiple seats share one account,
and a SyncRun table to cache insights so you're not hitting Meta on every load.
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
