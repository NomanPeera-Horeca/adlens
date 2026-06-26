"""
Creative scoring. Picks the best available signal from Meta:
  purchases/ROAS -> calls -> leads -> engagement (CTR vs CPC)
"""
from statistics import mean


def _avg_cpa(ads: list[dict], count_key: str) -> float:
    vals = [
        a["spend"] / a[count_key]
        for a in ads
        if a.get(count_key, 0) > 0 and a.get("spend", 0) > 0
    ]
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
    if any((a.get("landing_views") or 0) > 0 for a in ads):
        return "landing"
    return "engagement"


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
        "avg_cpa_contact": _avg_cpa(pool, "contacts"),
    }


def _verdict_cpa(ad: dict, count: int, avg_cpa: float, *, good_ratio: float = 0.85, bad_ratio: float = 1.35) -> dict:
    cpa = ad["spend"] / count
    if avg_cpa > 0:
        ratio = cpa / avg_cpa
        score = (1 / ratio) * 100 if ratio else 0
        if ratio <= good_ratio:
            return {"key": "scale", "label": "Scale", "score": score + 200}
        if ratio >= bad_ratio or (ad["spend"] > 150 and ratio >= 1.1):
            return {"key": "kill", "label": "Kill", "score": score - 100}
        return {"key": "watch", "label": "Watch", "score": score}
    score = count * 10 - ad["spend"] / 20
    if count >= 3:
        return {"key": "scale", "label": "Scale", "score": score}
    if count == 0:
        return {"key": "kill", "label": "Kill", "score": score - 50}
    return {"key": "watch", "label": "Watch", "score": score}


def verdict(ad: dict, ctx: dict, rules: dict) -> dict:
    if ad["spend"] < rules["min_spend"]:
        return {"key": "data", "label": "Need data", "score": 0.0}

    mode = ctx["mode"]
    if mode == "roas" and ad.get("roas") is not None:
        roas = ad["roas"]
        if roas >= rules["target_roas"]:
            return {"key": "scale", "label": "Scale", "score": 300 + roas * 10}
        if roas < rules["kill_roas"]:
            return {"key": "kill", "label": "Kill", "score": -200 + roas * 10}
        return {"key": "watch", "label": "Watch", "score": 100 + roas * 10}

    if mode == "calls" and ad.get("calls", 0) > 0:
        return _verdict_cpa(ad, int(ad["calls"]), ctx["avg_cpcall"])
    if mode == "calls" and ad["spend"] >= rules["kill_spend"]:
        return {"key": "kill", "label": "Kill", "score": -150}

    if mode == "leads" and ad.get("leads", 0) > 0:
        return _verdict_cpa(ad, int(ad["leads"]), ctx["avg_cpl"])
    if mode == "leads" and ad["spend"] >= rules["kill_spend"]:
        return {"key": "kill", "label": "Kill", "score": -150}

    if mode == "contacts" and ad.get("contacts", 0) > 0:
        return _verdict_cpa(ad, int(ad["contacts"]), ctx["avg_cpa_contact"])

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
        a["scoring_mode"] = ctx["mode"]
    return ads
