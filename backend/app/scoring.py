"""
Creative scoring with explicit actions.

Call-heavy accounts: weigh cost/call, call volume, CTR, spend, and product category.
High-ticket categories (walk-in) compare to category peers — weak CTR triggers
creative refresh, not automatic kill.
"""
from statistics import mean

from .categories import categorize

DEFAULT_RULES = {
    "target_roas": 2.0,
    "kill_roas": 1.0,
    "min_spend": 60.0,
    "kill_spend": 150.0,
}


def _avg_cpa(ads: list[dict], count_key: str) -> float:
    vals = [
        a["spend"] / a[count_key]
        for a in ads
        if a.get(count_key, 0) > 0 and a.get("spend", 0) > 0
    ]
    return mean(vals) if vals else 0.0


def _avg_calls(ads: list[dict]) -> float:
    vals = [a.get("calls") or 0 for a in ads if (a.get("calls") or 0) > 0]
    return mean(vals) if vals else 0.0


def _scoring_mode(ads: list[dict]) -> str:
    if any((a.get("purchases") or 0) > 0 or (a.get("pvalue") or 0) > 0 for a in ads):
        return "roas"
    if any((a.get("calls") or 0) > 0 for a in ads):
        return "calls"
    if any((a.get("leads") or 0) > 0 for a in ads):
        return "leads"
    if any((a.get("contacts") or 0) > 0 for a in ads):
        return "contacts"
    return "engagement"


def _pack(key: str, label: str, score: float, action: str, reasons: list[str]) -> dict:
    return {"key": key, "label": label, "score": score, "action": action, "reasons": reasons}


def _category_context(ads: list[dict], rules: dict) -> dict[str, dict]:
    judged = [a for a in ads if a["spend"] >= rules["min_spend"]]
    by_cat: dict[str, list[dict]] = {}
    for a in judged:
        key = (a.get("category") or {}).get("key") or "general"
        by_cat.setdefault(key, []).append(a)

    out: dict[str, dict] = {}
    for key, peers in by_cat.items():
        if len(peers) < 2:
            continue
        out[key] = {
            "avg_cpcall": _avg_cpa(peers, "calls"),
            "avg_ctr": mean([a["ctr"] for a in peers if a.get("ctr")]),
            "avg_calls": _avg_calls(peers),
            "count": len(peers),
        }
    return out


def _resolve_call_benchmark(ad: dict, ctx: dict) -> tuple[float, float, float, str]:
    """Return avg cost/call, avg CTR, avg calls, benchmark source label."""
    cat = ad.get("category") or {}
    cat_key = cat.get("key") or "general"
    cat_ctx = (ctx.get("by_category") or {}).get(cat_key)
    mult = float(cat.get("cpa_multiplier") or 1.0)

    avg_cpcall = ctx["avg_cpcall"]
    avg_ctr = ctx["avg_ctr"]
    avg_calls = ctx["avg_calls"]
    source = "account"

    if cat_ctx:
        if cat_ctx.get("avg_cpcall"):
            avg_cpcall = cat_ctx["avg_cpcall"]
        if cat_ctx.get("avg_ctr"):
            avg_ctr = cat_ctx["avg_ctr"]
        if cat_ctx.get("avg_calls"):
            avg_calls = cat_ctx["avg_calls"]
        source = f"{cat.get('label', cat_key)} peers"

    # High-ticket lines tolerate higher $/call even vs category peers.
    if mult > 1.0 and avg_cpcall:
        avg_cpcall = avg_cpcall * mult

    return avg_cpcall, avg_ctr, avg_calls, source


