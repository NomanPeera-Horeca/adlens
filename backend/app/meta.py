"""
Thin async client over the Meta Marketing API.
All calls go through the user's stored token — we never expose the app secret
to the browser, and the token never leaves the server after login.
"""
import asyncio
import json
import re
from datetime import datetime, timezone

import httpx

from .config import settings
from .dates import insights_params

BASE = f"https://graph.facebook.com/{settings.META_API_VERSION}"
PURCHASE_TYPES = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]

# Meta reports the same outcome under several action_type names — pick the best count per group.
CONVERSION_GROUPS: dict[str, list[str]] = {
    "calls": [
        "click_to_call_native_60s_call_connect",
        "click_to_call_native_20s_call_connect",
        "click_to_call_call_confirm",
        "click_to_call_native_call_placed",
        "phone_call",
    ],
    "leads": [
        "lead",
        "onsite_conversion.lead_grouped",
        "offsite_conversion.fb_pixel_lead",
        "leadgen_grouped",
    ],
    "contacts": [
        "contact",
        "contact_total",
        "onsite_conversion.contact",
    ],
    "landing_views": [
        "landing_page_view",
        "omni_landing_page_view",
    ],
    "link_clicks": ["link_click"],
    "registrations": [
        "complete_registration",
        "offsite_conversion.fb_pixel_complete_registration",
    ],
    "add_to_cart": [
        "add_to_cart",
        "offsite_conversion.fb_pixel_add_to_cart",
    ],
    "checkouts": [
        "initiate_checkout",
        "offsite_conversion.fb_pixel_initiate_checkout",
    ],
    "messages": [
        "onsite_conversion.messaging_conversation_started_7d",
        "onsite_conversion.messaging_first_reply",
    ],
}
RESULT_PRIORITY = ("calls", "leads", "purchases", "contacts", "landing_views", "registrations")
CREATIVE_FIELDS = "id,image_hash,image_url,thumbnail_url,object_story_spec,asset_feed_spec"
AD_DETAIL_FIELDS = (
    f"id,created_time,updated_time,effective_status,adset{{start_time}},creative{{{CREATIVE_FIELDS}}}"
)
STATUS_LABELS = {
    "ACTIVE": "Live",
    "PAUSED": "Paused",
    "CAMPAIGN_PAUSED": "Campaign paused",
    "ADSET_PAUSED": "Ad set paused",
    "ARCHIVED": "Archived",
    "DELETED": "Deleted",
    "DISAPPROVED": "Disapproved",
    "PENDING_REVIEW": "In review",
    "WITH_ISSUES": "Has issues",
}
# Campaigns/ads in these states should appear even before insights show spend.
LIVE_DELIVERY_STATUSES = ["ACTIVE", "PENDING_REVIEW", "WITH_ISSUES", "PREAPPROVED"]
PAUSED_DELIVERY_STATUSES = ["PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED"]
VISIBLE_CAMPAIGN_STATUSES = LIVE_DELIVERY_STATUSES + ["PAUSED"]
VISIBLE_AD_STATUSES = LIVE_DELIVERY_STATUSES + PAUSED_DELIVERY_STATUSES
PREVIEW_FORMATS = (
    "DESKTOP_FEED_STANDARD",
    "MOBILE_FEED_STANDARD",
    "INSTAGRAM_STANDARD",
    "INSTAGRAM_STORY",
)
MIN_IMAGE_PIXELS = 90_000  # ~300×300 — reject tiny Meta thumbs when we can


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


async def insights(token: str, account_id: str, date_query: dict | None = None) -> list[dict]:
    account_id = account_id.replace("act_", "")
    date_query = date_query or {"date_preset": "last_30d"}
    fields = ("campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,"
              "spend,impressions,clicks,ctr,cpc,cpm,reach,frequency,"
              "actions,action_values,purchase_roas")
    out, url = [], f"{BASE}/act_{account_id}/insights"
    params = {"level": "ad", "fields": fields, "limit": 500, "access_token": token}
    params.update(insights_params(date_query))
    async with httpx.AsyncClient(timeout=60) as c:
        while url:
            r = await c.get(url, params=params)
            r.raise_for_status()
            j = r.json()
            out += j.get("data", [])
            url = j.get("paging", {}).get("next")
            params = None
    return out


