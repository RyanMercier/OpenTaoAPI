"""Fill `tao_price_usd` on historical snapshots from MEXC klines.

Run after `scripts.backfill` has populated chain snapshots. The chain backfill
stores 0.0 for USD because MEXC has no per-block quote; this script rounds
every row down to its hour bucket and fills in the matching 1h kline close.

Usage:
    python -m scripts.backfill_prices
    python -m scripts.backfill_prices --start 2025-02-01T00:00 --end 2025-03-01T00:00
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import settings
from api.services.cache import TTLCache
from api.services.database import Database
from api.services.price_client import PriceClient


def _iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_hour_key(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H")


async def run(args: argparse.Namespace) -> None:
    db = Database(args.db_path)
    await db.startup()

    cache = TTLCache()
    prices = PriceClient(cache)
    await prices.startup()

    try:
        if args.start and args.end:
            start_ms, end_ms = _iso_to_ms(args.start), _iso_to_ms(args.end)
            print(f"Using supplied range: {args.start} -> {args.end}")
        else:
            rng = await db.get_missing_price_range()
            if not rng:
                print("No rows missing tao_price_usd — nothing to do.")
                return
            start_ms, end_ms = _iso_to_ms(rng[0]), _iso_to_ms(rng[1]) + 3600_000
            print(f"Filling missing prices from {rng[0]} to {rng[1]}")

        print(f"Fetching MEXC klines ({(end_ms - start_ms) / 3600_000:.0f} hours)...")
        klines = await prices.get_historical_klines(start_ms, end_ms, interval="1h")
        print(f"Got {len(klines)} klines")

        by_hour = {_ms_to_hour_key(k["open_ms"]): k["close"] for k in klines}
        updated = await db.update_tao_prices_hourly(by_hour)
        print(f"Updated {updated} rows.")
    finally:
        await prices.shutdown()
        await db.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill tao_price_usd from MEXC klines")
    parser.add_argument("--start", type=str, default=None, help="ISO timestamp, e.g. 2025-02-01T00:00")
    parser.add_argument("--end", type=str, default=None, help="ISO timestamp, e.g. 2025-03-01T00:00")
    parser.add_argument("--db-path", type=str, default=settings.database_path)
    args = parser.parse_args()
    if bool(args.start) ^ bool(args.end):
        parser.error("--start and --end must be supplied together")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
