import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.config import settings
from api.services.backfill_jobs import BackfillJobs
from api.services.broker import SnapshotBroker
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
from api.routes.stream import router as stream_router, init_stream_router
from api.routes.webhooks import router as webhooks_router, init_webhooks_router
from api.routes.embed import router as embed_router, init_embed_router
from api.routes.backfill import router as backfill_router, init_backfill_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

BLOCK_TIME_SECONDS = 12
POLL_TIMEOUT_SECONDS = 60  # per-cycle snapshot timeout

cache = TTLCache()
chain_client = ChainClient(cache)
price_client = PriceClient(cache)
database = Database(settings.database_path)
broker = SnapshotBroker()
backfill_jobs = BackfillJobs(database)

# Runtime state exposed via /health for external monitoring.
_poll_state = {
    "last_success": 0.0,
    "last_attempt": 0.0,
    "consecutive_failures": 0,
    "total_failures": 0,
    "total_successes": 0,
    "poller_restarts": 0,
}


async def _snapshot_all_subnets():
    """Snapshot every active subnet and insert into the database."""
    tao_price = await price_client.get_tao_price()
    all_sn = await chain_client.get_all_subnets_info()
    block = await chain_client.get_current_block()

    allowed: set[int] | None = None
    if settings.history_poll_netuids:
        allowed = {
            int(x.strip()) for x in settings.history_poll_netuids.split(",") if x.strip()
        }

    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for sn in all_sn:
        if allowed is not None and sn.netuid not in allowed:
            continue
        tao_in = float(sn.tao_in)
        alpha_in = float(sn.alpha_in)
        # Root subnet is 1:1 by convention. For every other subnet, an
        # empty alpha pool means the subnet is mid-init or broken; skip
        # the row rather than writing a zero that would poison OHLC bars.
        if sn.netuid == 0:
            price = 1.0
        elif alpha_in > 0:
            price = tao_in / alpha_in
        else:
            continue

        row = {
            "block": block,
            "timestamp": now,
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
        if await database.insert_snapshot(row):
            inserted += 1
            await broker.publish({
                "block": block,
                "timestamp": row["timestamp"],
                "netuid": sn.netuid,
                "alpha_price_tao": price,
                "tao_price_usd": tao_price,
                "tao_in": tao_in,
                "alpha_in": alpha_in,
            })

    return block, inserted


async def _live_poller():
    """Background poller with timeout-per-cycle and cache recovery.

    HISTORY_POLL_INTERVAL values:
      > 0  : poll every N seconds
      = 0  : disabled
      = -1 : poll every block (12s cadence)
    """
    interval = settings.history_poll_interval

    if interval == 0:
        logger.info("Live polling disabled (HISTORY_POLL_INTERVAL=0)")
        return

    if interval < 0:
        cadence = BLOCK_TIME_SECONDS
        logger.info("Live poller started (every block, ~12s cadence)")
    else:
        cadence = interval
        logger.info("Live poller started (every %ds)", cadence)

    last_block = 0

    while True:
        _poll_state["last_attempt"] = time.time()
        try:
            block, inserted = await asyncio.wait_for(
                _snapshot_all_subnets(),
                timeout=POLL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _poll_state["consecutive_failures"] += 1
            _poll_state["total_failures"] += 1
            logger.error(
                "Poll timed out after %ds (consecutive failures: %d). "
                "Resetting chain client cache.",
                POLL_TIMEOUT_SECONDS,
                _poll_state["consecutive_failures"],
            )
            # A hung RPC can leave cached futures in bad states. Flush them.
            await cache.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            _poll_state["consecutive_failures"] += 1
            _poll_state["total_failures"] += 1
            logger.exception(
                "Poll failed (consecutive failures: %d)",
                _poll_state["consecutive_failures"],
            )
        else:
            _poll_state["last_success"] = time.time()
            _poll_state["consecutive_failures"] = 0
            _poll_state["total_successes"] += 1
            if inserted > 0:
                logger.debug("Poll ok: +%d rows at block %d", inserted, block)
            elif interval < 0 and block == last_block:
                pass  # same block, skip log spam
            else:
                logger.debug("Poll ok: no new rows at block %d", block)
            last_block = block

        # If we're stuck, back off exponentially (capped at 5 min) to avoid
        # hammering a broken RPC.
        failures = _poll_state["consecutive_failures"]
        if failures > 0:
            sleep_s = min(cadence * (2 ** min(failures, 5)), 300)
        else:
            sleep_s = cadence
        await asyncio.sleep(sleep_s)


def _should_fire(direction: str, value: float, last: float | None, threshold: float) -> bool:
    """Return True if a webhook with the given `direction` should fire given
    the newly observed `value` and its previously stored `last` value."""
    if direction == "above":
        # Edge-trigger: only fire on the transition from <= to > threshold.
        return value > threshold and (last is None or last <= threshold)
    if direction == "below":
        return value < threshold and (last is None or last >= threshold)
    if direction == "cross_up":
        return last is not None and last <= threshold < value
    if direction == "cross_down":
        return last is not None and last >= threshold > value
    return False


async def _post_webhook(client: httpx.AsyncClient, url: str, payload: dict) -> bool:
    for attempt in range(3):
        try:
            resp = await client.post(url, json=payload, timeout=10.0)
            if resp.status_code < 400:
                return True
            logger.warning("Webhook %s returned %s", url, resp.status_code)
        except Exception as exc:  # noqa: BLE001, network errors are expected
            logger.warning("Webhook %s failed (attempt %d): %s", url, attempt + 1, exc)
        await asyncio.sleep(1 + attempt)
    return False


async def _webhook_evaluator():
    """Consume live snapshot events; fire any active webhook whose criteria
    cross on this event. The broker guarantees we see every inserted row."""
    async with broker.subscribe() as q, httpx.AsyncClient() as client:
        while True:
            event = await q.get()
            try:
                subs = await database.get_active_webhooks()
            except Exception:
                logger.exception("Evaluator: failed to load subscriptions")
                continue

            for sub in subs:
                if sub["netuid"] is not None and sub["netuid"] != event["netuid"]:
                    continue
                metric = sub["metric"]
                if metric == "market_cap_tao":
                    value = event.get("tao_in")
                else:
                    value = event.get(metric)
                if value is None:
                    continue

                last = sub.get("last_value")
                threshold = float(sub["threshold"])
                if not _should_fire(sub["direction"], value, last, threshold):
                    if last != value:
                        await database.update_webhook_value(sub["id"], value)
                    continue

                payload = {
                    "subscription_id": sub["id"],
                    "netuid": event["netuid"],
                    "metric": metric,
                    "value": value,
                    "threshold": threshold,
                    "direction": sub["direction"],
                    "timestamp": event["timestamp"],
                }
                await _post_webhook(client, sub["url"], payload)
                await database.update_webhook_fired(
                    sub["id"], value, datetime.now(timezone.utc).isoformat()
                )


async def _webhook_evaluator_supervisor():
    while True:
        try:
            await _webhook_evaluator()
            logger.info("Webhook evaluator exited cleanly")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Webhook evaluator crashed; restarting in 5s")
            await asyncio.sleep(5)


async def _poller_supervisor():
    """Restart the poller if it ever exits. This is the belt-and-suspenders
    against the 'container up but DB frozen' class of bugs."""
    while True:
        try:
            await _live_poller()
            # Normal return means polling was disabled. Exit cleanly.
            logger.info("Poller exited cleanly; supervisor stopping")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            _poll_state["poller_restarts"] += 1
            logger.exception(
                "Poller crashed (restart #%d); restarting in 5s",
                _poll_state["poller_restarts"],
            )
            await asyncio.sleep(5)


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
    init_stream_router(broker)
    init_webhooks_router(database)
    init_embed_router(database)
    init_backfill_router(backfill_jobs)

    # Initial snapshot populates the DB before serving requests.
    try:
        await asyncio.wait_for(_snapshot_all_subnets(), timeout=POLL_TIMEOUT_SECONDS)
        _poll_state["last_success"] = time.time()
        _poll_state["total_successes"] = 1
    except Exception:
        logger.exception("Initial snapshot failed; poller will retry")

    poller_task = asyncio.create_task(_poller_supervisor())
    evaluator_task = asyncio.create_task(_webhook_evaluator_supervisor())

    yield

    for task in (poller_task, evaluator_task):
        task.cancel()
    for task in (poller_task, evaluator_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await database.shutdown()
    await price_client.shutdown()
    await chain_client.shutdown()


app = FastAPI(
    title="OpenTaoAPI",
    description=(
        "**Self-hosted open-source alternative to TaoStats, TaoMarketCap, and tao.app.** "
        "Everything a hosted Bittensor analytics provider gives you, plus the "
        "integration primitives closed-source products can't offer.\n\n"
        "**What's here:**\n"
        "- Subnet prices, market caps, pool reserves (`tao_in` / `alpha_in`)\n"
        "- OHLC candles per subnet (`/subnet/{netuid}/candles`)\n"
        "- Miner + validator tables with daily emission estimates\n"
        "- Coldkey portfolios across every subnet\n"
        "- Historical snapshots (SQLite, epoch resolution)\n"
        "- Live SSE stream of every new snapshot (`/stream`)\n"
        "- Outbound webhooks on threshold crossings (`/webhooks/subscribe`)\n"
        "- Embeddable SVG sparkline widgets (`/embed/subnet/{netuid}/sparkline`)\n"
        "- TaoStats-compatible `/miner/{coldkey}/{netuid}` for drop-in replacement\n\n"
        "**Data sources:** Bittensor chain (AsyncSubtensor) + MEXC (TAO price)\n\n"
        "**Web UI:** Subnets dashboard at `/`, subnet detail at "
        "`/subnet/{netuid}`, webhooks manager at `/webhooks`, "
        "portfolio at `/portfolio/{coldkey}`\n\n"
        "**Backfilling history:** "
        "`python -m scripts.backfill --all-subnets --days 7 --concurrency 8`, "
        "then `python -m scripts.backfill_prices` to fill TAO/USD on old rows.\n\n"
        "**Source:** [github.com/ryanmercier/OpenTaoAPI](https://github.com/ryanmercier/OpenTaoAPI)"
    ),
    version="0.5.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "health", "description": "Liveness + poller freshness probe"},
        {"name": "subnet", "description": "Subnet info, metagraph, miners, and validators"},
        {"name": "history", "description": "Historical subnet data (prices, snapshots, OHLC candles)"},
        {"name": "stream", "description": "Live Server-Sent Events stream of snapshot inserts"},
        {"name": "webhooks", "description": "Subscribe to threshold-crossing events via outbound HTTP"},
        {"name": "embed", "description": "Embeddable widgets (inline SVG sparklines, no auth)"},
        {"name": "backfill", "description": "On-demand archive-node backfills per subnet"},
        {"name": "price", "description": "TAO price from MEXC"},
        {"name": "portfolio", "description": "Cross-subnet coldkey portfolio"},
        {"name": "miner", "description": "TaoStats-compatible miner endpoint"},
        {"name": "neuron", "description": "Neuron lookup by UID, hotkey, or coldkey"},
        {"name": "emissions", "description": "Daily/monthly emission estimates"},
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
app.include_router(stream_router, prefix="/api/v1")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(embed_router)  # mounted at root so <img src="..."> works
app.include_router(backfill_router, prefix="/api/v1")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/health", tags=["health"], summary="Liveness + poller freshness")
async def health() -> Response:
    """Liveness + freshness check. Returns **503** when ``stale`` is true,
    so Kubernetes liveness probes and Docker health checks restart the
    container automatically. ``stale`` flips true when the poller has not
    inserted a row within 2× its expected interval."""
    snapshots = await database.get_snapshot_count()
    now = time.time()
    age_seconds = now - _poll_state["last_success"] if _poll_state["last_success"] else None
    interval = settings.history_poll_interval
    expected_cadence = BLOCK_TIME_SECONDS if interval < 0 else (interval or 1800)
    stale = (age_seconds is None) or (age_seconds > expected_cadence * 2)

    body = {
        "status": "stale" if stale else "ok",
        "network": settings.bittensor_network,
        "historical_snapshots": snapshots,
        "poller": {
            "last_success_age_seconds": age_seconds,
            "consecutive_failures": _poll_state["consecutive_failures"],
            "total_successes": _poll_state["total_successes"],
            "total_failures": _poll_state["total_failures"],
            "poller_restarts": _poll_state["poller_restarts"],
            "expected_cadence_seconds": expected_cadence,
            "stale": stale,
        },
    }
    return JSONResponse(status_code=503 if stale else 200, content=body)


@app.get("/", include_in_schema=False)
async def landing():
    return FileResponse(FRONTEND_DIR / "subnets.html")


@app.get("/subnets", include_in_schema=False)
async def subnets_page():
    # Backwards-compat: the dashboard lives at both "/" and "/subnets".
    return FileResponse(FRONTEND_DIR / "subnets.html")


@app.get("/subnet/{netuid}", include_in_schema=False)
async def subnet_detail_page(netuid: int):
    return FileResponse(FRONTEND_DIR / "subnet-detail.html")


@app.get("/subnet/{netuid}/miners", include_in_schema=False)
async def miners_legacy(netuid: int):
    # Old URL. Send to the new detail page with the miners tab active.
    return RedirectResponse(
        url=f"/subnet/{netuid}?tab=miners",
        status_code=307,
    )


@app.get("/portfolio", include_in_schema=False)
async def portfolio_form():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/portfolio/{coldkey}", include_in_schema=False)
async def portfolio_page(coldkey: str):
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/coldkey/{coldkey}", include_in_schema=False)
async def coldkey_alias(coldkey: str):
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/webhooks", include_in_schema=False)
async def webhooks_page():
    return FileResponse(FRONTEND_DIR / "webhooks.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