async def asset_insights(
    token: str,
    account_id: str,
    date_query: dict | None = None,
    *,
    breakdown: str = "image_asset",
) -> list[dict]:
    """Per-creative metrics for dynamic / multi-asset ads (Meta asset breakdown)."""
    account_id = account_id.replace("act_", "")
    date_query = date_query or {"date_preset": "last_30d"}
    fields = "ad_id,ad_name,campaign_name,spend,impressions,clicks,actions"
    url = f"{BASE}/act_{account_id}/insights"
    params = {
        "level": "ad",
        "breakdowns": breakdown,
        "fields": fields,
        "limit": 500,
        "access_token": token,
    }
    params.update(insights_params(date_query))
    try:
        return await _paginate(token, url, params)
    except httpx.HTTPStatusError:
        return []


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
    """Live, in-review, and paused campaigns visible in Ads Manager."""
    account_id = account_id.replace("act_", "")
    url = f"{BASE}/act_{account_id}/campaigns"
    params = {
        "fields": "id,name,effective_status,status,created_time,updated_time",
        "limit": 500,
        "access_token": token,
        "filtering": json.dumps([{
            "field": "effective_status",
            "operator": "IN",
            "value": VISIBLE_CAMPAIGN_STATUSES,
        }]),
    }
    return await _paginate(token, url, params)


async def list_live_ads(token: str, account_id: str) -> list[dict]:
    """Live, in-review, and paused ads — includes zero-spend ads missing from insights."""
    account_id = account_id.replace("act_", "")
    url = f"{BASE}/act_{account_id}/ads"
    params = {
        "fields": "id,name,effective_status,created_time,updated_time,campaign{id,name},adset{id,name}",
        "limit": 500,
        "access_token": token,
        "filtering": json.dumps([{
            "field": "effective_status",
            "operator": "IN",
            "value": VISIBLE_AD_STATUSES,
        }]),
    }
    return await _paginate(token, url, params)


async def campaign_insights(token: str, account_id: str, date_query: dict | None = None) -> list[dict]:
    account_id = account_id.replace("act_", "")
    date_query = date_query or {"date_preset": "last_30d"}
    fields = ("campaign_id,campaign_name,spend,impressions,clicks,ctr,cpc,cpm,"
              "actions,action_values,purchase_roas")
    url = f"{BASE}/act_{account_id}/insights"
    params = {"level": "campaign", "fields": fields, "limit": 500, "access_token": token}
    params.update(insights_params(date_query))
    return await _paginate(token, url, params)


def _metrics_from_insight(row: dict | None) -> dict:
    row = row or {}
    spend = float(row.get("spend", 0) or 0)
    conv = _conversion_fields(row)
    roas = None
    pr = row.get("purchase_roas")
    pvalue = conv["pvalue"]
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
        "roas": roas,
        **conv,
    }


def merge_active_campaigns(catalog: list[dict], insight_rows: list[dict]) -> list[dict]:
    """Visible campaigns now + any campaign with delivery in the selected period."""
    by_id = {r["campaign_id"]: r for r in insight_rows if r.get("campaign_id")}
    seen: set[str] = set()
    out: list[dict] = []

    def append(cid: str, name: str, status: str, row: dict | None, updated_time: str | None = None):
        metrics = _metrics_from_insight(row)
        status_updated = _parse_meta_time(updated_time)
        out.append({
            "campaign_id": cid,
            "name": name or "Untitled campaign",
            "status": status,
            "status_label": _status_label(status),
            "is_delivering": _is_delivering(status),
            "status_updated": status_updated.isoformat() if status_updated else None,
            **metrics,
        })

    for c in catalog:
        cid = c["id"]
        seen.add(cid)
        row = by_id.get(cid)
        status = c.get("effective_status") or "ACTIVE"
        append(
            cid,
            c.get("name") or (row or {}).get("campaign_name"),
            status,
            row,
            c.get("updated_time"),
        )

    for cid, row in by_id.items():
        if cid in seen:
            continue
        imp = int(float(row.get("impressions") or 0))
        spend = float(row.get("spend") or 0)
        if imp > 0 or spend > 0:
            append(cid, row.get("campaign_name"), "PAUSED", row)

    out.sort(key=lambda x: (-x["spend"], x["name"].lower()))
    return out


