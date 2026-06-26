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


class CreativeWinner(SQLModel, table=True):
    """Proven creative asset for an account — used to compare new ads to past winners."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    account_id: str = Field(index=True)
    asset_id: str = Field(index=True)            # Meta image hash or ad:{ad_id}

    ad_id: str = ""
    ad_name: str = ""
    name: str = ""
    thumb: str = ""

    category_key: str = Field(index=True, default="general")
    category_label: str = ""
    goal_key: str = Field(index=True, default="calls")
    goal_label: str = ""

    spend: float = 0.0
    ctr: float = 0.0
    calls: int = 0
    leads: int = 0
    landing_views: int = 0

    primary_metric: str = "calls"
    primary_count: int = 0
    primary_cost: Optional[float] = None
    rank_score: float = 0.0

    insight_visual: str = ""
    insight_hook: str = ""

    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
