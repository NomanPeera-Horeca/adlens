"""
AdLens API.

Routes:
  GET  /api/me                       -> current user + plan
  GET  /api/accounts                 -> the user's Meta ad accounts
  GET  /api/insights?account=&range= -> scored creatives (cached; pass refresh=1 to force)
  POST /api/billing/checkout         -> Stripe checkout (stub until keys set)
  POST /api/billing/webhook          -> Stripe subscription events
  GET  /deauthorize, /data-deletion  -> Meta-required compliance callbacks

The frontend (static SPA) is served from / so the whole thing is one deploy.
"""
from fastapi import FastAPI, Request, Depends, HTTPException, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select

from .config import settings
from .db import init_db, get_session
from .models import User
from . import meta, crypto, sync, winners
from . import meta_compliance
from . import admin
from .dates import FREE_RANGES, PRO_ONLY_RANGES, resolve_date_query
from .auth import (
    router as auth_router,
    resolve_user,
    resolve_actor,
    ensure_meta_token_fresh,
    REMEMBER_COOKIE,
    MEMBER_REMEMBER_COOKIE,
)
from . import team as team_svc
from .team_models import TeamMember

app = FastAPI(title=settings.APP_NAME)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    same_site="lax",
    https_only=(settings.ENV == "prod"),
    max_age=settings.SESSION_MAX_AGE,
)
app.add_middleware(CORSMiddleware, allow_origins=[settings.FRONTEND_URL],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth_router)


@app.on_event("startup")
def _startup():
    init_db()


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = resolve_user(request, session)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


async def current_actor(request: Request, session: Session = Depends(get_session)):
    actor = resolve_actor(request, session)
    if not actor:
        raise HTTPException(401, "Not signed in")
    actor.user = await ensure_meta_token_fresh(session, actor.user)
    return actor


async def current_user_fresh(request: Request, session: Session = Depends(get_session)) -> User:
    actor = await current_actor(request, session)
    return actor.user


def owner_token(actor) -> str:
    if not actor.user.fb_token_enc:
        raise HTTPException(400, "No Meta connection")
    return crypto.decrypt(actor.user.fb_token_enc)


def user_token(user: User) -> str:
    if not user.fb_token_enc:
        raise HTTPException(400, "No Meta connection")
    return crypto.decrypt(user.fb_token_enc)


# ----------------------------- API -----------------------------

@app.get("/api/me")
async def api_me(request: Request, session: Session = Depends(get_session)):
    actor = resolve_actor(request, session)
    if not actor:
        return {
            "authenticated": False,
            "returning": bool(
                request.cookies.get(REMEMBER_COOKIE) or request.cookies.get(MEMBER_REMEMBER_COOKIE)
            ),
        }
    actor.user = await ensure_meta_token_fresh(session, actor.user)
    user = actor.user
    base = {
        "authenticated": True,
        "name": actor.display_name,
        "email": user.email if actor.is_owner else (actor.member.email if actor.member else ""),
        "plan": user.plan,
        "is_admin": admin.is_admin(user),
        "effective_plan": admin.effective_plan(user),
        "connected": bool(user.fb_token_enc),
        "ai_vision": bool(settings.OPENAI_API_KEY),
        "ai_model": settings.OPENAI_VISION_MODEL if settings.OPENAI_API_KEY else None,
        "role": "owner" if actor.is_owner else "member",
        "can_invite": actor.is_owner,
        "can_refresh": True,
        "workspace": actor.workspace.name if actor.workspace else "",
        "owner_name": user.name if not actor.is_owner else None,
    }
    if not actor.is_owner and actor.allowed_accounts is not None:
        base["allowed_accounts"] = actor.allowed_accounts
    return base


@app.get("/api/team")
async def api_team_list(session: Session = Depends(get_session), actor=Depends(current_actor)):
    if not actor.is_owner:
        raise HTTPException(403, "Only the workspace owner can manage team.")
    ws = team_svc.ensure_workspace(session, actor.user)
    members = session.exec(select(TeamMember).where(TeamMember.workspace_id == ws.id)).all()
    return {"members": [team_svc.member_to_dict(m) for m in members]}