def _fbcdn_variants(url: str) -> list[str]:
    """Return URL variants from smallest thumbnail to largest plausible CDN size."""
    if not url.startswith("http"):
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str):
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    add(url)

    def upsize(match: re.Match) -> str:
        w, h = int(match.group(3)), int(match.group(4))
        if max(w, h) >= 600:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}1200x1200"

    add(re.sub(r"([/_-])([ps])(\d+)x(\d+)", upsize, url))
    add(re.sub(r"([/_-])[ps]\d+x\d+", "", url))
    add(re.sub(r"stp=dst-jpg_[ps]\d+x\d+(_q\d+)?", r"stp=dst-jpg_s1200x1200\1", url))
    add(re.sub(r"([?&](?:width|w)=)\d+", r"\g<1>1200", url))
    add(re.sub(r"([?&](?:height|h)=)\d+", r"\g<1>1200", url))
    return out


def _dimensions_from_url(url: str) -> tuple[int, int] | None:
    for pat in (
        r"[/_-][ps](\d+)x(\d+)",
        r"stp=dst-jpg_[ps](\d+)x(\d+)",
        r"[?&](?:width|w)=(\d+).*?[?&](?:height|h)=(\d+)",
    ):
        m = re.search(pat, url)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC2):
                return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
            if marker == 0xD9:
                break
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            i += 2 + seg_len
    return None


def _image_score(data: bytes, url: str) -> tuple[int, int, int]:
    dims = _dimensions_from_bytes(data) or _dimensions_from_url(url)
    if dims:
        return dims[0] * dims[1], dims[0], dims[1]
    return len(data), 0, 0


def _collect_image_hashes(creative: dict) -> list[str]:
    if not creative:
        return []
    hashes: list[str] = []

    def add(val):
        if val and val not in hashes:
            hashes.append(str(val))

    add(creative.get("image_hash"))
    oss = creative.get("object_story_spec") or {}
    for block in (oss.get("link_data"), oss.get("photo_data"), oss.get("video_data")):
        if isinstance(block, dict):
            add(block.get("image_hash"))
    for att in (oss.get("link_data") or {}).get("child_attachments") or []:
        add(att.get("image_hash"))
    for img in (creative.get("asset_feed_spec") or {}).get("images") or []:
        add(img.get("hash"))
    return hashes


def _dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        for variant in _fbcdn_variants(url):
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


def _collect_creative_urls(creative: dict, *, allow_thumbnail: bool = True) -> list[str]:
    """Gather every image URL on a creative, best candidates first."""
    if not creative:
        return []
    urls: list[str] = []
    seen: set[str] = set()

    def add(val):
        if isinstance(val, str) and val.startswith("http"):
            for variant in _fbcdn_variants(val):
                if variant not in seen:
                    seen.add(variant)
                    urls.append(variant)

    for key in ("image_url", "thumbnail_url") if allow_thumbnail else ("image_url",):
        add(creative.get(key))
    oss = creative.get("object_story_spec") or {}
    for block in (oss.get("link_data"), oss.get("video_data"), oss.get("photo_data")):
        if not isinstance(block, dict):
            continue
        for key in ("picture", "image_url", "url"):
            add(block.get(key))
    for att in (oss.get("link_data") or {}).get("child_attachments") or []:
        for key in ("picture", "image_url"):
            add(att.get(key))
    for img in (creative.get("asset_feed_spec") or {}).get("images") or []:
        add(img.get("url"))
    return urls


def _pick_creative_url(creative: dict, *, allow_thumbnail: bool = True) -> str | None:
    urls = _collect_creative_urls(creative, allow_thumbnail=allow_thumbnail)
    return urls[0] if urls else None


