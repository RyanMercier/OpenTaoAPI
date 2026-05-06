"""Wallet watchlist CRUD.

A tracked wallet is a coldkey the operator wants snapshots of over time.
The background ``_wallet_poller`` in ``api/main.py`` reads this table and
inserts rows into ``wallet_portfolio_snapshots``. The ``/portfolio/{coldkey}``
page reads the resulting series for its time-of-flight chart.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from api.models.schemas import (
    TrackWalletRequest,
    TrackedWallet,
    TrackedWalletWithLatest,
)
from api.services.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wallets"])

_db: Database | None = None


def init_wallets_router(db: Database) -> None:
    global _db
    _db = db


def _row_to_tracked(row: dict) -> TrackedWallet:
    return TrackedWallet(
        id=row["id"],
        coldkey_ss58=row["coldkey_ss58"],
        label=row.get("label"),
        created_at=row["created_at"],
        last_polled_at=row.get("last_polled_at"),
        poll_interval_seconds=row["poll_interval_seconds"],
        active=bool(row["active"]),
    )


@router.post(
    "/wallets",
    response_model=TrackedWallet,
    status_code=201,
    summary="Add a coldkey to the watchlist",
)
async def add_wallet(req: TrackWalletRequest):
    """Add a coldkey to the watchlist. Idempotent: re-adding an existing
    coldkey reactivates it and updates label/interval. Snapshot history
    is preserved across reactivation."""
    if not _db:
        raise HTTPException(status_code=503, detail="Database not available")
    row = await _db.add_tracked_wallet(
        coldkey=req.coldkey,
        label=req.label,
        poll_interval_seconds=req.poll_interval_seconds,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return _row_to_tracked(row)


@router.get(
    "/wallets",
    response_model=list[TrackedWalletWithLatest],
    summary="List tracked wallets with their latest snapshot",
)
async def list_wallets():
    if not _db:
        raise HTTPException(status_code=503, detail="Database not available")
    wallets = await _db.list_tracked_wallets(active_only=False)
    out: list[TrackedWalletWithLatest] = []
    for row in wallets:
        latest = await _db.get_wallet_latest(row["coldkey_ss58"])
        out.append(TrackedWalletWithLatest(
            id=row["id"],
            coldkey_ss58=row["coldkey_ss58"],
            label=row.get("label"),
            created_at=row["created_at"],
            last_polled_at=row.get("last_polled_at"),
            poll_interval_seconds=row["poll_interval_seconds"],
            active=bool(row["active"]),
            latest_block=latest["block"] if latest else None,
            latest_timestamp=latest["timestamp"] if latest else None,
            total_balance_tao=latest["total_balance_tao"] if latest else None,
            total_balance_usd=latest["total_balance_usd"] if latest else None,
            total_staked_tao=latest["total_staked_tao"] if latest else None,
            free_balance_tao=latest["free_balance_tao"] if latest else None,
            subnet_count=latest["subnet_count"] if latest else None,
        ))
    return out


@router.delete("/wallets/{coldkey}", summary="Remove a coldkey from the watchlist")
async def remove_wallet(coldkey: str):
    """Soft delete: marks the wallet inactive but keeps snapshot history.
    Re-adding the coldkey reactivates polling without losing data."""
    if not _db:
        raise HTTPException(status_code=503, detail="Database not available")
    if not await _db.deactivate_tracked_wallet(coldkey):
        raise HTTPException(status_code=404, detail="Wallet not tracked")
    return {"coldkey": coldkey, "active": False}
