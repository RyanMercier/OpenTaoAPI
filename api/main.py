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
from api.services.portfolio_service import compute_portfolio
from api.services.price_client import PriceClient

from api.routes.price import router as price_router, init_price_router
from api.routes.miner import router as miner_router, init_miner_router
from api.routes.neuron import router as neuron_router, init_neuron_router
from api.routes.subnet import router as subnet_router, init_subnet_router
from api.routes.emissions import router as emissions_router, init_emissions_router
from api.routes.portfolio import router as portfolio_router, init_portfolio_router
from api.routes.wallets import router as wallets_router, init_wallets_router
from api.routes.history import router as history_router, init_history_router
from api.routes.stream import router as stream_router, init_stream_router
from api.routes.webhooks import router as webhooks_router, init_webhooks_router
from api.routes.embed import router as embed_router, init_embed_router
from api.routes.backfill import router as backfill_router, init_backfill_router
from api.routes.paper import router as paper_router, init_paper_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

BLOCK_TIME_SECONDS = 12
POLL_TIMEOUT_SECONDS = 60  # per-cycle snapshot timeout
WALLET_POLL_TIMEOUT_SECONDS = 90  # per-wallet portfolio compute can be slow
WALLET_POLLER_TICK_SECONDS = 60  # how often we wake to check the wallet queue

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


_wallet_backoff: dict[str, dict] = {}
WALLET_BACKOFF_BASE_SECONDS = 60
WALLET_BACKOFF_CAP_SECONDS = 1800   # 30 min cap


async def _wallet_poller():
    """Walk the ``tracked_wallets`` table on a slow tick and snapshot any
    wallet whose interval has elapsed. One row in
    ``wallet_portfolio_snapshots`` per cycle per wallet. Errors on a
    single wallet are caught and logged; the supervisor only restarts if
    the loop itself crashes.

    Per-wallet exponential backoff stops a flaky chain from producing a
    traceback every minute. Expected failures (timeouts, transient
    substrate-interface KeyErrors) print a one-liner; unexpected
    exceptions still get a full traceback.
    """
    logger.info(
        "Wallet poller started (tick %ds, default per-wallet 5min)",
        WALLET_POLLER_TICK_SECONDS,
    )
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            due = await database.get_wallets_due_for_poll(now_iso)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Wallet poller: failed to load tracked wallets")
            await asyncio.sleep(WALLET_POLLER_TICK_SECONDS)
            continue

        now_ts = time.time()
        for wallet in due:
            coldkey = wallet["coldkey_ss58"]

            # Skip if a prior failure has us in the penalty window.
            backoff = _wallet_backoff.get(coldkey)
            if backoff and now_ts < backoff["resume_at"]:
                continue

            try:
                portfolio, block = await asyncio.wait_for(
                    compute_portfolio(chain_client, price_client, coldkey),
                    timeout=WALLET_POLL_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, RuntimeError) as e:
                # Expected: chain RPC timed out, or compute_portfolio
                # wrapped a chain failure. One warning line, back off.
                _bump_wallet_backoff(coldkey, now_ts)
                resume_in = int(_wallet_backoff[coldkey]["resume_at"] - now_ts)
                reason = "timeout" if isinstance(e, asyncio.TimeoutError) else f"chain error ({e})"
                logger.warning(
                    "Wallet poller: %s for %s (failures=%d, retry in %ds)",
                    reason, coldkey,
                    _wallet_backoff[coldkey]["failures"], resume_in,
                )
                await database.mark_wallet_polled(coldkey, now_iso)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Unexpected: full traceback so we can debug.
                _bump_wallet_backoff(coldkey, now_ts)
                logger.exception(
                    "Wallet poller: unexpected error for %s", coldkey
                )
                await database.mark_wallet_polled(coldkey, now_iso)
                continue

            _wallet_backoff.pop(coldkey, None)

            row = {
                "coldkey_ss58": coldkey,
                "block": block,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_balance_tao": portfolio.total_balance_tao,
                "free_balance_tao": portfolio.free_balance_tao,
                "total_staked_tao": portfolio.total_staked_tao,
                "tao_price_usd": portfolio.tao_price_usd,
                "total_balance_usd": portfolio.total_balance_usd,
                "subnet_count": portfolio.subnet_count,
            }
            await database.insert_wallet_snapshot(row)
            await database.mark_wallet_polled(coldkey, now_iso)

        await asyncio.sleep(WALLET_POLLER_TICK_SECONDS)