def _verdict_calls(ad: dict, ctx: dict, rules: dict) -> dict:
    calls = int(ad.get("calls") or 0)
    spend = ad["spend"]
    ctr = float(ad.get("ctr") or 0)
    cpcall = ad.get("cost_per_call") or (round(spend / calls, 2) if calls else None)
    cat = ad.get("category") or {}
    high_ticket = bool(cat.get("high_ticket"))
    avg_cpcall, avg_ctr, avg_calls, bench = _resolve_call_benchmark(ad, ctx)
    reasons: list[str] = []
    score = 0.0

    if cat.get("label"):
        reasons.append(f"{cat['label']} · ~${cat.get('ticket_usd', 0):,} ticket")

    if calls == 0:
        if spend >= rules["kill_spend"]:
            action = (
                "Pause this ad and reallocate budget — no calls recorded in this period."
                if not high_ticket
                else "No calls yet at meaningful spend. Refresh creative and audience before pausing the walk-in line."
            )
            return _pack("kill", "Kill", -200, action, [f"${spend:,.0f} spent with zero calls in this period"])
        return _pack(
            "data", "Need data", 0,
            "Wait for more spend or extend the date range before changing this ad.",
            ["Not enough call data yet"],
        )

    cpa_ratio = (cpcall / avg_cpcall) if avg_cpcall and cpcall else 1.0
    if cpa_ratio <= 0.75:
        score += 130
        reasons.append(f"${cpcall:.2f}/call — well below {bench} avg (${avg_cpcall:.2f})")
    elif cpa_ratio <= 0.95:
        score += 90
        reasons.append(f"${cpcall:.2f}/call — below {bench} average")
    elif cpa_ratio <= 1.1:
        score += 45
        reasons.append(f"${cpcall:.2f}/call — near {bench} average")
    elif cpa_ratio <= 1.4:
        score -= 25
        reasons.append(f"${cpcall:.2f}/call — above average; trim budget or refresh creative")
    elif cpa_ratio <= 2.0 and high_ticket:
        score -= 15
        reasons.append(f"${cpcall:.2f}/call — high for {bench}, but expected for high-ticket")
    else:
        score -= 90
        reasons.append(f"${cpcall:.2f}/call — {cpa_ratio:.1f}× {bench} avg — too expensive")

    if avg_calls > 0:
        if calls >= avg_calls * 1.25:
            score += 45
            reasons.append(f"{calls} calls — high volume vs other ads")
        elif calls >= avg_calls * 0.8:
            score += 15
            reasons.append(f"{calls} calls — solid volume")
        elif calls < 5 and spend > 250:
            score -= 35
            reasons.append(f"Only {calls} calls for ${spend:,.0f} spend — low volume")
    else:
        reasons.append(f"{calls} calls recorded")

    ctr_weak = False
    if avg_ctr > 0:
        ctr_r = ctr / avg_ctr
        if ctr_r >= 1.15:
            score += 20
            reasons.append(f"CTR {ctr:.2f}% — above average engagement")
        elif ctr_r <= 0.75:
            score -= 20
            ctr_weak = True
            reasons.append(f"CTR {ctr:.2f}% — weak; test new hook or image")
        else:
            reasons.append(f"CTR {ctr:.2f}% — average engagement")

    if spend >= 500 and cpa_ratio > 1.25:
        score -= 30
        reasons.append(f"High spend (${spend:,.0f}) with expensive calls")

    # High-ticket with calls: prefer creative refresh over hard kill.
    if high_ticket and calls > 0 and (ctr_weak or cpa_ratio > 1.1):
        if score <= 20:
            return _pack(
                "watch", "Refresh", score,
                "Keep the walk-in campaign running but replace underperforming images/headlines. "
                "High-ticket products naturally have lower CTR — focus on better creative, not pausing the line.",
                reasons,
            )
        if score < 110:
            return _pack(
                "watch", "Watch", score,
                "Hold budget. Test 2–3 new creative variants aimed at commercial buyers, then compare $/call.",
                reasons,
            )

    if score >= 110:
        return _pack(
            "scale", "Scale", score,
            "Increase budget 20–50% on this creative. Duplicate the hook in new ad sets.",
            reasons,
        )
    if score <= 20:
        return _pack(
            "kill", "Kill", score,
            "Pause or cut budget. Shift spend to ads with lower $/call and similar CTR.",
            reasons,
        )
    return _pack(
        "watch", "Watch", score,
        "Hold budget steady. Review again in 7 days or test one new headline/image variant.",
        reasons,
    )


