"""
AdLens API.

Routes:
  GET  /api/me                       -> current user + plan
  GET  /api/accounts                 -> the user's Meta ad accounts
  GET  /api/insights?account=&range= -> scored creatives (the dashboard payload)
  POST /api/billing/checkout         -> Stripe checkout (stub until keys set)
  GET  /deauthorize, /data-deletion  -> Meta-required compliance callbacks

The frontend (static SPA) is served from / so the whole thing is one deploy.
"""
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session

from .config import settings
from .db import init_db, get_session
from .models import User
from . import meta, crypto, scoring
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
            "plan": user.plan, "connected": bool(user.fb_token_enc)}


@app.get("/api/accounts")
async def api_accounts(user: User = Depends(current_user)):
    try:
        return {"accounts": await meta.list_ad_accounts(user_token(user))}
    except Exception as e:
        raise HTTPException(502, f"Meta error: {e}")


@app.get("/api/insights")
async def api_insights(
    account: str = Query(...),
    range: str = Query("last_30d"),
    user: User = Depends(current_user),
):
    # Gate by plan: free tier sees limited window, pro sees everything.
    if user.plan == "free" and range in ("last_90d", "maximum"):
        range = "last_30d"
    token = user_token(user)
    try:
        rows = await meta.insights(token, account, range)
        thumbs = await meta.creative_thumbs(token, account)
    except Exception as e:
        raise HTTPException(502, f"Meta error: {e}")
    ads = scoring.score_all(meta.normalize(rows, thumbs))
    return {"ads": ads, "count": len(ads), "range": range}


# ----------------------------- Billing (stub) -----------------------------

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


# ----------------------------- Meta compliance (required for App Review) -----

@app.get("/deauthorize")
def deauthorize():
    # Meta pings this when a user removes your app. Wipe their token here.
    return {"ok": True}


@app.get("/data-deletion")
def data_deletion():
    # Meta requires a user-data deletion endpoint. Return a confirmation URL/code.
    return {"url": f"{settings.BASE_URL}/data-deletion", "confirmation_code": "adlens-del"}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ----------------------------- Serve frontend -----------------------------
# Keep this LAST so it doesn't shadow /api routes.
import os
_frontend = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
