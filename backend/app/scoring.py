"""
Creative scoring with explicit actions.

Call-heavy accounts: weigh cost/call, call volume, CTR, and spend together.
"""
from statistics import mean


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


def _verdict_calls(ad: dict, ctx: dict, rules: dict) -> dict:
    calls = int(ad.get("calls") or 0)
    spend = ad["spend"]
    ctr = float(ad.get("ctr") or 0)
    cpcall = ad.get("cost_per_call") or (round(spend / calls, 2) if calls else None)
    avg_cpcall = ctx["avg_cpcall"]
    avg_ctr = ctx["avg_ctr"]
    avg_calls = ctx["avg_calls"]
    reasons: list[str] = []
    score = 0.0

    if calls == 0:
        if spend >= rules["kill_spend"]:
            return _pack(
                "kill", "Kill", -200,
                "Pause this ad and move budget to creatives that are generating calls.",
                [f"${spend:,.0f} spent with zero calls in this period"],
            )
        return _pack(
            "data", "Need data", 0,
            "Wait for more spend or extend the date range before changing this ad.",
            ["Not enough call data yet"],
        )

    cpa_ratio = (cpcall / avg_cpcall) if avg_cpcall and cpcall else 1.0
    if cpa_ratio <= 0.75:
        score += 130
        reasons.append(f"${cpcall:.2f}/call — well below account avg (${avg_cpcall:.2f})")
    elif cpa_ratio <= 0.95:
        score += 90
        reasons.append(f"${cpcall:.2f}/call — below account average")
    elif cpa_ratio <= 1.1:
        score += 45
        reasons.append(f"${cpcall:.2f}/call — near account average")
    elif cpa_ratio <= 1.4:
        score -= 25
        reasons.append(f"${cpcall:.2f}/call — above average; trim budget")
    else:
        score -= 90
        reasons.append(f"${cpcall:.2f}/call — {cpa_ratio:.1f}× account avg — too expensive")

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

    if avg_ctr > 0:
        ctr_r = ctr / avg_ctr
        if ctr_r >= 1.15:
            score += 20
            reasons.append(f"CTR {ctr:.2f}% — above average engagement")
        elif ctr_r <= 0.75:
            score -= 20
            reasons.append(f"CTR {ctr:.2f}% — weak; refresh hook or image")
        else:
            reasons.append(f"CTR {ctr:.2f}% — average engagement")

    if spend >= 500 and cpa_ratio > 1.25:
        score -= 30
        reasons.append(f"High spend (${spend:,.0f}) with expensive calls")

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


def score_all(ads: list[dict], rules: dict | None = None) -> list[dict]:
    rules = {**DEFAULT_RULES, **(rules or {})}
    ctx = build_context(ads, rules)
    for a in ads:
        a["verdict"] = verdict(a, ctx, rules)
        a["scoring_mode"] = ctx["mode"]
    return ads
