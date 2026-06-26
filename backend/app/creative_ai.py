"""
Optional vision analysis for ad creatives.

Set OPENAI_API_KEY in Render to enable AI image review. Without it, AdLens
still returns a short heuristic insight from metrics + category + goal.
"""
import json
import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

_PROMPT = """You review Meta ad creatives for a B2B restaurant/hotel equipment dealer.
Ad goal: {goal}
Product category: {category}
Creative performance: {calls} calls, {lpv} landing page views, CTR {ctr}%, spend ${spend}

Look at the image. Return JSON only:
{{"visual":"what the image shows in ≤12 words","hook":"main message or angle in ≤10 words","fit":"strong|mixed|weak for the stated goal","tip":"one specific action in ≤20 words"}}"""


def heuristic_insight(asset: dict, ad: dict) -> dict:
    goal = (ad.get("goal") or {}).get("label") or "Phone calls"
    cat = (ad.get("category") or {}).get("label") or "Commercial equipment"
    v = asset.get("verdict") or {}
    vk = v.get("key", "data")
    fit = "strong" if vk == "scale" else "weak" if vk == "kill" else "mixed"
    metric = (ad.get("goal") or {}).get("metric") or "calls"
    count = int(asset.get(metric) or 0)
    return {
        "visual": f"{cat} product shot",
        "hook": asset.get("name") or "Untitled creative",
        "fit": fit,
        "tip": (v.get("action") or "Monitor performance")[:120],
        "ai": False,
        "summary": f"{fit.title()} for {goal.lower()} · {count} {metric.replace('_', ' ')}",
    }


async def _vision_analyze(image_url: str, prompt: str) -> dict | None:
    if not settings.OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": settings.OPENAI_VISION_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                        ],
                    }],
                    "max_tokens": 180,
                    "response_format": {"type": "json_object"},
                },
            )
            if r.status_code != 200:
                log.warning("Vision API %s: %s", r.status_code, r.text[:200])
                return None
            text = r.json()["choices"][0]["message"]["content"]
            data = json.loads(text)
            data["ai"] = True
            data["summary"] = f"{data.get('fit', 'mixed').title()} for goal · {data.get('visual', '')[:40]}"
            return data
    except Exception as e:
        log.warning("Vision analyze failed: %s", e)
        return None


async def analyze_asset(asset: dict, ad: dict) -> dict:
    base = heuristic_insight(asset, ad)
    thumb = asset.get("thumb")
    if not thumb or not str(thumb).startswith("http"):
        return base

    goal = (ad.get("goal") or {}).get("label") or "Phone calls"
    cat = (ad.get("category") or {}).get("label") or "Commercial equipment"
    prompt = _PROMPT.format(
        goal=goal,
        category=cat,
        calls=int(asset.get("calls") or 0),
        lpv=int(asset.get("landing_views") or 0),
        ctr=float(asset.get("ctr") or 0),
        spend=float(asset.get("spend") or 0),
    )
    vision = await _vision_analyze(thumb, prompt)
    return vision or base


async def enrich_ads_with_insights(ads: list[dict]) -> None:
    """Attach insight to top-spend unique creatives (vision if API key set)."""
    candidates: list[tuple[dict, dict]] = []
    for ad in ads:
        for asset in ad.get("assets") or []:
            if asset.get("thumb") and float(asset.get("spend") or 0) >= 20:
                candidates.append((ad, asset))
    candidates.sort(key=lambda x: -float(x[1].get("spend") or 0))
    limit = settings.AI_VISION_LIMIT

    for ad, asset in candidates[:limit]:
        asset["insight"] = await analyze_asset(asset, ad)

    for ad in ads:
        for asset in ad.get("assets") or []:
            if "insight" not in asset:
                asset["insight"] = heuristic_insight(asset, ad)
