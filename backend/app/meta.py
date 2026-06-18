"""
Thin async client over the Meta Marketing API.
All calls go through the user's stored token — we never expose the app secret
to the browser, and the token never leaves the server after login.
"""
import httpx
from .config import settings

BASE = f"https://graph.facebook.com/{settings.META_API_VERSION}"
PURCHASE_TYPES = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]


def login_dialog_url(state: str) -> str:
    """URL we redirect the user to so they grant access on facebook.com."""
    return (
        f"https://www.facebook.com/{settings.META_API_VERSION}/dialog/oauth"
        f"?client_id={settings.META_APP_ID}"
        f"&redirect_uri={settings.META_REDIRECT_URI}"
        f"&state={state}"
        f"&scope={settings.META_SCOPES}"
    )


async def exchange_code_for_token(code: str) -> dict:
    """Step 1: turn the OAuth code into a short-lived token (server-side)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/oauth/access_token", params={
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "redirect_uri": settings.META_REDIRECT_URI,
            "code": code,
        })
        r.raise_for_status()
        return r.json()


async def long_lived_token(short_token: str) -> dict:
    """Step 2: upgrade to a ~60-day token. (System-user tokens don't expire.)"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "fb_exchange_token": short_token,
        })
        r.raise_for_status()
        return r.json()


async def me(token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/me", params={"fields": "id,name,email", "access_token": token})
        r.raise_for_status()
        return r.json()


async def list_ad_accounts(token: str) -> list[dict]:
    out, url = [], f"{BASE}/me/adaccounts"
    params = {"fields": "account_id,name,currency,account_status", "limit": 200, "access_token": token}
    async with httpx.AsyncClient(timeout=30) as c:
        while url:
            r = await c.get(url, params=params)
            r.raise_for_status()
            j = r.json()
            out += j.get("data", [])
            url = j.get("paging", {}).get("next")
            params = None  # 'next' is a full URL
    return out


async def insights(token: str, account_id: str, date_preset: str = "last_30d") -> list[dict]:
    account_id = account_id.replace("act_", "")
    fields = ("ad_id,ad_name,spend,impressions,clicks,ctr,cpc,cpm,reach,frequency,"
              "actions,action_values,purchase_roas")
    out, url = [], f"{BASE}/act_{account_id}/insights"
    params = {"level": "ad", "fields": fields, "date_preset": date_preset,
              "limit": 300, "access_token": token}
    async with httpx.AsyncClient(timeout=60) as c:
        while url:
            r = await c.get(url, params=params)
            r.raise_for_status()
            j = r.json()
            out += j.get("data", [])
            url = j.get("paging", {}).get("next")
            params = None
    return out


async def creative_thumbs(token: str, account_id: str) -> dict:
    """Map ad_id -> thumbnail url."""
    account_id = account_id.replace("act_", "")
    thumbs, url = {}, f"{BASE}/act_{account_id}/ads"
    params = {"fields": "id,creative{thumbnail_url,image_url}", "limit": 300, "access_token": token}
    async with httpx.AsyncClient(timeout=60) as c:
        while url:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                break
            j = r.json()
            for ad in j.get("data", []):
                cr = ad.get("creative") or {}
                thumbs[ad["id"]] = cr.get("thumbnail_url") or cr.get("image_url")
            url = j.get("paging", {}).get("next")
            params = None
    return thumbs


def _find_action(arr, types):
    if not isinstance(arr, list):
        return 0.0
    for t in types:
        for x in arr:
            if x.get("action_type") == t:
                try:
                    return float(x.get("value", 0))
                except (TypeError, ValueError):
                    return 0.0
    return 0.0


def normalize(rows: list[dict], thumbs: dict) -> list[dict]:
    out = []
    for r in rows:
        spend = float(r.get("spend", 0) or 0)
        if spend <= 0:
            continue
        purchases = _find_action(r.get("actions"), PURCHASE_TYPES)
        pvalue = _find_action(r.get("action_values"), PURCHASE_TYPES)
        roas = None
        pr = r.get("purchase_roas")
        if isinstance(pr, list) and pr:
            try:
                roas = float(pr[0].get("value", 0))
            except (TypeError, ValueError):
                roas = None
        elif pvalue > 0:
            roas = pvalue / spend
        out.append({
            "name": r.get("ad_name") or "Untitled ad",
            "thumb": thumbs.get(r.get("ad_id")),
            "spend": spend,
            "impressions": int(float(r.get("impressions", 0) or 0)),
            "clicks": int(float(r.get("clicks", 0) or 0)),
            "ctr": float(r.get("ctr", 0) or 0),
            "cpc": float(r.get("cpc", 0) or 0),
            "cpm": float(r.get("cpm", 0) or 0),
            "purchases": purchases,
            "pvalue": pvalue,
            "roas": roas,
        })
    return out