async def _fetch_bytes(c: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    try:
        img = await c.get(url)
    except httpx.HTTPError:
        return None
    if img.status_code != 200:
        return None
    ctype = img.headers.get("content-type") or "image/jpeg"
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    return img.content, ctype


async def _best_image_from_urls(
    c: httpx.AsyncClient, urls: list[str], *, min_pixels: int = 0,
) -> tuple[str, bytes, str, int, int] | None:
    """Download candidates; pick sharpest by pixel area, not file size."""
    best: tuple[str, bytes, str, int, int] | None = None
    best_score = 0
    tried: set[str] = set()
    for url in _dedupe_urls(urls):
        if url in tried:
            continue
        tried.add(url)
        got = await _fetch_bytes(c, url)
        if not got:
            continue
        data, ctype = got
        score, w, h = _image_score(data, url)
        if score < min_pixels:
            continue
        if score > best_score:
            best_score = score
            best = (url, data, ctype, w, h)
    return best


async def _library_urls_from_hashes(
    c: httpx.AsyncClient, token: str, account_id: str, hashes: list[str],
) -> list[str]:
    if not hashes or not account_id:
        return []
    account_id = account_id.replace("act_", "")
    r = await c.get(
        f"{BASE}/act_{account_id}/adimages",
        params={
            "hashes": json.dumps(hashes),
            "fields": "hash,url,permalink_url,width,height",
            "access_token": token,
        },
    )
    if r.status_code != 200:
        return []
    rows = r.json().get("data") or []
    rows.sort(key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0), reverse=True)
    urls: list[str] = []
    for row in rows:
        for key in ("url", "permalink_url"):
            val = row.get(key)
            if isinstance(val, str) and val.startswith("http"):
                urls.append(val)
    return urls


async def _preview_image_urls(
    c: httpx.AsyncClient, token: str, ad_id: str, ad_format: str,
) -> list[str]:
    r = await c.get(
        f"{BASE}/{ad_id}/previews",
        params={"ad_format": ad_format, "access_token": token},
    )
    if r.status_code != 200:
        return []
    urls: list[str] = []
    for item in r.json().get("data") or []:
        body = item.get("body") or ""
        for match in re.findall(r"""(?:src|data-src)=["']([^"']+)["']""", body):
            url = match.replace("&amp;", "&")
            if url.startswith("http") and "facebook.com/tr?" not in url:
                urls.append(url)
    return urls


async def _load_creative(c: httpx.AsyncClient, token: str, ad_id: str) -> dict:
    r = await c.get(f"{BASE}/{ad_id}", params={"fields": AD_DETAIL_FIELDS, "access_token": token})
    if r.status_code != 200:
        return {}
    ad = r.json()
    creative = ad.get("creative") or {}
    cid = creative.get("id")
    if cid and not _collect_image_hashes(creative) and not creative.get("image_url"):
        cr = await c.get(
            f"{BASE}/{cid}",
            params={"fields": CREATIVE_FIELDS, "access_token": token},
        )
        if cr.status_code == 200:
            creative = cr.json()
    return creative


async def _resolve_ad_image(
    c: httpx.AsyncClient, token: str, ad_id: str, account_id: str, creative: dict | None = None,
) -> dict | None:
    """Return {url, width, height, data, content_type} for the sharpest available image."""
    creative = creative or await _load_creative(c, token, ad_id)
    if not creative:
        return None

    url_candidates: list[str] = []
    url_candidates.extend(await _library_urls_from_hashes(
        c, token, account_id, _collect_image_hashes(creative),
    ))
    url_candidates.extend(_collect_creative_urls(creative, allow_thumbnail=False))
    for fmt in PREVIEW_FORMATS:
        url_candidates.extend(await _preview_image_urls(c, token, ad_id, fmt))
    url_candidates.extend(_collect_creative_urls(creative, allow_thumbnail=True))

    best = await _best_image_from_urls(c, url_candidates, min_pixels=MIN_IMAGE_PIXELS)
    if not best:
        best = await _best_image_from_urls(c, url_candidates, min_pixels=0)
    if not best:
        return None

    url, data, ctype, w, h = best
    return {"url": url, "width": w, "height": h, "data": data, "content_type": ctype}


