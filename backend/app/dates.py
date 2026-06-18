"""Map UI date selections to Meta Insights API params + cache keys."""
import json
import re

FREE_RANGES = {"last_7d", "last_14d", "last_30d", "this_month", "this_year"}

PRO_ONLY_RANGES = {
    "last_90d", "maximum", "last_month", "this_quarter", "last_quarter", "custom",
}


def resolve_date_query(range_key: str, since: str = "", until: str = "") -> tuple[str, dict]:
    """Return (cache_key, query) where query uses date_preset or time_range."""
    if range_key == "custom":
        since = (since or "").strip()
        until = (until or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
            raise ValueError("Custom range requires since and until as YYYY-MM-DD")
        cache_key = f"custom:{since}:{until}"
        return cache_key, {"time_range": {"since": since, "until": until}}
    preset = range_key or "last_30d"
    return preset, {"date_preset": preset}


def insights_params(date_query: dict) -> dict:
    if "time_range" in date_query:
        return {"time_range": json.dumps(date_query["time_range"])}
    return {"date_preset": date_query["date_preset"]}
