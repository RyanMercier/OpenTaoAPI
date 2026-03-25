import httpx

from api.services.cache import TTLCache
from api.config import settings

MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"


class PriceClient:
    def __init__(self, cache: TTLCache):
        self._cache = cache
        self._client: httpx.AsyncClient | None = None

    async def startup(self):
        self._client = httpx.AsyncClient(timeout=10.0)

    async def shutdown(self):
        if self._client:
            await self._client.aclose()

    async def get_tao_price(self) -> float:
        return await self._cache.get_or_set(
            "price:tao_usdt",
            self._fetch_price,
            ttl=settings.cache_ttl_price,
        )

    async def _fetch_price(self) -> float:
        if not self._client:
            raise RuntimeError("PriceClient not started — call startup() first")
        resp = await self._client.get(
            MEXC_TICKER_URL, params={"symbol": "TAOUSDT"}
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        if price is None:
            raise ValueError(f"MEXC response missing 'price' field: {data}")
        return float(price)
