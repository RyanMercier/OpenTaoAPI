"""
Historical data backfill from Bittensor chain via archive node.

Scrapes subnet pool snapshots at epoch-level resolution and stores in SQLite.
Resumable, handles rate limits with retries, supports all subnets.

Usage:
    # Single subnet, last 30 days
    python -m scripts.backfill --netuid 51 --days 30

    # All subnets from a start block
    python -m scripts.backfill --all-subnets --start-block 3000000

    # Resume all subnets from where they left off
    python -m scripts.backfill --all-subnets --resume

    # Include metagraph data (slower)
    python -m scripts.backfill --netuid 51 --days 7 --full
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import settings
from api.services.calculations import BLOCKS_PER_DAY
from api.services.database import Database

MAX_RETRIES = 3
RETRY_BACKOFF = 5


async def scrape_block(subtensor, netuid: int, block: int, full: bool = False):
    """Fetch subnet state at a specific block. Returns snapshot dict or None."""
    try:
        dyn = await subtensor.subnet(netuid=netuid, block=block)
        if dyn is None:
            return None

        tao_in = float(dyn.tao_in)
        alpha_in = float(dyn.alpha_in)
        price = tao_in / alpha_in if alpha_in > 0 else 0.0

        ts = await subtensor.get_timestamp(block=block)
        timestamp = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)

        total_stake = 0.0
        emission_rate = 0.0
        validator_count = 0
        neuron_count = 0

        if full:
            try:
                meta = await subtensor.metagraph(netuid=netuid, block=block, lite=True)
                if hasattr(meta, 'validator_permit'):
                    validator_count = int(sum(1 for i in range(meta.n) if meta.validator_permit[i]))
                total_stake = float(sum(meta.S)) if hasattr(meta, 'S') else 0.0
                emission_rate = float(sum(meta.E)) if hasattr(meta, 'E') else 0.0
                neuron_count = int(meta.n)
            except Exception as e:
                # Best-effort: the pool data is already captured, just drop
                # the metagraph-derived fields for this block and move on.
                print(f"  block {block} metagraph skipped: {e}")

        return {
            "block": block,
            "timestamp": timestamp,
            "netuid": netuid,
            "alpha_price_tao": price,
            "tao_price_usd": 0.0,
            "tao_in": tao_in,
            "alpha_in": alpha_in,
            "total_stake": total_stake,
            "emission_rate": emission_rate,
            "validator_count": validator_count,
            "neuron_count": neuron_count,
        }
    except Exception as e:
        msg = str(e)
        if "State discarded" in msg or "not found" in msg.lower():
            return None  # subnet didn't exist at this block
        raise  # let caller handle retries


async def scrape_with_retry(subtensor, netuid, block, full, delay):
    """Scrape a block with retries and backoff for rate limits."""
    for attempt in range(MAX_RETRIES):
        try:
            return await scrape_block(subtensor, netuid, block, full)
        except Exception as e:
            msg = str(e).lower()
            if attempt < MAX_RETRIES - 1 and ("rate" in msg or "limit" in msg or "timeout" in msg or "connection" in msg):
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"    Rate limited at block {block}, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
            else:
                print(f"    Block {block} error: {str(e)[:100]}")
                return None
    return None


async def backfill_subnet(subtensor, db, netuid, start, end, step, full, delay):
    """Backfill one subnet from start to end block."""
    total_blocks = max((end - start) // step, 1)
    print(f"\n  SN{netuid}: blocks {start} -> {end}, step {step}, ~{total_blocks} snapshots")

    scraped = 0
    skipped = 0
    errors = 0
    consecutive_nones = 0
    t0 = time.time()

    for block in range(start, end, step):
        snapshot = await scrape_with_retry(subtensor, netuid, block, full, delay)

        if snapshot:
            consecutive_nones = 0
            if await db.insert_snapshot(snapshot):
                scraped += 1
            else:
                skipped += 1
        else:
            errors += 1
            consecutive_nones += 1
            # If subnet doesn't exist for 20 consecutive blocks, skip ahead
            if consecutive_nones >= 20:
                print(f"    SN{netuid}: 20 consecutive failures at block {block}, subnet likely didn't exist yet. Skipping.")
                break

        done = (block - start) // step + 1
        if done % 100 == 0 or done == total_blocks:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            remaining = total_blocks - done
            eta_s = remaining / rate if rate > 0 else 0
            eta_h = eta_s / 3600
            print(
                f"    [{done}/{total_blocks}] "
                f"+{scraped} new, {skipped} dup, {errors} err "
                f"({rate:.1f}/s, ETA {eta_h:.1f}h)"
            )

        await asyncio.sleep(delay)

    print(f"  SN{netuid} done: +{scraped} new snapshots")
    return scraped


async def run_backfill(args):
    from bittensor.core.async_subtensor import AsyncSubtensor

    db = Database(args.db_path)
    await db.startup()

    network = args.endpoint or settings.archive_endpoint
    print(f"Connecting to {network}...")

    async with AsyncSubtensor(network=network) as subtensor:
        current_block = await subtensor.get_current_block()
        print(f"Current block: {current_block}")

        # Determine which subnets to backfill
        if args.all_subnets:
            subs = await subtensor.get_subnets()
            netuids = sorted([int(n) for n in subs if int(n) != 0])
            print(f"Backfilling {len(netuids)} subnets: {netuids[:10]}...")
        else:
            netuids = [args.netuid]

        step = args.step
        sem = asyncio.Semaphore(args.concurrency)

        async def _one(netuid: int) -> int:
            async with sem:
                if args.resume:
                    latest = await db.get_latest_block(netuid)
                    start = (latest + step) if latest else (args.start_block or 3000000)
                elif args.days:
                    start = current_block - (BLOCKS_PER_DAY * args.days)
                else:
                    start = args.start_block

                end = args.end_block or current_block
                if start >= end:
                    print(f"\n  SN{netuid}: already up to date")
                    return 0

                return await backfill_subnet(
                    subtensor, db, netuid, start, end, step, args.full, args.delay
                )

        results = await asyncio.gather(
            *[_one(n) for n in netuids], return_exceptions=True
        )
        grand_total = 0
        for netuid, result in zip(netuids, results):
            if isinstance(result, Exception):
                print(f"\n  SN{netuid}: failed — {result}")
            else:
                grand_total += result

        total_in_db = await db.get_snapshot_count()
        print(f"\nComplete. +{grand_total} new snapshots. Database total: {total_in_db}")

    await db.shutdown()

    if not args.skip_prices:
        # Chain scraper writes tao_price_usd=0 because MEXC has no per-block
        # quote. Automatically run the price backfill so a fresh operator
        # doesn't end up with silently-zero USD prices everywhere.
        from scripts.backfill_prices import run as backfill_prices_run

        class _PricesArgs:
            start = None
            end = None
            db_path = args.db_path

        print("\nFilling tao_price_usd from MEXC klines...")
        try:
            await backfill_prices_run(_PricesArgs())
        except Exception as e:
            print(f"  price backfill failed: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical subnet data from Bittensor archive node"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--netuid", type=int, help="Single subnet ID")
    group.add_argument("--all-subnets", action="store_true", help="Backfill all subnets")

    parser.add_argument("--start-block", type=int, default=None, help="Start block (default: 3000000)")
    parser.add_argument("--end-block", type=int, default=None, help="End block (default: current)")
    parser.add_argument("--step", type=int, default=360, help="Blocks between samples (default: 360 = 1 epoch)")
    parser.add_argument("--days", type=int, default=None, help="Backfill last N days instead of start-block")
    parser.add_argument("--resume", action="store_true", help="Resume each subnet from its last scraped block")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between RPC calls within a subnet (default: 1.0)")
    parser.add_argument("--concurrency", type=int, default=8, help="Number of subnets to backfill in parallel (default: 8)")
    parser.add_argument("--endpoint", type=str, default=None, help="Archive endpoint override")
    parser.add_argument("--full", action="store_true", help="Also fetch metagraph (slower)")
    parser.add_argument("--skip-prices", action="store_true", help="Don't auto-run scripts.backfill_prices after the chain scrape")
    parser.add_argument("--db-path", type=str, default=settings.database_path, help="Database path")

    args = parser.parse_args()

    if not args.start_block and not args.days and not args.resume:
        parser.error("Provide --start-block, --days, or --resume")

    asyncio.run(run_backfill(args))


if __name__ == "__main__":
    main()
