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
        f"&redirect_uri={settings.meta_redirect_uri}"
        f"&state={state}"
        f"&scope={settings.META_SCOPES}"
    )


async def exchange_code_for_token(code: str) -> dict:
    """Step 1: turn the OAuth code into a short-lived token (server-side)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/oauth/access_token", params={
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "redirect_uri": settings.meta_redirect_uri,
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
    fields = ("campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,"
              "spend,impressions,clicks,ctr,cpc,cpm,reach,frequency,"
              "actions,action_values,purchase_roas")
    out, url = [], f"{BASE}/act_{account_id}/insights"
    params = {"level": "ad", "fields": fields, "date_preset": date_preset,
              "limit": 500, "access_token": token}
    async with httpx.AsyncClient(timeout=60) as c:
        while url:
            r = await c.get(url, params=params)
            r.raise_for_status()
            j = r.json()
            out += j.get("data", [])
            url = j.get("paging", {}).get("next")
            params = None
    return out


async def _paginate(token: str, url: str, params: dict) -> list[dict]:
    out = []
    async with httpx.AsyncClient(timeout=60) as c:
        while url:
            r = await c.get(url, params=params)
            r.raise_for_status()
            j = r.json()
            out += j.get("data", [])
            url = j.get("paging", {}).get("next")
            params = None
    return out


async def list_active_campaigns(token: str, account_id: str) -> list[dict]:
    """All campaigns currently in ACTIVE delivery state."""
    account_id = account_id.replace("act_", "")
    url = f"{BASE}/act_{account_id}/campaigns"
    params = {
        "fields": "id,name,effective_status,status",
        "limit": 500,
        "access_token": token,
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
    }
    return await _paginate(token, url, params)


async def campaign_insights(token: str, account_id: str, date_preset: str = "last_30d") -> list[dict]:
    account_id = account_id.replace("act_", "")
    fields = ("campaign_id,campaign_name,spend,impressions,clicks,ctr,cpc,cpm,"
              "actions,action_values,purchase_roas")
    url = f"{BASE}/act_{account_id}/insights"
    params = {"level": "campaign", "fields": fields, "date_preset": date_preset,
              "limit": 500, "access_token": token}
    return await _paginate(token, url, params)


def _metrics_from_insight(row: dict | None) -> dict:
    row = row or {}
    spend = float(row.get("spend", 0) or 0)
    purchases = _find_action(row.get("actions"), PURCHASE_TYPES)
    pvalue = _find_action(row.get("action_values"), PURCHASE_TYPES)
    roas = None
    pr = row.get("purchase_roas")
    if isinstance(pr, list) and pr:
        try:
            roas = float(pr[0].get("value", 0))
        except (TypeError, ValueError):
            roas = None
    elif pvalue > 0 and spend > 0:
        roas = pvalue / spend
    return {
        "spend": spend,
        "impressions": int(float(row.get("impressions", 0) or 0)),
        "clicks": int(float(row.get("clicks", 0) or 0)),
        "ctr": float(row.get("ctr", 0) or 0),
        "cpc": float(row.get("cpc", 0) or 0),
        "cpm": float(row.get("cpm", 0) or 0),
        "purchases": purchases,
        "pvalue": pvalue,
        "roas": roas,
    }


def merge_active_campaigns(catalog: list[dict], insight_rows: list[dict]) -> list[dict]:
    """ACTIVE campaigns now + any campaign with delivery in the selected period."""
    by_id = {r["campaign_id"]: r for r in insight_rows if r.get("campaign_id")}
    seen: set[str] = set()
    out: list[dict] = []

    def append(cid: str, name: str, status: str, row: dict | None):
        metrics = _metrics_from_insight(row)
        out.append({
            "campaign_id": cid,
            "name": name or "Untitled campaign",
            "status": status,
            **metrics,
        })

    for c in catalog:
        cid = c["id"]
        seen.add(cid)
        row = by_id.get(cid)
        append(cid, c.get("name") or (row or {}).get("campaign_name"), c.get("effective_status") or "ACTIVE", row)

    for cid, row in by_id.items():
        if cid in seen:
            continue
        imp = int(float(row.get("impressions") or 0))
        spend = float(row.get("spend") or 0)
        if imp > 0 or spend > 0:
            append(cid, row.get("campaign_name"), "PAUSED", row)

    out.sort(key=lambda x: (-x["spend"], x["name"].lower()))
    return out
    """Meta CDN preview URLs embed tiny dimensions — bump to a usable size."""
    if not url:
        return None
    for small, large in (
        ("p64x64", "p720x720"),
        ("p130x130", "p720x720"),
        ("s64x64", "s720x720"),
        ("s130x130", "s720x720"),
    ):
        if small in url:
            return url.replace(small, large)
    return url


def _best_creative_image(creative: dict) -> str | None:
    """Prefer full image URLs over Meta's tiny thumbnail_url previews."""
    if not creative:
        return None
    candidates: list[str] = []

    def add(val):
        if isinstance(val, str) and val.startswith("http"):
            candidates.append(val)

    add(creative.get("image_url"))
    oss = creative.get("object_story_spec") or {}
    link = oss.get("link_data") or {}
    add(link.get("picture"))
    add(link.get("image_url"))
    for att in link.get("child_attachments") or []:
        add(att.get("picture"))
        add(att.get("image_url"))
    video = oss.get("video_data") or {}
    add(video.get("image_url"))
    photo = oss.get("photo_data") or {}
    add(photo.get("url"))
    add(creative.get("thumbnail_url"))

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        upscaled = _upscale_cdn_url(url)
        if upscaled:
            return upscaled
    return None


async def creative_thumbs(token: str, account_id: str) -> dict:
    """Map ad_id -> best available preview image url."""
    account_id = account_id.replace("act_", "")
    thumbs, url = {}, f"{BASE}/act_{account_id}/ads"
    fields = (
        "id,creative{thumbnail_url,image_url,"
        "object_story_spec{link_data{picture,image_url,child_attachments{picture,image_url}},"
        "video_data{image_url},photo_data{url}}}"
    )
    params = {"fields": fields, "limit": 500, "access_token": token}
    async with httpx.AsyncClient(timeout=60) as c:
        while url:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                break
            j = r.json()
            for ad in j.get("data", []):
                img = _best_creative_image(ad.get("creative") or {})
                if img:
                    thumbs[ad["id"]] = img
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
            "ad_id": r.get("ad_id"),
            "name": r.get("ad_name") or "Untitled ad",
            "campaign": r.get("campaign_name") or "Unknown campaign",
            "adset": r.get("adset_name") or "",
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
