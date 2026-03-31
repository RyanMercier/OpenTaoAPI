import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.config import settings
from api.services.cache import TTLCache
from api.services.chain_client import ChainClient
from api.services.database import Database
from api.services.price_client import PriceClient

from api.routes.price import router as price_router, init_price_router
from api.routes.miner import router as miner_router, init_miner_router
from api.routes.neuron import router as neuron_router, init_neuron_router
from api.routes.subnet import router as subnet_router, init_subnet_router
from api.routes.emissions import router as emissions_router, init_emissions_router
from api.routes.portfolio import router as portfolio_router, init_portfolio_router
from api.routes.history import router as history_router, init_history_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

cache = TTLCache()
chain_client = ChainClient(cache)
price_client = PriceClient(cache)
database = Database(settings.database_path)


async def _snapshot_all_subnets():
    """Take a snapshot of all active subnets and store in the database."""
    try:
        tao_price = await price_client.get_tao_price()
        all_sn = await chain_client.get_all_subnets_info()
        block = await chain_client.get_current_block()

        netuids = [s.netuid for s in all_sn if hasattr(s, 'netuid')]

        # Filter to configured subnets if set
        if settings.history_poll_netuids:
            allowed = set(int(x.strip()) for x in settings.history_poll_netuids.split(",") if x.strip())
            netuids = [n for n in netuids if n in allowed]

        inserted = 0
        for sn in all_sn:
            if sn.netuid not in netuids:
                continue
            tao_in = float(sn.tao_in)
            alpha_in = float(sn.alpha_in)
            price = tao_in / alpha_in if alpha_in > 0 else (1.0 if sn.netuid == 0 else 0.0)

            from datetime import datetime, timezone
            snapshot = {
                "block": block,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "netuid": sn.netuid,
                "alpha_price_tao": price,
                "tao_price_usd": tao_price,
                "tao_in": tao_in,
                "alpha_in": alpha_in,
                "total_stake": 0.0,
                "emission_rate": 0.0,
                "validator_count": 0,
                "neuron_count": 0,
            }
            if await database.insert_snapshot(snapshot):
                inserted += 1

        if inserted > 0:
            logger.info(f"Live poll: saved {inserted} snapshots at block {block}")
    except Exception as e:
        logger.error(f"Live poll failed: {e}")


async def _live_poller():
    """Background task that snapshots subnet state.

    Modes controlled by HISTORY_POLL_INTERVAL:
      > 0  — poll every N seconds (default 1800 = 30min)
      = 0  — disabled
      = -1 — every block (~12s), subscribes to new block headers
    """
    interval = settings.history_poll_interval

    if interval == 0:
        logger.info("Live history polling disabled (interval=0)")
        return

    if interval == -1:
        logger.info("Live history poller started (every block)")
        last_block = 0
        while True:
            try:
                block = await chain_client.get_current_block()
                if block != last_block:
                    last_block = block
                    await _snapshot_all_subnets()
            except Exception as e:
                logger.error(f"Block poller error: {e}")
            await asyncio.sleep(12)  # block time ~12s
        return

    logger.info(f"Live history poller started (every {interval}s)")
    while True:
        await asyncio.sleep(interval)
        await _snapshot_all_subnets()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await chain_client.startup()
    await price_client.startup()
    await database.startup()

    init_price_router(price_client)
    init_miner_router(chain_client, price_client)
    init_neuron_router(chain_client, price_client)
    init_subnet_router(chain_client, price_client)
    init_emissions_router(chain_client, price_client)
    init_portfolio_router(chain_client, price_client)
    init_history_router(database)

    # Take an initial snapshot, then start background poller
    await _snapshot_all_subnets()
    poller_task = asyncio.create_task(_live_poller())

    yield

    poller_task.cancel()
    await database.shutdown()
    await price_client.shutdown()
    await chain_client.shutdown()


app = FastAPI(
    title="OpenTaoAPI",
    description=(
        "Open-source Bittensor network explorer API. "
        "Self-hostable alternative to TaoStats and TaoMarketCap.\n\n"
        "**Data sources:** Bittensor chain (AsyncSubtensor) + MEXC (TAO price)\n\n"
        "**Web UI:** Portfolio viewer at `/`, subnets at `/subnets`, "
        "miners/validators at `/subnet/{netuid}/miners`\n\n"
        "**Historical data:** Daily snapshots back to Feb 2025 (SQLite). "
        "Backfill with `python -m scripts.backfill_taostats --all-subnets`, "
        "live polling every 30min."
    ),
    version="0.3.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "price", "description": "TAO price from MEXC"},
        {"name": "portfolio", "description": "Cross-subnet coldkey portfolio"},
        {"name": "miner", "description": "TaoStats-compatible miner endpoint"},
        {"name": "neuron", "description": "Neuron lookup by UID, hotkey, or coldkey"},
        {"name": "subnet", "description": "Subnet info, metagraph, miners, and validators"},
        {"name": "emissions", "description": "Daily/monthly emission estimates"},
        {"name": "history", "description": "Historical subnet data (price, stake, emissions)"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(price_router, prefix="/api/v1")
app.include_router(miner_router, prefix="/api/v1")
app.include_router(neuron_router, prefix="/api/v1")
app.include_router(subnet_router, prefix="/api/v1")
app.include_router(emissions_router, prefix="/api/v1")
app.include_router(portfolio_router, prefix="/api/v1")
app.include_router(history_router, prefix="/api/v1")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/health")
async def health():
    count = await database.get_snapshot_count()
    return {
        "status": "ok",
        "network": settings.bittensor_network,
        "historical_snapshots": count,
    }


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/subnets")
async def subnets_page():
    return FileResponse(FRONTEND_DIR / "subnets.html")


@app.get("/subnet/{netuid}/miners")
async def miners_page(netuid: int):
    return FileResponse(FRONTEND_DIR / "miners.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
