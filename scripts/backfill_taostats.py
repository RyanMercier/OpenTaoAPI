"""
Backfill historical data from TaoStats API into OpenTaoAPI's SQLite database.

Uses two API keys alternating to get 10 req/min effective rate.
Each request fetches a page of historical pool snapshots.

Usage:
    python -m scripts.backfill_taostats --netuid 51
    python -m scripts.backfill_taostats --netuid 51 --all-subnets
"""

import argparse
import asyncio
import sys
import time
from itertools import cycle
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import settings
from api.services.database import Database

TAOSTATS_BASE = "https://api.taostats.io/api"

API_KEYS = [
    "tao-3bb5ced7-3c26-42e3-9e7d-36dfd53e4f3b:78366fe8",
    "tao-f443355f-7fee-47a1-b895-92de3e4dec53:f7f5c8e6",
    "tao-3e9b5db8-2e8d-4db0-9cec-fbe957260446:af2660a5",
    "tao-b2f9516c-6e2d-47ef-b91c-f58c04103936:a238dd1b",
    "tao-b0995bbf-ff11-41f0-8f6d-0711ed4199b7:26a129b2",
]

# 5 req/min per key. With 5 keys round-robin: 25 req/min = 1 req per 2.5s
REQUEST_DELAY = 2.5


async def fetch_pool_history(
    client: httpx.AsyncClient,
    key_cycle,
    netuid: int,
    page: int = 1,
    limit: int = 1000,
) -> dict:
    """Fetch one page of pool history from TaoStats."""
    key = next(key_cycle)
    resp = await client.get(
        f"{TAOSTATS_BASE}/dtao/pool/history/v1",
        params={"netuid": netuid, "page": page, "limit": limit},
        headers={"Authorization": key},
    )
    resp.raise_for_status()
    return resp.json()


def taostats_to_snapshot(record: dict) -> dict:
    """Convert a TaoStats pool history record to our snapshot format."""
    price = float(record.get("price", 0))
    total_tao = int(record.get("total_tao", 0)) / 1e9
    alpha_in_pool = int(record.get("alpha_in_pool", 0)) / 1e9
    alpha_staked = int(record.get("alpha_staked", 0)) / 1e9
    total_alpha = int(record.get("total_alpha", 0)) / 1e9
    liquidity = int(record.get("liquidity", 0)) / 1e9

    return {
        "block": record["block_number"],
        "timestamp": record["timestamp"],
        "netuid": record["netuid"],
        "alpha_price_tao": price,
        "tao_price_usd": 0.0,  # TaoStats pool history doesn't include USD price
        "tao_in": total_tao,
        "alpha_in": alpha_in_pool,
        "total_stake": alpha_staked,
        "emission_rate": 0.0,
        "validator_count": 0,
        "neuron_count": 0,
    }


async def backfill_subnet(db: Database, client: httpx.AsyncClient, key_cycle, netuid: int):
    """Backfill all available history for a subnet."""
    print(f"\n--- Subnet {netuid} ---")

    # First request to get total pages
    data = await fetch_pool_history(client, key_cycle, netuid, page=1, limit=1000)
    pagination = data["pagination"]
    total_items = pagination["total_items"]
    total_pages = pagination["total_pages"]
    print(f"  TaoStats has {total_items} records across {total_pages} pages")

    if total_items == 0:
        print("  No data available")
        return 0

    # Process first page
    total_inserted = 0
    records = data.get("data", [])
    snapshots = [taostats_to_snapshot(r) for r in records]
    inserted = await db.insert_batch(snapshots)
    total_inserted += inserted
    print(f"  Page 1/{total_pages}: {len(records)} records, {inserted} new")

    # Fetch remaining pages
    for page in range(2, total_pages + 1):
        await asyncio.sleep(REQUEST_DELAY)

        try:
            data = await fetch_pool_history(client, key_cycle, netuid, page=page, limit=1000)
            records = data.get("data", [])
            snapshots = [taostats_to_snapshot(r) for r in records]
            inserted = await db.insert_batch(snapshots)
            total_inserted += inserted
            print(f"  Page {page}/{total_pages}: {len(records)} records, {inserted} new")
        except Exception as e:
            print(f"  Page {page} failed: {e}")
            await asyncio.sleep(REQUEST_DELAY)

    print(f"  Total: {total_inserted} new snapshots inserted for SN{netuid}")
    return total_inserted


async def get_all_netuids(client: httpx.AsyncClient, key_cycle) -> list[int]:
    """Get list of all subnet netuids from TaoStats."""
    key = next(key_cycle)
    resp = await client.get(
        f"{TAOSTATS_BASE}/dtao/pool/latest/v1",
        params={"limit": 200},
        headers={"Authorization": key},
    )
    resp.raise_for_status()
    data = resp.json()
    return [r["netuid"] for r in data.get("data", []) if r["netuid"] != 0]


async def run(args):
    db = Database(args.db_path)
    await db.startup()

    key_cycle = cycle(API_KEYS)

    async with httpx.AsyncClient(timeout=60.0) as client:
        if args.all_subnets:
            print("Fetching list of all subnets...")
            netuids = await get_all_netuids(client, key_cycle)
            await asyncio.sleep(REQUEST_DELAY)
            print(f"Found {len(netuids)} subnets")
        else:
            netuids = [args.netuid]

        grand_total = 0
        for netuid in netuids:
            inserted = await backfill_subnet(db, client, key_cycle, netuid)
            grand_total += inserted

        total_in_db = await db.get_snapshot_count()
        print(f"\nDone. {grand_total} new snapshots total. Database has {total_in_db} rows.")

    await db.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical data from TaoStats API"
    )
    parser.add_argument("--netuid", type=int, default=51, help="Subnet ID (default: 51)")
    parser.add_argument("--all-subnets", action="store_true", help="Backfill ALL subnets")
    parser.add_argument("--db-path", type=str, default=settings.database_path, help="Database path")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
