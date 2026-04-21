import httpx

from api.services.cache import TTLCache
from api.config import settings

MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"
MEXC_KLINES_URL = "https://api.mexc.com/api/v3/klines"


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

    async def get_historical_klines(
        self,
        start_ms: int,
        end_ms: int,
        interval: str = "1h",
    ) -> list[dict]:
        """Paginate MEXC klines across a wide range. Returns dicts with
        ``open_ms``, ``close_ms``, ``open``, ``high``, ``low``, ``close``,
        ``volume``. Closed klines are immutable so callers can cache freely."""
        if not self._client:
            raise RuntimeError("PriceClient not started — call startup() first")

        out: list[dict] = []
        cursor = start_ms
        # MEXC caps at 1000 candles per request.
        while cursor < end_ms:
            resp = await self._client.get(
                MEXC_KLINES_URL,
                params={
                    "symbol": "TAOUSDT",
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for k in batch:
                # MEXC kline layout: [open_ms, open, high, low, close, volume,
                # close_ms, ...]
                out.append({
                    "open_ms": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_ms": int(k[6]),
                })
            # Advance past the last close to avoid duplicates; bail if the
            # exchange gave us a short page OR didn't move the cursor forward
            # (which would otherwise infinite-loop on malformed data).
            next_cursor = out[-1]["close_ms"] + 1
            if next_cursor <= cursor or len(batch) < 1000:
                break
            cursor = next_cursor
        return out