async def ad_image_source(token: str, ad_id: str, account_id: str = "") -> tuple[bytes, str] | None:
    """Fetch highest-res creative image bytes server-side."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        resolved = await _resolve_ad_image(c, token, ad_id, account_id)
        if not resolved:
            return None
        return resolved["data"], resolved["content_type"]


async def _best_image_url_fast(
    c: httpx.AsyncClient, token: str, ad_id: str, account_id: str, creative: dict,
) -> str | None:
    """Resolve best image URL without downloading bytes — safe during insights sync."""
    cid = creative.get("id")
    if cid and not _collect_image_hashes(creative) and not creative.get("image_url"):
        cr = await c.get(
            f"{BASE}/{cid}",
            params={"fields": CREATIVE_FIELDS, "access_token": token},
        )
        if cr.status_code == 200:
            creative = cr.json()

    library = await _library_urls_from_hashes(
        c, token, account_id, _collect_image_hashes(creative),
    )
    if library:
        return library[0]

    urls = _dedupe_urls(_collect_creative_urls(creative, allow_thumbnail=False))
    if urls:
        return urls[0]

    previews = await _preview_image_urls(c, token, ad_id, PREVIEW_FORMATS[0])
    if previews:
        return previews[0]

    urls = _dedupe_urls(_collect_creative_urls(creative, allow_thumbnail=True))
    return urls[0] if urls else None


async def _fetch_one_ad_meta(
    c: httpx.AsyncClient, token: str, ad_id: str, account_id: str,
) -> tuple[str, dict] | None:
    r = await c.get(
        f"{BASE}/{ad_id}",
        params={"fields": AD_DETAIL_FIELDS, "access_token": token},
    )
    if r.status_code != 200:
        return None
    ad = r.json()
    creative = ad.get("creative") or {}
    image_url = await _best_image_url_fast(c, token, ad_id, account_id, creative)
    return ad_id, {
        "thumb": image_url or _pick_creative_url(creative),
        "created_time": ad.get("created_time"),
        "updated_time": ad.get("updated_time"),
        "effective_status": ad.get("effective_status"),
        "adset_start": (ad.get("adset") or {}).get("start_time"),
    }


async def fetch_ads_meta(token: str, account_id: str, ad_ids: list[str] | None = None) -> dict[str, dict]:
    """Per-ad metadata: created time, delivery status, best-effort image URL (no byte downloads)."""
    account_id = account_id.replace("act_", "")
    out: dict[str, dict] = {}
    ids = [str(i) for i in (ad_ids or []) if i]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        if ids:
            sem = asyncio.Semaphore(8)

            async def bound(ad_id: str):
                async with sem:
                    return await _fetch_one_ad_meta(c, token, ad_id, account_id)

            for result in await asyncio.gather(*[bound(i) for i in ids], return_exceptions=True):
                if isinstance(result, Exception) or not result:
                    continue
                ad_id, info = result
                out[ad_id] = info
            return out
        url = f"{BASE}/act_{account_id}/ads"
        params = {"fields": AD_DETAIL_FIELDS, "limit": 500, "access_token": token}
        while url:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                break
            j = r.json()
            page_ids = [ad["id"] for ad in j.get("data", [])]
            sem = asyncio.Semaphore(8)

            async def bound(ad_id: str):
                async with sem:
                    return await _fetch_one_ad_meta(c, token, ad_id, account_id)

            for result in await asyncio.gather(*[bound(i) for i in page_ids], return_exceptions=True):
                if isinstance(result, Exception) or not result:
                    continue
                ad_id, info = result
                out[ad_id] = info
            url = j.get("paging", {}).get("next")
            params = None
    return out


async def creative_thumbs(token: str, account_id: str, ad_ids: list[str] | None = None) -> dict:
    """Map ad_id -> direct CDN url (fallback; prefer /api/ad-image proxy in normalize)."""
    meta = await fetch_ads_meta(token, account_id, ad_ids)
    return {ad_id: info["thumb"] for ad_id, info in meta.items() if info.get("thumb")}


def _pick_action(actions, types: list[str]) -> float:
    """Highest count among Meta aliases for one outcome (avoids double-counting)."""
    if not isinstance(actions, list):
        return 0.0
    vals: list[float] = []
    for t in types:
        for x in actions:
            if x.get("action_type") == t:
                try:
                    vals.append(float(x.get("value", 0)))
                except (TypeError, ValueError):
                    pass
    return max(vals) if vals else 0.0


def _find_action(arr, types):
    return _pick_action(arr, types)


def _cost_per(spend: float, count: float) -> float | None:
    if spend > 0 and count > 0:
        return round(spend / count, 2)
    return None


def _conversion_fields(row: dict) -> dict:
    actions = row.get("actions")
    out: dict = {}
    for key, types in CONVERSION_GROUPS.items():
        out[key] = int(_pick_action(actions, types))

    purchases = int(_pick_action(actions, PURCHASE_TYPES))
    pvalue = _find_action(row.get("action_values"), PURCHASE_TYPES)
    spend = float(row.get("spend", 0) or 0)

    out["purchases"] = purchases
    out["pvalue"] = pvalue
    out["cost_per_lead"] = _cost_per(spend, out["leads"])
    out["cost_per_call"] = _cost_per(spend, out["calls"])
    out["cost_per_contact"] = _cost_per(spend, out["contacts"])
    out["cost_per_lpv"] = _cost_per(spend, out["landing_views"])

    out["primary_result"] = None
    out["primary_count"] = 0
    out["cost_per_result"] = None
    counts = {"purchases": purchases, **{k: out[k] for k in CONVERSION_GROUPS}}
    for field in RESULT_PRIORITY:
        if counts.get(field, 0) > 0:
            out["primary_result"] = field
            out["primary_count"] = int(counts[field])
            out["cost_per_result"] = _cost_per(spend, counts[field])
            break
    return out


def _parse_meta_time(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("+0000", "+00:00"))
    except ValueError:
        return None


def _status_label(status: str) -> str:
    if not status:
        return "Unknown"
    return STATUS_LABELS.get(status, status.replace("_", " ").title())


def _is_delivering(status: str) -> bool:
    return status == "ACTIVE"


def _live_since(info: dict) -> datetime | None:
    """Best estimate of when this ad was created / scheduled to start."""
    created = _parse_meta_time(info.get("created_time"))
    start = _parse_meta_time(info.get("adset_start"))
    if created and start:
        return max(created, start)
    return created or start


def _days_live(since: datetime | None) -> int | None:
    if not since:
        return None
    now = datetime.now(timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return max(0, (now.date() - since.astimezone(timezone.utc).date()).days)


def _asset_key(row: dict, breakdown: str) -> str | None:
    block = row.get(breakdown) or {}
    if isinstance(block, dict):
        for key in ("hash", "id", "video_id"):
            val = block.get(key)
            if val:
                return str(val)
    return None


def _asset_thumb(row: dict, breakdown: str) -> str | None:
    block = row.get(breakdown) or {}
    if not isinstance(block, dict):
        return None
    for key in ("url", "thumbnail_url", "permalink_url"):
        val = block.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


def _normalize_asset_row(row: dict, breakdown: str) -> dict | None:
    spend = float(row.get("spend", 0) or 0)
    if spend <= 0:
        return None
    asset_id = _asset_key(row, breakdown)
    if not asset_id:
        return None
    impressions = int(float(row.get("impressions", 0) or 0))
    clicks = int(float(row.get("clicks", 0) or 0))
    ctr = round(clicks / impressions * 100, 2) if impressions > 0 else 0.0
    conv = _conversion_fields(row)
    block = row.get(breakdown) or {}
    out = {
        "asset_id": asset_id,
        "asset_type": breakdown.replace("_asset", ""),
        "name": block.get("name") or asset_id[:12],
        "thumb": _asset_thumb(row, breakdown),
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": ctr,
        "cpc": round(spend / clicks, 2) if clicks > 0 else None,
        **conv,
    }
    if breakdown == "video_asset" and isinstance(block, dict):
        vid = block.get("video_id") or block.get("id")
        if vid:
            out["video_id"] = str(vid)
    return out


async def resolve_asset_videos(token: str, ads: list[dict]) -> None:
    """Fetch playable video URLs from Meta for video-type creative assets."""
    video_ids: set[str] = set()
    for ad in ads:
        for asset in ad.get("assets") or []:
            if asset.get("asset_type") == "video" and asset.get("video_id"):
                video_ids.add(str(asset["video_id"]))
    if not video_ids:
        return

    resolved: dict[str, dict] = {}

    async def fetch_one(c: httpx.AsyncClient, vid: str) -> None:
        r = await c.get(
            f"{BASE}/{vid}",
            params={"fields": "source,picture,title", "access_token": token},
        )
        if r.status_code != 200:
            return
        j = r.json()
        pic = j.get("picture")
        if isinstance(pic, str):
            thumb = pic
        elif isinstance(pic, dict):
            thumb = pic.get("data", {}).get("url") if isinstance(pic.get("data"), dict) else None
        else:
            thumb = None
        resolved[vid] = {"source": j.get("source"), "thumb": thumb, "title": j.get("title")}

    async with httpx.AsyncClient(timeout=30) as c:
        sem = asyncio.Semaphore(6)

        async def bound(vid: str):
            async with sem:
                await fetch_one(c, vid)

        await asyncio.gather(*[bound(v) for v in video_ids])

    for ad in ads:
        for asset in ad.get("assets") or []:
            vid = asset.get("video_id")
            if not vid or vid not in resolved:
                continue
            info = resolved[vid]
            if info.get("source"):
                asset["video_url"] = info["source"]
            if not asset.get("thumb") and info.get("thumb"):
                asset["thumb"] = info["thumb"]
            if info.get("title") and (asset.get("name") or "").startswith((asset.get("asset_id") or "")[:8]):
                asset["name"] = info["title"]


def group_asset_insights(rows: list[dict]) -> dict[str, list[dict]]:
    """Group Meta asset breakdown rows by ad_id, merging duplicate image hashes."""
    by_ad: dict[str, list[dict]] = {}
    for row in rows:
        ad_id = row.get("ad_id")
        if not ad_id:
            continue
        breakdown = "image_asset" if row.get("image_asset") else "video_asset" if row.get("video_asset") else None
        if not breakdown:
            continue
        asset = _normalize_asset_row(row, breakdown)
        if not asset:
            continue
        by_ad.setdefault(str(ad_id), []).append(asset)
    return {ad_id: _dedupe_assets(assets) for ad_id, assets in by_ad.items()}


_METRIC_SUM_KEYS = (
    "spend", "impressions", "clicks", "calls", "leads", "contacts",
    "landing_views", "messages", "purchases", "pvalue",
)


def _dedupe_assets(assets: list[dict]) -> list[dict]:
    """Club identical creatives (same Meta image/video hash) into one row."""
    by_id: dict[str, dict] = {}
    for a in assets:
        key = a["asset_id"]
        if key in by_id:
            ex = by_id[key]
            for k in _METRIC_SUM_KEYS:
                ex[k] = (ex.get(k) or 0) + (a.get(k) or 0)
            ex["variant_count"] = ex.get("variant_count", 1) + 1
            if not ex.get("thumb") and a.get("thumb"):
                ex["thumb"] = a["thumb"]
        else:
            by_id[key] = {**a, "variant_count": 1}
    out = [_finalize_asset(ex) for ex in by_id.values()]
    out.sort(key=lambda x: (-x["spend"], -x.get("calls", 0)))
    return out


def _finalize_asset(asset: dict) -> dict:
    imp = int(asset.get("impressions") or 0)
    clicks = int(asset.get("clicks") or 0)
    spend = float(asset.get("spend") or 0)
    asset["ctr"] = round(clicks / imp * 100, 2) if imp else 0.0
    asset["cpc"] = round(spend / clicks, 2) if clicks else None
    asset["cost_per_call"] = _cost_per(spend, asset.get("calls") or 0)
    asset["cost_per_lead"] = _cost_per(spend, asset.get("leads") or 0)
    asset["cost_per_lpv"] = _cost_per(spend, asset.get("landing_views") or 0)
    return asset


def attach_assets(ads: list[dict], asset_rows: list[dict]) -> list[dict]:
    by_ad = group_asset_insights(asset_rows)
    for ad in ads:
        ad_id = str(ad.get("ad_id") or "")
        ad["assets"] = by_ad.get(ad_id, [])
    return ads


async def fetch_asset_insights(token: str, account_id: str, date_query: dict | None = None) -> list[dict]:
    """Image + video asset breakdowns merged (best-effort — some ads only support one)."""
    image_rows = await asset_insights(token, account_id, date_query, breakdown="image_asset")
    video_rows = await asset_insights(token, account_id, date_query, breakdown="video_asset")
    return image_rows + video_rows


def _ad_thumb(ad_id: str, info: dict, account_id: str) -> str | None:
    fast_url = info.get("thumb")
    if isinstance(fast_url, str) and fast_url.startswith("http"):
        return fast_url
    if ad_id and account_id:
        return f"/api/ad-image?account={account_id}&ad={ad_id}"
    return None


def _stub_ad_from_live(row: dict, info: dict, account_id: str) -> dict:
    ad_id = str(row.get("id") or "")
    campaign = row.get("campaign") if isinstance(row.get("campaign"), dict) else {}
    adset = row.get("adset") if isinstance(row.get("adset"), dict) else {}
    merged_info = {**info, "created_time": row.get("created_time") or info.get("created_time")}
    since = _live_since(merged_info)
    days = _days_live(since)
    status = row.get("effective_status") or info.get("effective_status") or ""
    status_updated = _parse_meta_time(row.get("updated_time") or info.get("updated_time"))
    return {
        "ad_id": ad_id,
        "name": row.get("name") or info.get("name") or "Untitled ad",
        "campaign": campaign.get("name") or "Unknown campaign",
        "adset": adset.get("name") or "",
        "thumb": _ad_thumb(ad_id, info, account_id),
        "status": status,
        "status_label": _status_label(status),
        "is_delivering": _is_delivering(status),
        "status_updated": status_updated.isoformat() if status_updated else None,
        "live_since": since.isoformat() if since else None,
        "days_live": days,
        "spend": 0.0,
        "impressions": 0,
        "clicks": 0,
        "ctr": 0.0,
        "cpc": 0.0,
        "cpm": 0.0,
        "roas": None,
        "calls": 0,
        "leads": 0,
        "purchases": 0,
        "pvalue": 0.0,
        "contacts": 0,
        "landing_views": 0,
        "messages": 0,
        "registrations": 0,
    }


def merge_live_ads(ads: list[dict], live_rows: list[dict], ad_meta: dict, account_id: str) -> list[dict]:
    """Add live/in-review ads that have no insights spend yet (common for same-day launches)."""
    account_id = account_id.replace("act_", "")
    by_id = {str(a.get("ad_id")): a for a in ads if a.get("ad_id")}
    for row in live_rows:
        ad_id = str(row.get("id") or "")
        if not ad_id or ad_id in by_id:
            continue
        by_id[ad_id] = _stub_ad_from_live(row, ad_meta.get(ad_id) or {}, account_id)
    out = list(by_id.values())
    out.sort(key=lambda x: (-float(x.get("spend") or 0), (x.get("name") or "").lower()))
    return out


def normalize(rows: list[dict], ad_meta: dict, account_id: str = "") -> list[dict]:
    account_id = account_id.replace("act_", "")
    out = []
    for r in rows:
        spend = float(r.get("spend", 0) or 0)
        if spend <= 0:
            continue
        ad_id = r.get("ad_id")
        conv = _conversion_fields(r)
        roas = None
        pr = r.get("purchase_roas")
        pvalue = conv["pvalue"]
        if isinstance(pr, list) and pr:
            try:
                roas = float(pr[0].get("value", 0))
            except (TypeError, ValueError):
                roas = None
        elif pvalue > 0:
            roas = pvalue / spend
        thumb = None
        info = ad_meta.get(ad_id) or {}
        fast_url = info.get("thumb")
        if isinstance(fast_url, str) and fast_url.startswith("http"):
            thumb = fast_url
        elif ad_id and account_id:
            thumb = f"/api/ad-image?account={account_id}&ad={ad_id}"
        since = _live_since(info)
        days = _days_live(since)
        status = info.get("effective_status") or ""
        status_updated = _parse_meta_time(info.get("updated_time"))
        out.append({
            "ad_id": ad_id,
            "name": r.get("ad_name") or "Untitled ad",
            "campaign": r.get("campaign_name") or "Unknown campaign",
            "adset": r.get("adset_name") or "",
            "thumb": thumb,
            "status": status,
            "status_label": _status_label(status),
            "is_delivering": _is_delivering(status),
            "status_updated": status_updated.isoformat() if status_updated else None,
            "live_since": since.isoformat() if since else None,
            "days_live": days,
            "spend": spend,
            "impressions": int(float(r.get("impressions", 0) or 0)),
            "clicks": int(float(r.get("clicks", 0) or 0)),
            "ctr": float(r.get("ctr", 0) or 0),
            "cpc": float(r.get("cpc", 0) or 0),
            "cpm": float(r.get("cpm", 0) or 0),
            "roas": roas,
            **conv,
        })
    return out