def _bump_wallet_backoff(coldkey: str, now_ts: float) -> None:
    state = _wallet_backoff.get(coldkey, {"failures": 0, "resume_at": 0.0})
    state["failures"] += 1
    delay = min(
        WALLET_BACKOFF_BASE_SECONDS * (2 ** min(state["failures"] - 1, 5)),
        WALLET_BACKOFF_CAP_SECONDS,
    )
    state["resume_at"] = now_ts + delay
    _wallet_backoff[coldkey] = state


async def _wallet_poller_supervisor():
    while True:
        try:
            await _wallet_poller()
            logger.info("Wallet poller exited cleanly; supervisor stopping")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Wallet poller crashed; restarting in 5s")
            await asyncio.sleep(5)


# --- Paper trading runner -------------------------------------------------

_paper_traders: dict = {}  # portfolio_id -> PaperTrader instance


async def _paper_trader_runner():
    """Advance every active paper portfolio one cycle when its interval
    elapses. ``_paper_traders`` keeps trader instances alive between
    cycles so position state and strategy internal counters survive."""
    if not settings.paper_trading_enabled:
        logger.info("Paper trader disabled (PAPER_TRADING_ENABLED=false)")
        return

    # Lazy import: a paper-trading-disabled instance shouldn't pay the
    # cost of importing the trading package at startup.
    import json as _json
    from api.trading.paper_trader import PaperTrader, hydrate_portfolio
    from api.trading.strategies import load_external_strategies

    if settings.opentao_external_strategies:
        load_external_strategies(settings.opentao_external_strategies)

    logger.info("Paper trader runner started")

    while True:
        try:
            portfolios = await database.list_paper_portfolios(active_only=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Paper trader: failed to load portfolios")
            await asyncio.sleep(60)
            continue

        now_ts = time.time()
        next_due_in = 600.0  # max time to sleep when nothing is due

        for row in portfolios:
            pid = row["id"]
            cfg_dict = {}
            try:
                cfg_dict = _json.loads(row["config_json"])
            except Exception:
                logger.warning("Paper portfolio %d has unparseable config_json", pid)

            interval = int(cfg_dict.get("paper_poll_interval_seconds", 1800))

            last = row.get("last_cycle_at")
            if last:
                try:
                    last_ts = datetime.fromisoformat(last).timestamp()
                except Exception:
                    last_ts = 0.0
                age = now_ts - last_ts
                if age < interval:
                    next_due_in = min(next_due_in, max(60.0, interval - age))
                    continue

            trader = _paper_traders.get(pid)
            if trader is None:
                config = _build_trading_config(cfg_dict, row)
                portfolio = await hydrate_portfolio(database, pid, config)
                trader = PaperTrader(
                    portfolio_id=pid,
                    config=config,
                    portfolio=portfolio,
                    chain_client=chain_client,
                    price_client=price_client,
                    database=database,
                )
                _paper_traders[pid] = trader

            try:
                result = await asyncio.wait_for(trader.run_once(), timeout=180)
                logger.info(
                    "Paper portfolio %d cycle: %s", pid, result
                )
            except asyncio.TimeoutError:
                logger.warning("Paper portfolio %d cycle timed out", pid)
                # Advance last_cycle_at so we don't hot-loop.
                await database.update_paper_portfolio_runtime(
                    pid,
                    peak_value=trader.portfolio.peak_value,
                    free_tao=trader.portfolio.free_tao,
                    hotkey_cooldowns=trader.portfolio.hotkey_cooldowns,
                    last_cycle_at=datetime.now(timezone.utc).isoformat(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Paper portfolio %d cycle failed", pid)
                # Mark cycled so we back off.
                try:
                    await database.update_paper_portfolio_runtime(
                        pid,
                        peak_value=trader.portfolio.peak_value,
                        free_tao=trader.portfolio.free_tao,
                        hotkey_cooldowns=trader.portfolio.hotkey_cooldowns,
                        last_cycle_at=datetime.now(timezone.utc).isoformat(),
                    )
                except Exception:
                    logger.exception(
                        "Paper portfolio %d: also failed to update last_cycle_at", pid
                    )

            next_due_in = min(next_due_in, float(interval))

        # Drop traders for portfolios that are no longer active.
        active_ids = {p["id"] for p in portfolios}
        for stale_id in list(_paper_traders):
            if stale_id not in active_ids:
                _paper_traders.pop(stale_id, None)

        await asyncio.sleep(max(60.0, min(next_due_in, 600.0)))


def _build_trading_config(cfg_dict: dict, portfolio_row: dict):
    """Build a TradingConfig from a portfolio row plus its stored
    ``config_json``. Defaults come from the dataclass; per-portfolio
    overrides win."""
    from api.trading.config import TradingConfig
    config = TradingConfig()
    config.db_path = settings.database_path
    config.initial_capital_tao = float(
        portfolio_row.get("initial_capital_tao", config.initial_capital_tao)
    )
    for attr in (
        "strategies", "max_positions", "max_single_position_pct",
        "reserve_pct", "max_position_pct_of_pool", "max_slippage_pct",
        "num_hotkeys", "external_strategy_paths",
        "paper_poll_interval_seconds",
    ):
        if attr in cfg_dict and cfg_dict[attr] is not None:
            setattr(config, attr, cfg_dict[attr])
    return config


async def _paper_trader_supervisor():
    while True:
        try:
            await _paper_trader_runner()
            logger.info("Paper trader runner exited cleanly; supervisor stopping")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Paper trader runner crashed; restarting in 30s")
            await asyncio.sleep(30)


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
    init_portfolio_router(chain_client, price_client, database)
    init_wallets_router(database)
    init_history_router(database)
    init_stream_router(broker)
    init_webhooks_router(database)
    init_embed_router(database)
    init_backfill_router(backfill_jobs)
    init_paper_router(database)

    # Initial snapshot populates the DB before serving requests.
    try:
        await asyncio.wait_for(_snapshot_all_subnets(), timeout=POLL_TIMEOUT_SECONDS)
        _poll_state["last_success"] = time.time()
        _poll_state["total_successes"] = 1
    except Exception:
        logger.exception("Initial snapshot failed; poller will retry")

    poller_task = asyncio.create_task(_poller_supervisor())
    evaluator_task = asyncio.create_task(_webhook_evaluator_supervisor())
    wallet_task = asyncio.create_task(_wallet_poller_supervisor())
    paper_task = asyncio.create_task(_paper_trader_supervisor())

    yield

    for task in (poller_task, evaluator_task, wallet_task, paper_task):
        task.cancel()
    for task in (poller_task, evaluator_task, wallet_task, paper_task):
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
    version="0.8.0",
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
app.include_router(wallets_router, prefix="/api/v1")
app.include_router(paper_router, prefix="/api/v1")
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
    inserted a row within 2x its expected interval."""
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


@app.get("/wallets", include_in_schema=False)
async def wallets_page():
    return FileResponse(FRONTEND_DIR / "wallets.html")


@app.get("/paper", include_in_schema=False)
async def paper_index_page():
    return FileResponse(FRONTEND_DIR / "paper.html")


@app.get("/paper/{portfolio_id}", include_in_schema=False)
async def paper_detail_page(portfolio_id: int):
    return FileResponse(FRONTEND_DIR / "paper.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