@app.post("/api/team/invite")
async def api_team_invite(
    request: Request,
    session: Session = Depends(get_session),
    actor=Depends(current_actor),
):
    if not actor.is_owner:
        raise HTTPException(403, "Only the workspace owner can invite teammates.")
    body = await request.json()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    account_ids = body.get("account_ids") or []
    ws = team_svc.ensure_workspace(session, actor.user)
    member = team_svc.create_invite(session, ws, name=name, email=email, account_ids=account_ids)
    return team_svc.member_to_dict(member)


@app.delete("/api/team/members/{member_id}")
async def api_team_remove(member_id: int, session: Session = Depends(get_session), actor=Depends(current_actor)):
    if not actor.is_owner:
        raise HTTPException(403, "Only the workspace owner can remove teammates.")
    ws = team_svc.ensure_workspace(session, actor.user)
    member = session.get(TeamMember, member_id)
    if not member or member.workspace_id != ws.id:
        raise HTTPException(404, "Member not found.")
    session.delete(member)
    session.commit()
    return {"ok": True}


@app.get("/api/accounts")
async def api_accounts(actor=Depends(current_actor)):
    try:
        accounts = await meta.list_ad_accounts(owner_token(actor))
        return {"accounts": team_svc.filter_accounts(accounts, actor)}
    except Exception as e:
        raise HTTPException(502, f"Meta error: {e}")


@app.get("/api/ad-image")
async def api_ad_image(
    account: str = Query(...),
    ad: str = Query(...),
    actor=Depends(current_actor),
):
    team_svc.assert_account_access(actor, account)
    token = owner_token(actor)
    try:
        result = await meta.ad_image_source(token, ad, account_id=account)
    except Exception as e:
        raise HTTPException(502, f"Meta error: {e}")
    if not result:
        raise HTTPException(404, "No image for this ad")
    data, ctype = result
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


def _insights_response(payload: dict, effective_range: str, requested_range: str,
                       cached: bool, synced_at, stale=False, error="", range_limited=False):
    ads = payload["ads"]
    campaigns = payload["campaigns"]
    out = {
        "ads": ads,
        "campaigns": campaigns,
        "count": len(ads),
        "campaign_count": len(campaigns),
        "range": effective_range,
        "requested_range": requested_range,
        "range_limited": range_limited or effective_range != requested_range,
        "cached": cached,
        "synced_at": synced_at.isoformat() + "Z" if synced_at else None,
    }
    if stale:
        out["stale"] = True
        out["error"] = error
    return out


def _resolve_insights_range(range_key: str, since: str, until: str) -> tuple[str, dict]:
    try:
        return resolve_date_query(range_key, since, until)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/insights")
async def api_insights(
    account: str = Query(...),
    range: str = Query("last_30d"),
    since: str = Query(""),
    until: str = Query(""),
    refresh: bool = Query(False),
    session: Session = Depends(get_session),
    actor=Depends(current_actor),
):
    team_svc.assert_account_access(actor, account)
    user = actor.user
    requested_range = range if range != "custom" else f"custom:{since}:{until}"
    cache_key, date_query = _resolve_insights_range(range, since, until)
    effective_key, effective_query = cache_key, date_query
    plan = admin.effective_plan(user)
    range_limited = False
    if plan == "free" and (range in PRO_ONLY_RANGES or range not in FREE_RANGES):
        effective_key, effective_query = resolve_date_query("last_30d")
        range_limited = effective_key != cache_key

    cached = sync.get_cached(session, user.id, account, effective_key)
    if cached and sync.is_fresh(cached, plan) and not refresh:
        payload = sync.payload_from_run(cached)
        winners.attach_peer_winners(session, user.id, account, payload["ads"])
        return _insights_response(payload, effective_key, requested_range, True, cached.synced_at,
                                  stale=False, error="", range_limited=range_limited)

    token = owner_token(actor)
    try:
        run = await sync.fetch_and_store(session, user.id, account, effective_key, effective_query, token)
    except Exception as e:
        if cached and cached.status == "success":
            payload = sync.payload_from_run(cached)
            winners.attach_peer_winners(session, user.id, account, payload["ads"])
            return _insights_response(payload, effective_key, requested_range, True,
                                      cached.synced_at, stale=True, error=str(e),
                                      range_limited=range_limited)
        raise HTTPException(502, f"Meta error: {e}")

    payload = sync.payload_from_run(run)
    winners.attach_peer_winners(session, user.id, account, payload["ads"])
    return _insights_response(payload, effective_key, requested_range, False, run.synced_at,
                              range_limited=range_limited)