def _verdict_leads(ad: dict, ctx: dict, rules: dict) -> dict:
    leads = int(ad.get("leads") or 0)
    spend = ad["spend"]
    cpl = ad.get("cost_per_lead") or (round(spend / leads, 2) if leads else None)
    avg_cpl = ctx["avg_cpl"]
    reasons: list[str] = []
    score = 0.0

    if leads == 0:
        if spend >= rules["kill_spend"]:
            return _pack("kill", "Kill", -180, "Pause — spending without leads.", [f"${spend:,.0f} spend, 0 leads"])
        return _pack("data", "Need data", 0, "Need more data before changing this ad.", ["No leads yet"])

    ratio = (cpl / avg_cpl) if avg_cpl and cpl else 1.0
    if ratio <= 0.85:
        score += 100
        reasons.append(f"${cpl:.2f}/lead — below average")
    elif ratio <= 1.15:
        score += 40
        reasons.append(f"${cpl:.2f}/lead — near average")
    else:
        score -= 60
        reasons.append(f"${cpl:.2f}/lead — above average")

    reasons.append(f"{leads} leads")
    if score >= 80:
        return _pack("scale", "Scale", score, "Scale budget on this lead driver.", reasons)
    if score <= 10:
        return _pack("kill", "Kill", score, "Pause and reallocate to lower $/lead ads.", reasons)
    return _pack("watch", "Watch", score, "Monitor lead quality and cost for another week.", reasons)


def _verdict_roas(ad: dict, rules: dict) -> dict:
    roas = ad.get("roas")
    if roas is None:
        return _pack("watch", "Watch", 50, "Track until purchase value is available.", ["No ROAS yet"])
    reasons = [f"ROAS {roas:.2f}x"]
    if roas >= rules["target_roas"]:
        return _pack("scale", "Scale", 300 + roas * 10, "Increase budget — strong return on ad spend.", reasons)
    if roas < rules["kill_roas"]:
        return _pack("kill", "Kill", -200 + roas * 10, "Pause — ROAS below target.", reasons)
    return _pack("watch", "Watch", 100 + roas * 10, "Optimize or wait for more conversion data.", reasons)


def _verdict_engagement(ad: dict, ctx: dict, rules: dict) -> dict:
    ctr_r = (ad["ctr"] / ctx["avg_ctr"]) if ctx["avg_ctr"] else 1.0
    cpc_r = (ad["cpc"] / ctx["avg_cpc"]) if (ctx["avg_cpc"] and ad["cpc"]) else 1.0
    score = ctr_r * 120 - cpc_r * 60
    reasons = [f"CTR {ad['ctr']:.2f}%", f"CPC ${ad['cpc']:.2f}"]
    if ctr_r >= 1.15 and cpc_r <= 0.95:
        return _pack("scale", "Scale", score, "Strong CTR efficiency — test scaling if calls/leads tracking is added.", reasons)
    if ctr_r < 0.6 or (ad["spend"] > rules["kill_spend"] and ctr_r < 0.85):
        return _pack("kill", "Kill", score, "Pause — weak engagement vs other ads.", reasons)
    return _pack("watch", "Watch", score, "Add call or lead tracking to score on real outcomes.", reasons)


def build_context(ads: list[dict], rules: dict) -> dict:
    judged = [a for a in ads if a["spend"] >= rules["min_spend"]]
    pool = judged or ads
    mode = _scoring_mode(ads)

    def avg(arr, key):
        vals = [a[key] for a in arr if a.get(key)]
        return mean(vals) if vals else 0.0

    return {
        "mode": mode,
        "has_conv": mode != "engagement",
        "avg_ctr": avg(pool, "ctr"),
        "avg_cpc": avg(pool, "cpc"),
        "avg_cpl": _avg_cpa(pool, "leads"),
        "avg_cpcall": _avg_cpa(pool, "calls"),
        "avg_calls": _avg_calls(pool),
        "by_category": _category_context(ads, rules),
    }


