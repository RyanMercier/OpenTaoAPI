import logging

from fastapi import APIRouter, HTTPException, Query

from api.models.schemas import (
    PortfolioHistoryPoint,
    PortfolioHistoryResponse,
    PortfolioResponse,
)
from api.services.chain_client import ChainClient
from api.services.database import Database
from api.services.portfolio_service import compute_portfolio
from api.services.price_client import PriceClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio"])

_chain_client: ChainClient | None = None
_price_client: PriceClient | None = None
_db: Database | None = None


def init_portfolio_router(
    chain_client: ChainClient,
    price_client: PriceClient,
    db: Database,
):
    global _chain_client, _price_client, _db
    _chain_client = chain_client
    _price_client = price_client
    _db = db


@router.get("/portfolio/{coldkey}", response_model=PortfolioResponse)
async def get_portfolio(coldkey: str):
    """Full cross-subnet portfolio for a coldkey, computed live."""
    try:
        portfolio, _ = await compute_portfolio(_chain_client, _price_client, coldkey)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return portfolio


@router.get(
    "/portfolio/{coldkey}/history",
    response_model=PortfolioHistoryResponse,
    summary="Portfolio value time series for a tracked wallet",
)
async def get_portfolio_history(
    coldkey: str,
    hours: int = Query(168, ge=1, le=8760),
    limit: int = Query(500, ge=1, le=5000),
):
    """Time series of total portfolio value for a coldkey. Only populated
    for wallets that have been added to the watchlist via
    ``POST /wallets``; otherwise the array is empty."""
    if not _db:
        raise HTTPException(status_code=503, detail="Historical data not available")
    rows = await _db.get_wallet_history(coldkey, hours=hours, limit=limit)
    points = [
        PortfolioHistoryPoint(
            block=r["block"],
            timestamp=r["timestamp"],
            total_balance_tao=r["total_balance_tao"],
            free_balance_tao=r["free_balance_tao"],
            total_staked_tao=r["total_staked_tao"],
            tao_price_usd=r["tao_price_usd"],
            total_balance_usd=r["total_balance_usd"],
            subnet_count=r["subnet_count"],
        )
        for r in rows
    ]
    return PortfolioHistoryResponse(coldkey=coldkey, hours=hours, points=points)
