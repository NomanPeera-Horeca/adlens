"""
Cache Meta insights locally so the dashboard loads fast and we respect rate limits.

Each (user, ad account, date range) gets one SyncRun row. Stale rows are
refreshed on demand; the frontend Refresh button forces a new pull.
"""
import json
from datetime import datetime, timedelta

from sqlmodel import Session, select

from . import meta, scoring
from . import creative_ai, winners
from .config import settings
from .models import SyncRun


def _norm_account(account_id: str) -> str:
    return account_id.replace("act_", "")


def cache_ttl(user_plan: str) -> timedelta:
    minutes = settings.CACHE_TTL_PRO if (user_plan == "pro") else settings.CACHE_TTL_FREE_MIN
    return timedelta(minutes=minutes)


def get_cached(session: Session, user_id: int, account_id: str, date_range: str) -> SyncRun | None:
    account_id = _norm_account(account_id)
    return session.exec(
        select(SyncRun).where(
            SyncRun.user_id == user_id,
            SyncRun.account_id == account_id,
            SyncRun.date_range == date_range,
        )
    ).first()


def is_fresh(run: SyncRun, user_plan: str) -> bool:
    if run.status != "success" or not run.synced_at:
        return False
    return datetime.utcnow() - run.synced_at < cache_ttl(user_plan)


def payload_from_run(run: SyncRun) -> dict:
    if not run.ads_json:
        return {"ads": [], "campaigns": []}
    raw = json.loads(run.ads_json)
    if isinstance(raw, list):
        return {"ads": raw, "campaigns": []}
    return {"ads": raw.get("ads", []), "campaigns": raw.get("campaigns", [])}


async def fetch_and_store(
    session: Session,
    user_id: int,
    account_id: str,
    cache_key: str,
    date_query: dict,
    token: str,
) -> SyncRun:
    account_id = _norm_account(account_id)
    run = get_cached(session, user_id, account_id, cache_key)
    if not run:
        run = SyncRun(user_id=user_id, account_id=account_id, date_range=cache_key)
    run.status = "pending"
    run.error_message = ""
    session.add(run)
    session.commit()

    try:
        rows = await meta.insights(token, account_id, date_query)
        asset_rows = await meta.fetch_asset_insights(token, account_id, date_query)
        catalog = await meta.list_active_campaigns(token, account_id)
        live_ads = await meta.list_live_ads(token, account_id)
        camp_rows = await meta.campaign_insights(token, account_id, date_query)
        ad_ids = list({
            *(str(r.get("ad_id")) for r in rows if r.get("ad_id")),
            *(str(a.get("id")) for a in live_ads if a.get("id")),
        })
        ad_meta = await meta.fetch_ads_meta(token, account_id, ad_ids)
        ads = meta.normalize(rows, ad_meta, account_id)
        ads = meta.merge_live_ads(ads, live_ads, ad_meta, account_id)
        ads = meta.attach_assets(ads, asset_rows)
        await meta.resolve_asset_videos(token, ads)
        ads = scoring.score_all(ads)
        winners.attach_peer_winners(session, user_id, account_id, ads)
        await creative_ai.enrich_ads_with_insights(ads)
        winners.record_from_sync(session, user_id, account_id, ads)
        campaigns = scoring.score_all(meta.merge_active_campaigns(catalog, camp_rows))
        run.ads_json = json.dumps({"ads": ads, "campaigns": campaigns})
        run.status = "success"
        run.synced_at = datetime.utcnow()
    except Exception as e:
        run.status = "error"
        run.error_message = str(e)
        session.add(run)
        session.commit()
        raise

    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def ads_from_run(run: SyncRun) -> list[dict]:
    return payload_from_run(run)["ads"]


def campaigns_from_run(run: SyncRun) -> list[dict]:
    return payload_from_run(run)["campaigns"]
