"""
Infer product category from ad/campaign names so high-ticket lines (walk-in)
are scored against peers, not blended with low-ticket fridge ads.
"""
from __future__ import annotations

# (key, label, ticket_usd, cpa_multiplier, keywords)
_RULES: list[tuple[str, str, int, float, tuple[str, ...]]] = [
    (
        "walk_in",
        "Walk-in / cold room",
        10_000,
        3.0,
        ("walk-in", "walkin", "walk in", "cold room", "cooler room", "walkinref"),
    ),
    (
        "reach_in",
        "Reach-in refrigeration",
        2_000,
        1.0,
        ("reach-in", "reach in", "medal", "door-us", "refrigerator", "fridge", "freezer", "display case"),
    ),
    (
        "hotel",
        "Hotel / bedding",
        5_000,
        1.5,
        ("hotel", "mattress", "bedding", "bed ", "linen", "texashotel"),
    ),
    (
        "kitchen",
        "Kitchen equipment",
        4_000,
        1.2,
        ("fryer", "range", "oven", "griddle", "kitchen", "cooking"),
    ),
    (
        "startup",
        "Restaurant startup",
        8_000,
        2.0,
        ("startup", "restaurant setup", "restaurantsetup", "new restaurant"),
    ),
    (
        "marketplace",
        "Marketplace / engagement",
        3_000,
        1.0,
        ("marketplace", "engagement ad", "new engagement"),
    ),
]

_DEFAULT = {
    "key": "general",
    "label": "General equipment",
    "ticket_usd": 3_000,
    "cpa_multiplier": 1.0,
    "high_ticket": False,
}


def categorize(name: str = "", campaign: str = "") -> dict:
    text = f"{name or ''} {campaign or ''}".lower()
    for key, label, ticket, mult, keywords in _RULES:
        if any(kw in text for kw in keywords):
            return {
                "key": key,
                "label": label,
                "ticket_usd": ticket,
                "cpa_multiplier": mult,
                "high_ticket": mult >= 2.0 or ticket >= 8_000,
            }
    return dict(_DEFAULT)