# ----------------------------- Billing -----------------------------

@app.post("/api/billing/checkout")
def checkout(user: User = Depends(current_user)):
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(501, "Billing not configured. Set STRIPE_SECRET_KEY + STRIPE_PRICE_ID.")
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    s = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{settings.FRONTEND_URL}/?upgraded=1",
        cancel_url=f"{settings.FRONTEND_URL}/",
        customer_email=user.email or None,
        client_reference_id=str(user.id),
    )
    return {"url": s.url}


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)):
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(501, "Webhook not configured")
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Invalid signature")

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        user_id = sess.get("client_reference_id")
        if user_id:
            user = session.get(User, int(user_id))
            if user and not admin.is_admin(user):
                user.plan = "pro"
                user.stripe_customer_id = sess.get("customer")
                user.stripe_subscription_id = sess.get("subscription")
                session.add(user)
                session.commit()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        if sub_id:
            user = session.exec(select(User).where(User.stripe_subscription_id == sub_id)).first()
            if user and not admin.is_admin(user):
                user.plan = "free"
                user.stripe_subscription_id = None
                session.add(user)
                session.commit()

    return {"ok": True}


# ----------------------------- Meta compliance (required for App Review) -----

def _wipe_user_by_fb_id(session: Session, fb_user_id: str) -> None:
    user = session.exec(select(User).where(User.fb_user_id == fb_user_id)).first()
    if not user:
        return
    user.fb_token_enc = ""
    user.token_expires_at = None
    user.name = ""
    user.email = ""
    session.add(user)
    session.commit()


@app.post("/deauthorize")
def deauthorize(signed_request: str = Form(default=""), session: Session = Depends(get_session)):
    data = meta_compliance.parse_signed_request(signed_request, settings.META_APP_SECRET)
    if data and data.get("user_id"):
        _wipe_user_by_fb_id(session, str(data["user_id"]))
    return {"ok": True}


@app.get("/deauthorize")
def deauthorize_get():
    return {"ok": True, "message": "Meta deauthorize callback. POST with signed_request in production."}


@app.post("/data-deletion")
def data_deletion(signed_request: str = Form(default=""), session: Session = Depends(get_session)):
    data = meta_compliance.parse_signed_request(signed_request, settings.META_APP_SECRET)
    fb_user_id = str(data["user_id"]) if data and data.get("user_id") else "unknown"
    if data and data.get("user_id"):
        _wipe_user_by_fb_id(session, str(data["user_id"]))
    code = f"adlens-del-{fb_user_id}"
    return {
        "url": f"{settings.BASE_URL}/data-deletion/status?code={code}",
        "confirmation_code": code,
    }


@app.get("/data-deletion")
def data_deletion_info():
    return {
        "url": f"{settings.BASE_URL}/data-deletion",
        "confirmation_code": "adlens-del",
        "message": "Meta data-deletion callback. POST with signed_request in production.",
    }


@app.get("/data-deletion/status")
def data_deletion_status(code: str = Query("")):
    return {"status": "completed", "confirmation_code": code or "adlens-del"}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ----------------------------- Serve frontend -----------------------------
# Keep this LAST so it doesn't shadow /api routes.
import os


def _frontend_dir() -> str | None:
    here = os.path.dirname(__file__)
    for rel in ("../frontend", "../../frontend"):
        path = os.path.abspath(os.path.join(here, rel))
        if os.path.isfile(os.path.join(path, "index.html")):
            return path
    return None


_frontend = _frontend_dir()
if _frontend:
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
else:
    @app.get("/")
    def missing_frontend():
        raise HTTPException(
            503,
            "Dashboard files missing on server. Redeploy with frontend bundled in backend/frontend/.",
        )
