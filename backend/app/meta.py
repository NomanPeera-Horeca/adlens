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
    params = {
        "fields": "account_id,name,currency,timezone_name,account_status,business_name",
        "limit": 200,
        "access_token": token,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        while url:
            r = await c.get(url, params=params)
            r.raise_for_status()
            j = r.json()
            out += j.get("data", [])
            url = j.get("paging", {}).get("next")
            params = None
    # USD accounts first, then name — makes USA vs UAE easy to spot.
    out.sort(key=lambda a: (0 if a.get("currency") == "USD" else 1, (a.get("name") or "").lower()))
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


def _pick_creative_url(creative: dict, *, allow_thumbnail: bool = True) -> str | None:
    """Return best direct CDN url. Prefer full image_url over tiny thumbnail."""
    if not creative:
        return None
    keys = ("image_url", "thumbnail_url") if allow_thumbnail else ("image_url",)
    for key in keys:
        url = creative.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    oss = creative.get("object_story_spec") or {}
    for block in (oss.get("link_data"), oss.get("video_data"), oss.get("photo_data")):
        if not isinstance(block, dict):
            continue
        for key in ("picture", "image_url", "url"):
            url = block.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return url
    for att in (oss.get("link_data") or {}).get("child_attachments") or []:
        for key in ("picture", "image_url"):
            url = att.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


async def _fetch_bytes(c: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    img = await c.get(url)
    if img.status_code != 200:
        return None
    ctype = img.headers.get("content-type") or "image/jpeg"
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    return img.content, ctype


async def _full_image_from_hash(c: httpx.AsyncClient, token: str, account_id: str, image_hash: str) -> str | None:
    account_id = account_id.replace("act_", "")
    r = await c.get(f"{BASE}/act_{account_id}/adimages", params={
        "hashes": f'["{image_hash}"]',
        "fields": "url,permalink_url,width,height",
        "access_token": token,
    })
    if r.status_code != 200:
        return None
    images = r.json().get("data") or []
    if not images:
        return None
    best = max(images, key=lambda x: int(x.get("width") or 0))
    return best.get("url") or best.get("permalink_url")


async def ad_image_source(token: str, ad_id: str, account_id: str = "") -> tuple[bytes, str] | None:
    """Fetch highest-res creative image bytes server-side."""
    fields = "creative{id,image_url,thumbnail_url,image_hash,object_story_spec}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(f"{BASE}/{ad_id}", params={"fields": fields, "access_token": token})
        if r.status_code != 200:
            return None
        creative = r.json().get("creative") or {}

        # Full-size library image via hash (best quality for single-image ads).
        if account_id and creative.get("image_hash"):
            lib_url = await _full_image_from_hash(c, token, account_id, creative["image_hash"])
            if lib_url:
                got = await _fetch_bytes(c, lib_url)
                if got:
                    return got

        img_url = _pick_creative_url(creative, allow_thumbnail=False)
        if img_url:
            got = await _fetch_bytes(c, img_url)
            if got:
                return got

        img_url = _pick_creative_url(creative, allow_thumbnail=True)
        if img_url:
            return await _fetch_bytes(c, img_url)
    return None


async def creative_thumbs(token: str, account_id: str, ad_ids: list[str] | None = None) -> dict:
    """Map ad_id -> direct CDN url (fallback; prefer /api/ad-image proxy in normalize)."""
    account_id = account_id.replace("act_", "")
    thumbs: dict[str, str] = {}
    fields = "id,creative{thumbnail_url,image_url,object_story_spec}"
    ids = [str(i) for i in (ad_ids or []) if i]
    async with httpx.AsyncClient(timeout=60) as c:
        if ids:
            for ad_id in ids:
                r = await c.get(f"{BASE}/{ad_id}", params={"fields": fields, "access_token": token})
                if r.status_code != 200:
                    continue
                ad = r.json()
                img = _pick_creative_url(ad.get("creative") or {})
                if img:
                    thumbs[ad_id] = img
            return thumbs
        url = f"{BASE}/act_{account_id}/ads"
        params = {"fields": fields, "limit": 500, "access_token": token}
        while url:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                break
            j = r.json()
            for ad in j.get("data", []):
                img = _pick_creative_url(ad.get("creative") or {})
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


def normalize(rows: list[dict], thumbs: dict, account_id: str = "") -> list[dict]:
    account_id = account_id.replace("act_", "")
    out = []
    for r in rows:
        spend = float(r.get("spend", 0) or 0)
        if spend <= 0:
            continue
        ad_id = r.get("ad_id")
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
        thumb = None
        if ad_id and account_id:
            thumb = f"/api/ad-image?account={account_id}&ad={ad_id}"
        elif ad_id and thumbs.get(ad_id):
            thumb = thumbs[ad_id]
        out.append({
            "ad_id": ad_id,
            "name": r.get("ad_name") or "Untitled ad",
            "campaign": r.get("campaign_name") or "Unknown campaign",
            "adset": r.get("adset_name") or "",
            "thumb": thumb,
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
