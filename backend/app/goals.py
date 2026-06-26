"""Infer what each ad is optimized for from Meta conversion data."""


def infer_ad_goal(ad: dict) -> dict:
    calls = int(ad.get("calls") or 0)
    leads = int(ad.get("leads") or 0)
    lpv = int(ad.get("landing_views") or 0)
    messages = int(ad.get("messages") or 0)
    clicks = int(ad.get("clicks") or 0)
    purchases = int(ad.get("purchases") or 0)

    if purchases > 0 and (ad.get("pvalue") or 0) > 0:
        return {"key": "purchases", "label": "Purchases", "metric": "purchases", "cost_key": "cost_per_result"}

    if calls >= max(leads, 3) and calls >= lpv // 8:
        return {"key": "calls", "label": "Phone calls", "metric": "calls", "cost_key": "cost_per_call"}

    if leads >= max(calls, 2):
        return {"key": "leads", "label": "Leads", "metric": "leads", "cost_key": "cost_per_lead"}

    if lpv >= max(calls, clicks // 4, 5):
        return {"key": "traffic", "label": "Site visitors", "metric": "landing_views", "cost_key": "cost_per_lpv"}

    if messages > 0:
        return {"key": "messages", "label": "Messages", "metric": "messages", "cost_key": None}

    if calls > 0:
        return {"key": "calls", "label": "Phone calls", "metric": "calls", "cost_key": "cost_per_call"}

    if lpv > 0:
        return {"key": "traffic", "label": "Site visitors", "metric": "landing_views", "cost_key": "cost_per_lpv"}

    return {"key": "engagement", "label": "Engagement", "metric": "clicks", "cost_key": "cpc"}
