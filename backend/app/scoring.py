"""
Creative scoring. Two modes:
  - if purchase/ROAS data exists -> score on ROAS vs targets
  - if it doesn't (e.g. broken pixel) -> score on CTR-vs-CPC efficiency
This is the product's opinion layer. Tune freely; it's where you add value
beyond "here are your numbers."
"""
from statistics import mean


def build_context(ads: list[dict], rules: dict) -> dict:
    judged = [a for a in ads if a["spend"] >= rules["min_spend"]]
    has_conv = any((a.get("purchases") or 0) > 0 or (a.get("pvalue") or 0) > 0 for a in ads)

    def avg(arr, key):
        vals = [a[key] for a in arr if a.get(key)]
        return mean(vals) if vals else 0.0

    pool = judged or ads
    return {"has_conv": has_conv, "avg_ctr": avg(pool, "ctr"), "avg_cpc": avg(pool, "cpc")}


def verdict(ad: dict, ctx: dict, rules: dict) -> dict:
    if ad["spend"] < rules["min_spend"]:
        return {"key": "data", "label": "Need data", "score": 0.0}

    if ctx["has_conv"] and ad.get("roas") is not None:
        roas = ad["roas"]
        if roas >= rules["target_roas"]:
            return {"key": "scale", "label": "Scale", "score": 300 + roas * 10}
        if roas < rules["kill_roas"]:
            return {"key": "kill", "label": "Kill", "score": -200 + roas * 10}
        return {"key": "watch", "label": "Watch", "score": 100 + roas * 10}

    ctr_r = (ad["ctr"] / ctx["avg_ctr"]) if ctx["avg_ctr"] else 1.0
    cpc_r = (ad["cpc"] / ctx["avg_cpc"]) if (ctx["avg_cpc"] and ad["cpc"]) else 1.0
    score = ctr_r * 120 - cpc_r * 60
    if ctr_r >= 1.15 and cpc_r <= 0.95:
        return {"key": "scale", "label": "Scale", "score": score}
    if ctr_r < 0.6 or (ad["spend"] > rules["kill_spend"] and ctr_r < 0.85):
        return {"key": "kill", "label": "Kill", "score": score}
    return {"key": "watch", "label": "Watch", "score": score}


DEFAULT_RULES = {"target_roas": 2.0, "kill_roas": 1.0, "min_spend": 60.0, "kill_spend": 150.0}


def score_all(ads: list[dict], rules: dict | None = None) -> list[dict]:
    rules = {**DEFAULT_RULES, **(rules or {})}
    ctx = build_context(ads, rules)
    for a in ads:
        a["verdict"] = verdict(a, ctx, rules)
    return ads
