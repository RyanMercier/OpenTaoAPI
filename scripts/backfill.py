"""
Historical data backfill script for OpenTaoAPI.

Scrapes subnet snapshots from the Bittensor chain at epoch-level resolution
and stores them in SQLite. Resumable — skips blocks already in the database.

Usage:
    python -m scripts.backfill --netuid 51 --start-block 7000000 --step 360
    python -m scripts.backfill --netuid 51 --days 7          # last 7 days
    python -m scripts.backfill --netuid 51 --resume           # continue from last block

Rate limiting: 0.5s between queries by default. Safe for public Finney RPC.
For your own subtensor node, use --delay 0.
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import settings
from api.services.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("backfill")
logger.setLevel(logging.INFO)

BLOCKS_PER_DAY = 7200


async def scrape_block(
    subtensor, netuid: int, block: int, tao_price: float = 0.0, full: bool = False,
):
    """Fetch subnet state at a specific block and return a snapshot dict.

    By default fetches only pool data (subnet()) which is fast.
    With full=True, also fetches metagraph for stake/emission/neuron counts (slower).
    """
    try:
        dyn = await subtensor.subnet(netuid=netuid, block=block)
        if dyn is None:
            return None

        tao_in = float(dyn.tao_in)
        alpha_in = float(dyn.alpha_in)
        price = tao_in / alpha_in if alpha_in > 0 else 0.0

        # Get block timestamp from chain
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
                print(f"Metagraph fetch failed at block {block}: {e}")

        return {
            "block": block,
            "timestamp": timestamp,
            "netuid": netuid,
            "alpha_price_tao": price,
            "tao_price_usd": tao_price,
            "tao_in": tao_in,
            "alpha_in": alpha_in,
            "total_stake": total_stake,
            "emission_rate": emission_rate,
            "validator_count": validator_count,
            "neuron_count": neuron_count,
        }
    except Exception as e:
        print(f"Block {block}: {e}")
        return None


async def run_backfill(args):
    from bittensor.core.async_subtensor import AsyncSubtensor

    db = Database(args.db_path)
    await db.startup()

    # Connect to chain
    network = args.endpoint or settings.subtensor_endpoint or settings.bittensor_network
    print(f"Connecting to {network}...")

    async with AsyncSubtensor(network=network) as subtensor:
        current_block = await subtensor.get_current_block()
        print(f"Current block: {current_block}")

        # Determine start block
        if args.resume:
            latest = await db.get_latest_block(args.netuid)
            if latest:
                start = latest + args.step
                print(f"Resuming from block {start} (last: {latest})")
            else:
                start = args.start_block or (current_block - BLOCKS_PER_DAY * 30)
                print(f"No history found, starting from {start}")
        elif args.days:
            start = current_block - (BLOCKS_PER_DAY * args.days)
            print(f"Backfilling last {args.days} days from block {start}")
        else:
            start = args.start_block
            print(f"Starting from block {start}")

        end = args.end_block or current_block
        step = args.step
        total_blocks = (end - start) // step
        print(
            f"Scraping SN{args.netuid}: blocks {start} → {end}, "
            f"step {step}, ~{total_blocks} snapshots"
        )

        scraped = 0
        skipped = 0
        errors = 0
        t0 = time.time()

        for block in range(start, end, step):
            snapshot = await scrape_block(subtensor, args.netuid, block, full=args.full)
            if snapshot:
                inserted = await db.insert_snapshot(snapshot)
                if inserted:
                    scraped += 1
                else:
                    skipped += 1
            else:
                errors += 1

            # Progress
            done = (block - start) // step + 1
            if done % 20 == 0 or done == total_blocks:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total_blocks - done) / rate if rate > 0 else 0
                print(
                    f"  [{done}/{total_blocks}] "
                    f"scraped={scraped} skipped={skipped} errors={errors} "
                    f"({rate:.1f} blocks/s, ETA {eta:.0f}s)"
                )

            await asyncio.sleep(args.delay)

    total_in_db = await db.get_snapshot_count()
    print(
        f"Done. Inserted {scraped} new snapshots. "
        f"Total in database: {total_in_db}"
    )
    await db.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical subnet data from Bittensor chain"
    )
    parser.add_argument("--netuid", type=int, required=True, help="Subnet ID")
    parser.add_argument("--start-block", type=int, default=None, help="Start block")
    parser.add_argument("--end-block", type=int, default=None, help="End block (default: current)")
    parser.add_argument("--step", type=int, default=360, help="Blocks between samples (default: 360 = 1 epoch)")
    parser.add_argument("--days", type=int, default=None, help="Backfill last N days")
    parser.add_argument("--resume", action="store_true", help="Resume from last scraped block")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between RPC calls (default: 0.5)")
    parser.add_argument("--endpoint", type=str, default=None, help="Subtensor endpoint override")
    parser.add_argument("--full", action="store_true", help="Also fetch metagraph (slower, adds stake/emission/neuron data)")
    parser.add_argument("--db-path", type=str, default=settings.database_path, help="Database path")

    args = parser.parse_args()

    if not args.start_block and not args.days and not args.resume:
        parser.error("Provide --start-block, --days, or --resume")

    asyncio.run(run_backfill(args))


if __name__ == "__main__":
    main()
