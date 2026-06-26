"""
Facebook Login flow (server-side OAuth). This is the part that makes it a SaaS:
the user clicks Connect, grants access on facebook.com, and we exchange the code
for a long-lived token *on the server* using the app secret — which never
touches the browser.
"""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, TimestampSigner
from sqlmodel import Session, select

from . import meta, crypto
from .db import get_session
from .models import User
from . import team as team_svc
from .config import settings

router = APIRouter(prefix="/auth", tags=["auth"])
REMEMBER_COOKIE = "adlens_remember"
MEMBER_REMEMBER_COOKIE = "adlens_member"
_signer = TimestampSigner(settings.SESSION_SECRET)


def _cookie_flags() -> dict:
    return {
        "httponly": True,
        "secure": settings.ENV == "prod",
        "samesite": "lax",
        "path": "/",
    }


def sign_remember(user_id: int) -> str:
    return _signer.sign(str(user_id).encode()).decode()


def unsign_remember(value: str) -> int | None:
    try:
        raw = _signer.unsign(value, max_age=settings.REMEMBER_MAX_AGE)
        return int(raw.decode())
    except (BadSignature, ValueError):
        return None


def sign_member_remember(member_id: int) -> str:
    return _signer.sign(f"m:{member_id}".encode()).decode()


def unsign_member_remember(value: str) -> int | None:
    try:
        raw = _signer.unsign(value, max_age=settings.REMEMBER_MAX_AGE).decode()
        if not raw.startswith("m:"):
            return None
        return int(raw[2:])
    except (BadSignature, ValueError):
        return None


def attach_remember_cookie(response: RedirectResponse | JSONResponse, user_id: int) -> None:
    response.set_cookie(
        REMEMBER_COOKIE,
        sign_remember(user_id),
        max_age=settings.REMEMBER_MAX_AGE,
        **_cookie_flags(),
    )


def attach_member_remember_cookie(response: RedirectResponse | JSONResponse, member_id: int) -> None:
    response.set_cookie(
        MEMBER_REMEMBER_COOKIE,
        sign_member_remember(member_id),
        max_age=settings.REMEMBER_MAX_AGE,
        **_cookie_flags(),
    )


def clear_member_remember_cookie(response: JSONResponse) -> None:
    response.delete_cookie(MEMBER_REMEMBER_COOKIE, path="/")


def clear_remember_cookie(response: JSONResponse) -> None:
    response.delete_cookie(REMEMBER_COOKIE, path="/")
    clear_member_remember_cookie(response)


def touch_session(request: Request, user_id: int) -> None:
    request.session.pop("member_id", None)
    request.session["user_id"] = user_id


def touch_member_session(request: Request, member_id: int) -> None:
    request.session.clear()
    request.session["member_id"] = member_id


def resolve_user(request: Request, session: Session) -> User | None:
    """Owner login via Facebook (skipped when team member session is active)."""
    if request.session.get("member_id"):
        return None
    uid = request.session.get("user_id")
    if not uid:
        remembered = request.cookies.get(REMEMBER_COOKIE)
        if remembered:
            uid = unsign_remember(remembered)
            if uid:
                user = session.get(User, uid)
                if user:
                    touch_session(request, user.id)
                    return user
        return None
    user = session.get(User, uid)
    if not user:
        return None
    touch_session(request, user.id)
    return user


def resolve_actor(request: Request, session: Session) -> team_svc.Actor | None:
    member_id = request.session.get("member_id")
    if member_id:
        member = team_svc.get_member(session, member_id)
        if member:
            return team_svc.resolve_actor_from_member(session, member)
        request.session.pop("member_id", None)
    else:
        remembered_m = request.cookies.get(MEMBER_REMEMBER_COOKIE)
        if remembered_m:
            mid = unsign_member_remember(remembered_m)
            if mid:
                member = team_svc.get_member(session, mid)
                if member:
                    touch_member_session(request, member.id)
                    return team_svc.resolve_actor_from_member(session, member)

    user = resolve_user(request, session)
    if user:
        ws = team_svc.ensure_workspace(session, user)
        return team_svc.Actor(kind="owner", user=user, workspace=ws)
    return None


async def ensure_meta_token_fresh(session: Session, user: User) -> User:
    """Refresh Meta long-lived token before it expires (~60 days)."""
    if not user.fb_token_enc:
        return user
    threshold = datetime.utcnow() + timedelta(days=settings.TOKEN_REFRESH_DAYS)
    if user.token_expires_at and user.token_expires_at > threshold:
        return user
    try:
        token = crypto.decrypt(user.fb_token_enc)
        refreshed = await meta.long_lived_token(token)
        user.fb_token_enc = crypto.encrypt(refreshed["access_token"])
        expires_in = refreshed.get("expires_in")
        user.token_expires_at = (
            datetime.utcnow() + timedelta(seconds=expires_in) if expires_in else None
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    except Exception:
        pass
    return user


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
        expires_in = long.get("expires_in")

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

        team_svc.ensure_workspace(session, user)
        touch_session(request, user.id)
        response = RedirectResponse(settings.FRONTEND_URL + "/")
        attach_remember_cookie(response, user.id)
        return response
    except Exception:
        return _auth_error("meta_exchange_failed")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    response = JSONResponse({"ok": True})
    clear_remember_cookie(response)
    return response


@router.get("/invite/{token}/preview")
def invite_preview(token: str, session: Session = Depends(get_session)):
    return team_svc.invite_preview(session, token)


@router.post("/invite/{token}/accept")
def invite_accept(
    token: str,
    request: Request,
    session: Session = Depends(get_session),
    name: str = Query(""),
):
    member = team_svc.accept_invite(session, token, name)
    touch_member_session(request, member.id)
    response = JSONResponse({"ok": True, "name": member.name, "role": "member"})
    attach_member_remember_cookie(response, member.id)
    return response
