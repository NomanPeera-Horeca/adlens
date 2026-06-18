"""Owner/admin bypass — full Pro features without billing."""
from .config import settings
from .models import User


def _admin_emails() -> set[str]:
    if not settings.ADMIN_EMAILS.strip():
        return set()
    return {e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()}


def _admin_fb_ids() -> set[str]:
    if not settings.ADMIN_FB_IDS.strip():
        return set()
    return {i.strip() for i in settings.ADMIN_FB_IDS.split(",") if i.strip()}


def is_admin(user: User) -> bool:
    if user.email and user.email.lower() in _admin_emails():
        return True
    if user.fb_user_id in _admin_fb_ids():
        return True
    return False


def effective_plan(user: User) -> str:
    """Plan used for feature gating (date ranges, cache TTL)."""
    if is_admin(user):
        return "pro"
    return user.plan
