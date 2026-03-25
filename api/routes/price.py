from fastapi import APIRouter, HTTPException

from api.models.schemas import PriceResponse
from api.services.price_client import PriceClient

router = APIRouter(tags=["price"])

_price_client: PriceClient | None = None


def init_price_router(price_client: PriceClient):
    global _price_client
    _price_client = price_client


@router.get("/price/tao", response_model=PriceResponse)
async def get_tao_price():
    """Current TAO/USDT price from MEXC. Cached for 30 seconds."""
    try:
        price = await _price_client.get_tao_price()
        return PriceResponse(price=price)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch price: {e}")
