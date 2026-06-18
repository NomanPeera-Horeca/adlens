"""
Facebook Login flow (server-side OAuth). This is the part that makes it a SaaS:
the user clicks Connect, grants access on facebook.com, and we exchange the code
for a long-lived token *on the server* using the app secret — which never
touches the browser.
"""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from . import meta, crypto
from .db import get_session
from .models import User
from .config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_error(code: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.FRONTEND_URL}/?error={code}")


@router.get("/facebook/login")
def fb_login(request: Request):
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        return _auth_error("meta_not_configured")
    if not settings.FERNET_KEY:
        return _auth_error("fernet_not_configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(meta.login_dialog_url(state))


@router.get("/facebook/callback")
async def fb_callback(request: Request, code: str = "", state: str = "",
                      error: str = "", error_description: str = "",
                      session: Session = Depends(get_session)):
    if error:
        return _auth_error("meta_denied")
    if not code or state != request.session.get("oauth_state"):
        return _auth_error("auth_failed")

    try:
        short = await meta.exchange_code_for_token(code)
        long = await meta.long_lived_token(short["access_token"])
        token = long["access_token"]
        expires_in = long.get("expires_in")  # None for non-expiring tokens

        profile = await meta.me(token)

        user = session.exec(select(User).where(User.fb_user_id == profile["id"])).first()
        if not user:
            user = User(fb_user_id=profile["id"])
        user.name = profile.get("name", "")
        user.email = profile.get("email", "")
        user.fb_token_enc = crypto.encrypt(token)
        user.token_expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)) if expires_in else None
        user.last_login_at = datetime.utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)

        request.session["user_id"] = user.id
        return RedirectResponse(settings.FRONTEND_URL + "/")
    except Exception:
        return _auth_error("meta_exchange_failed")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
