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
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select

from .config import settings
from .db import init_db, get_session
from .models import User
from . import meta, crypto, sync
from . import meta_compliance
from . import admin
from .auth import router as auth_router

app = FastAPI(title=settings.APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET,
                   same_site="lax", https_only=(settings.ENV == "prod"))
app.add_middleware(CORSMiddleware, allow_origins=[settings.FRONTEND_URL],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth_router)


@app.on_event("startup")
def _startup():
    init_db()


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Not signed in")
    user = session.get(User, uid)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def user_token(user: User) -> str:
    if not user.fb_token_enc:
        raise HTTPException(400, "No Meta connection")
    return crypto.decrypt(user.fb_token_enc)


# ----------------------------- API -----------------------------

@app.get("/api/me")
def api_me(request: Request, session: Session = Depends(get_session)):
    uid = request.session.get("user_id")
    if not uid:
        return {"authenticated": False}
    user = session.get(User, uid)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "name": user.name, "email": user.email,
            "plan": user.plan, "is_admin": admin.is_admin(user),
            "effective_plan": admin.effective_plan(user),
            "connected": bool(user.fb_token_enc)}


@app.get("/api/accounts")
async def api_accounts(user: User = Depends(current_user)):
    try:
        return {"accounts": await meta.list_ad_accounts(user_token(user))}
    except Exception as e:
        raise HTTPException(502, f"Meta error: {e}")


def _insights_response(payload: dict, effective_range: str, requested_range: str,
                       cached: bool, synced_at, stale=False, error=""):
    ads = payload["ads"]
    campaigns = payload["campaigns"]
    out = {
        "ads": ads,
        "campaigns": campaigns,
        "count": len(ads),
        "campaign_count": len(campaigns),
        "range": effective_range,
        "requested_range": requested_range,
        "range_limited": effective_range != requested_range,
        "cached": cached,
        "synced_at": synced_at.isoformat() + "Z" if synced_at else None,
    }
    if stale:
        out["stale"] = True
        out["error"] = error
    return out


@app.get("/api/insights")
async def api_insights(
    account: str = Query(...),
    range: str = Query("last_30d"),
    refresh: bool = Query(False),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    requested_range = range
    effective_range = range
    plan = admin.effective_plan(user)
    # Gate by plan: free tier sees limited window, pro/admin sees everything.
    if plan == "free" and range in ("last_90d", "maximum"):
        effective_range = "last_30d"

    cached = sync.get_cached(session, user.id, account, effective_range)
    if cached and sync.is_fresh(cached, plan) and not refresh:
        payload = sync.payload_from_run(cached)
        return _insights_response(payload, effective_range, requested_range, True, cached.synced_at)

    token = user_token(user)
    try:
        run = await sync.fetch_and_store(session, user.id, account, effective_range, token)
    except Exception as e:
        if cached and cached.status == "success":
            payload = sync.payload_from_run(cached)
            return _insights_response(payload, effective_range, requested_range, True,
                                      cached.synced_at, stale=True, error=str(e))
        raise HTTPException(502, f"Meta error: {e}")

    payload = sync.payload_from_run(run)
    return _insights_response(payload, effective_range, requested_range, False, run.synced_at)


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