def verdict(ad: dict, ctx: dict, rules: dict) -> dict:
    if ad["spend"] < rules["min_spend"]:
        return _pack(
            "data", "Need data", 0,
            f"Wait until at least ${rules['min_spend']:.0f} spend before judging.",
            [f'Only ${ad["spend"]:.0f} spent so far'],
        )

    mode = ctx["mode"]
    if mode == "roas":
        return _verdict_roas(ad, rules)
    if mode == "calls":
        return _verdict_calls(ad, ctx, rules)
    if mode == "leads":
        return _verdict_leads(ad, ctx, rules)
    return _verdict_engagement(ad, ctx, rules)


def _score_one_asset(asset: dict, siblings: list[dict], ad: dict) -> dict:
    """Rank creatives within one ad — identify which image to remove."""
    spend = float(asset.get("spend") or 0)
    calls = int(asset.get("calls") or 0)
    ctr = float(asset.get("ctr") or 0)
    cpcall = asset.get("cost_per_call")
    reasons: list[str] = []

    if spend < 5:
        return _pack(
            "data", "Need data", 0,
            "Not enough spend on this creative yet.",
            [f"${spend:.0f} spent"],
        )

    sibling_calls = [s for s in siblings if (s.get("calls") or 0) > 0 and s is not asset]
    best_cpcall = min((s.get("cost_per_call") or 999999 for s in sibling_calls), default=None)
    best_ctr = max((s.get("ctr") or 0 for s in siblings if s is not asset), default=0)
    score = 0.0

    if calls == 0 and spend >= 30:
        return _pack(
            "kill", "Remove", -100,
            "Turn off this image in Ads Manager — it spent without generating calls.",
            [f"${spend:.0f} spend · 0 calls · CTR {ctr:.2f}%"],
        )

    if cpcall and best_cpcall and best_cpcall < 999999:
        ratio = cpcall / best_cpcall
        if ratio <= 0.85:
            score += 80
            reasons.append(f"${cpcall:.2f}/call — best or near-best in this ad")
        elif ratio <= 1.15:
            score += 30
            reasons.append(f"${cpcall:.2f}/call — similar to other creatives")
        else:
            score -= 60
            reasons.append(f"${cpcall:.2f}/call — {ratio:.1f}× worse than best creative in ad")

    if best_ctr > 0:
        if ctr >= best_ctr * 1.05:
            score += 25
            reasons.append(f"CTR {ctr:.2f}% — top engagement in this ad")
        elif ctr <= best_ctr * 0.7:
            score -= 25
            reasons.append(f"CTR {ctr:.2f}% — weakest hook in this ad")

    reasons.insert(0, f"{calls} calls · ${spend:.0f} spend")

    if score >= 70:
        return _pack("scale", "Keep", score, "Best-performing creative in this ad — leave it running.", reasons)
    if score <= -20:
        return _pack("kill", "Remove", score, "Remove or pause this image in Meta — shift rotation to better creatives.", reasons)
    return _pack("watch", "Watch", score, "Mixed signals — give it more spend or replace if another creative wins.", reasons)


def score_assets(ad: dict) -> list[dict]:
    assets = ad.get("assets") or []
    if not assets:
        return []
    scored = []
    for asset in assets:
        v = _score_one_asset(asset, assets, ad)
        scored.append({**asset, "verdict": v})
    scored.sort(key=lambda x: (-(x.get("verdict") or {}).get("score", 0), -(x.get("spend") or 0)))
    return scored


def score_all(ads: list[dict], rules: dict | None = None) -> list[dict]:
    rules = {**DEFAULT_RULES, **(rules or {})}
    for a in ads:
        a["category"] = categorize(a.get("name") or "", a.get("campaign") or "")
    ctx = build_context(ads, rules)
    for a in ads:
        a["verdict"] = verdict(a, ctx, rules)
        a["scoring_mode"] = ctx["mode"]
        if a.get("assets"):
            a["assets"] = score_assets(a)
    return ads
