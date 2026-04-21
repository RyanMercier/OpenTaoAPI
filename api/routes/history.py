from fastapi import APIRouter, HTTPException, Query

from api.models.schemas import HistoryStatsResponse, PricePoint, SnapshotPoint
from api.services.database import Database

router = APIRouter(tags=["history"])

_db: Database | None = None


def init_history_router(db: Database):
    global _db
    _db = db


@router.get("/history/{netuid}/price", response_model=list[PricePoint])
async def get_price_history(
    netuid: int,
    hours: int = Query(24, ge=1, le=8760),
    limit: int = Query(500, ge=1, le=5000),
):
    """Alpha price history for a subnet over the last N hours."""
    if not _db:
        raise HTTPException(status_code=503, detail="Historical data not available")
    rows = await _db.get_price_history(netuid, hours=hours, limit=limit)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No historical data for subnet {netuid}. Run the backfill script first.",
        )
    return rows


@router.get("/history/{netuid}/snapshots", response_model=list[SnapshotPoint])
async def get_snapshots(
    netuid: int,
    hours: int = Query(24, ge=1, le=8760),
    limit: int = Query(500, ge=1, le=5000),
):
    """Full historical snapshots for a subnet."""
    if not _db:
        raise HTTPException(status_code=503, detail="Historical data not available")
    rows = await _db.get_snapshots(netuid, hours=hours, limit=limit)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No historical data for subnet {netuid}",
        )
    return rows


@router.get("/history/{netuid}/stats", response_model=HistoryStatsResponse)
async def get_history_stats(netuid: int):
    """Summary stats for a subnet's historical data coverage."""
    if not _db:
        raise HTTPException(status_code=503, detail="Historical data not available")
    return await _db.get_stats(netuid)


_CANDLE_INTERVALS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


@router.get(
    "/subnet/{netuid}/candles",
    summary="OHLC candles for subnet alpha price",
)
async def get_candles(
    netuid: int,
    interval: str = Query("1h", pattern="^(5m|15m|1h|4h|1d)$"),
    hours: int = Query(168, ge=1, le=8760),
):
    """TradingView-style OHLC. Each bar is ``{t, o, h, l, c, n}`` where
    ``t`` is the bucket-start unix timestamp (seconds) and ``n`` is the
    sample count inside that bucket. Good drop-in for lightweight-charts,
    Grafana, or any charting UI."""
    if not _db:
        raise HTTPException(status_code=503, detail="Historical data not available")
    rows = await _db.get_candles(netuid, _CANDLE_INTERVALS[interval], hours)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No candle data for subnet {netuid}. Run the backfill script first.",
        )
    return rows
