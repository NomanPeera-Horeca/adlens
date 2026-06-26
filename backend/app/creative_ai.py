"""
Creative analysis grounded in account winners + optional GPT-4o vision.

Set OPENAI_API_KEY in Render for image review. Without it, recommendations
still compare to your stored winner library from past syncs.
"""
import json
import logging

import httpx

from .config import settings
from .winners import format_peers_for_prompt

log = logging.getLogger(__name__)

_PROMPT = """You review Meta ad creatives for a B2B restaurant/hotel equipment dealer (The Horeca Store).

Ad goal: {goal}
Product category: {category}
THIS creative's performance: {calls} calls, {lpv} landing page views, CTR {ctr}%, spend ${spend}

YOUR ACCOUNT'S PROVEN WINNERS (same category + goal — real past performance, not generic advice):
{peers}

Look at THIS image. Compare it to the winners above — layout, product count, CTA, text, format.
Return JSON only:
{{"visual":"what this image shows in ≤12 words","hook":"main message in ≤10 words","fit":"strong|mixed|weak for the goal","like_winner":"exact winner ad name to copy OR null","diff":"what this lacks vs winners in ≤25 words","tip":"one specific next step based on winners in ≤25 words"}}"""


def heuristic_insight(asset: dict, ad: dict) -> dict:
    goal = (ad.get("goal") or {}).get("label") or "Phone calls"
    cat = (ad.get("category") or {}).get("label") or "Commercial equipment"
    v = asset.get("verdict") or {}
    vk = v.get("key", "data")
    fit = "strong" if vk == "scale" else "weak" if vk == "kill" else "mixed"
    metric = (ad.get("goal") or {}).get("metric") or "calls"
    count = int(asset.get(metric) or 0)
    peers = ad.get("peer_winners") or []

    tip = (v.get("action") or "Monitor performance")[:120]
    like_winner = None
    diff = ""

    if peers and vk in ("kill", "watch", "data"):
        best = peers[0]
        pc = best.get("primary_cost")
        cost_s = f"${pc:.2f}" if pc else "strong cost"
        like_winner = best.get("ad_name") or best.get("name")
        diff = best.get("insight_visual") or "proven layout and hook"
        tip = (
            f"Model after your winner \"{like_winner}\" "
            f"({best.get('primary_count', 0)} {best.get('primary_metric', 'results')} at {cost_s}). "
            f"Match its format: {diff[:60]}."
        )

    return {
        "visual": f"{cat} product shot",
        "hook": asset.get("name") or "Untitled creative",
        "fit": fit,
        "like_winner": like_winner,
        "diff": diff,
        "tip": tip[:200],
        "ai": False,
        "grounded": bool(peers),
        "summary": f"{fit.title()} for {goal.lower()} · {count} {metric.replace('_', ' ')}",
    }


async def _vision_analyze(image_url: str, prompt: str) -> dict | None:
    if not settings.OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
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
                    "max_tokens": 260,
                    "response_format": {"type": "json_object"},
                },
            )
            if r.status_code != 200:
                log.warning("Vision API %s: %s", r.status_code, r.text[:200])
                return None
            text = r.json()["choices"][0]["message"]["content"]
            data = json.loads(text)
            data["ai"] = True
            data["grounded"] = True
            lw = data.get("like_winner")
            data["summary"] = (
                f"Like \"{lw}\"" if lw else data.get("fit", "mixed").title()
            ) + f" · {data.get('visual', '')[:35]}"
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
    peers = format_peers_for_prompt(ad.get("peer_winners") or [])
    prompt = _PROMPT.format(
        goal=goal,
        category=cat,
        calls=int(asset.get("calls") or 0),
        lpv=int(asset.get("landing_views") or 0),
        ctr=float(asset.get("ctr") or 0),
        spend=float(asset.get("spend") or 0),
        peers=peers,
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
