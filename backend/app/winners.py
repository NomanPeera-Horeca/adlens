"""
Winner library — learn from proven creatives in each account.

After each sync we store top-performing assets by category + goal.
Future analysis compares weak creatives to what already worked for YOU.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from .models import CreativeWinner

PEER_LIMIT = 3
MIN_SPEND = 25.0


def _primary(ad: dict) -> tuple[str, str | None, int, float | None]:
    goal = ad.get("goal") or {}
    metric = goal.get("metric") or "calls"
    cost_key = goal.get("cost_key") or "cost_per_call"
    count = int(ad.get(metric) or 0)
    cost = ad.get(cost_key) if cost_key else ad.get("cpc")
    return metric, cost_key, count, cost


def _winner_score(count: int, cost: float | None, spend: float) -> float:
    if count > 0 and cost and cost > 0:
        return count * 1000 / cost + min(spend, 500) * 0.01
    if count > 0:
        return count * 50 + spend * 0.05
    return spend * 0.02


def _candidate_from_asset(ad: dict, asset: dict) -> dict | None:
    verdict = (asset.get("verdict") or {}).get("key")
    if verdict != "scale":
        return None
    metric, cost_key, count, cost = _primary(ad)
    spend = float(asset.get("spend") or 0)
    if spend < MIN_SPEND:
        return None
    if count <= 0 and spend < 80:
        return None
    cat = ad.get("category") or {}
    goal = ad.get("goal") or {}
    insight = asset.get("insight") or {}
    return {
        "asset_id": str(asset.get("asset_id") or ""),
        "ad_id": str(ad.get("ad_id") or ""),
        "ad_name": ad.get("name") or "",
        "name": asset.get("name") or ad.get("name") or "Creative",
        "thumb": asset.get("thumb") or ad.get("thumb") or "",
        "category_key": cat.get("key") or "general",
        "category_label": cat.get("label") or "General",
        "goal_key": goal.get("key") or "calls",
        "goal_label": goal.get("label") or "Phone calls",
        "spend": spend,
        "ctr": float(asset.get("ctr") or 0),
        "calls": int(asset.get("calls") or 0),
        "leads": int(asset.get("leads") or 0),
        "landing_views": int(asset.get("landing_views") or 0),
        "primary_metric": metric,
        "primary_count": count,
        "primary_cost": float(cost) if cost is not None else None,
        "insight_visual": insight.get("visual") or "",
        "insight_hook": insight.get("hook") or "",
        "rank_score": _winner_score(count, cost, spend),
    }


def _candidate_from_ad(ad: dict) -> dict | None:
    if (ad.get("verdict") or {}).get("key") != "scale":
        return None
    metric, cost_key, count, cost = _primary(ad)
    spend = float(ad.get("spend") or 0)
    if spend < MIN_SPEND or (count <= 0 and spend < 80):
        return None
    ad_id = str(ad.get("ad_id") or "")
    if not ad_id:
        return None
    cat = ad.get("category") or {}
    goal = ad.get("goal") or {}
    return {
        "asset_id": f"ad:{ad_id}",
        "ad_id": ad_id,
        "ad_name": ad.get("name") or "",
        "name": ad.get("name") or "Ad",
        "thumb": ad.get("thumb") or "",
        "category_key": cat.get("key") or "general",
        "category_label": cat.get("label") or "General",
        "goal_key": goal.get("key") or "calls",
        "goal_label": goal.get("label") or "Phone calls",
        "spend": spend,
        "ctr": float(ad.get("ctr") or 0),
        "calls": int(ad.get("calls") or 0),
        "leads": int(ad.get("leads") or 0),
        "landing_views": int(ad.get("landing_views") or 0),
        "primary_metric": metric,
        "primary_count": count,
        "primary_cost": float(cost) if cost is not None else None,
        "insight_visual": "",
        "insight_hook": "",
        "rank_score": _winner_score(count, cost, spend),
    }


def collect_candidates(ads: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for ad in ads:
        assets = ad.get("assets") or []
        if assets:
            for asset in assets:
                row = _candidate_from_asset(ad, asset)
                if row and row["asset_id"] and row["asset_id"] not in seen:
                    seen.add(row["asset_id"])
                    out.append(row)
        else:
            row = _candidate_from_ad(ad)
            if row and row["asset_id"] not in seen:
                seen.add(row["asset_id"])
                out.append(row)
    return out


def record_from_sync(session: Session, user_id: int, account_id: str, ads: list[dict]) -> int:
    account_id = account_id.replace("act_", "")
    now = datetime.utcnow()
    saved = 0
    for row in collect_candidates(ads):
        existing = session.exec(
            select(CreativeWinner).where(
                CreativeWinner.user_id == user_id,
                CreativeWinner.account_id == account_id,
                CreativeWinner.asset_id == row["asset_id"],
            )
        ).first()
        if existing:
            for key in (
                "ad_id", "ad_name", "name", "thumb", "category_key", "category_label",
                "goal_key", "goal_label", "spend", "ctr", "calls", "leads", "landing_views",
                "primary_metric", "primary_count", "primary_cost", "insight_visual",
                "insight_hook", "rank_score",
            ):
                setattr(existing, key, row[key])
            existing.last_seen_at = now
        else:
            session.add(CreativeWinner(user_id=user_id, account_id=account_id, first_seen_at=now, last_seen_at=now, **row))
        saved += 1
    session.commit()
    return saved


def _winner_to_peer(w: CreativeWinner) -> dict:
    return {
        "asset_id": w.asset_id,
        "ad_name": w.ad_name,
        "name": w.name,
        "thumb": w.thumb,
        "category_label": w.category_label,
        "goal_label": w.goal_label,
        "spend": w.spend,
        "ctr": w.ctr,
        "primary_metric": w.primary_metric,
        "primary_count": w.primary_count,
        "primary_cost": w.primary_cost,
        "insight_visual": w.insight_visual,
        "insight_hook": w.insight_hook,
    }


def load_peers(
    session: Session,
    user_id: int,
    account_id: str,
    category_key: str,
    goal_key: str,
    exclude_asset_ids: set[str] | None = None,
    limit: int = PEER_LIMIT,
) -> list[dict]:
    account_id = account_id.replace("act_", "")
    exclude = exclude_asset_ids or set()
    rows = session.exec(
        select(CreativeWinner).where(
            CreativeWinner.user_id == user_id,
            CreativeWinner.account_id == account_id,
            CreativeWinner.category_key == category_key,
            CreativeWinner.goal_key == goal_key,
        )
    ).all()
    rows = [r for r in rows if r.asset_id not in exclude]
    rows.sort(key=lambda r: (-r.rank_score, -(r.primary_count or 0)))
    return [_winner_to_peer(r) for r in rows[:limit]]


def attach_peer_winners(session: Session, user_id: int, account_id: str, ads: list[dict]) -> None:
    for ad in ads:
        cat = (ad.get("category") or {}).get("key") or "general"
        goal = (ad.get("goal") or {}).get("key") or "calls"
        exclude = {str(a.get("asset_id")) for a in (ad.get("assets") or []) if a.get("asset_id")}
        ad_id = str(ad.get("ad_id") or "")
        if ad_id:
            exclude.add(f"ad:{ad_id}")
        ad["peer_winners"] = load_peers(session, user_id, account_id, cat, goal, exclude)


def format_peers_for_prompt(peers: list[dict]) -> str:
    if not peers:
        return "No prior winners recorded yet for this category — use B2B Horeca best practices."
    lines = []
    for i, p in enumerate(peers, 1):
        cost = f"${p['primary_cost']:.2f}" if p.get("primary_cost") else "n/a"
        visual = p.get("insight_visual") or "proven performer"
        hook = p.get("insight_hook") or p.get("name") or ""
        lines.append(
            f"{i}. \"{p.get('ad_name') or p.get('name')}\" — "
            f"{p.get('primary_count', 0)} {p.get('primary_metric', 'results')} at {cost}, "
            f"CTR {p.get('ctr', 0):.2f}% — looks like: {visual}; hook: {hook}"
        )
    return "\n".join(lines)
