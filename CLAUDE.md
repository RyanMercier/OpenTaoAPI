# Context for the next Claude session

This file is your full briefing. Read it top to bottom before doing anything else.

## Writing rules (HARD, applies to everything you produce)

- **Never use em dashes (—, U+2014).** Not in code, comments, docstrings, markdown, chat output, commit messages, anything. The user shipped a public README full of them and got rightly furious. Use commas, colons, semicolons, periods, parentheses. If you feel the urge to write one, rewrite the sentence.
- **Never use en dashes (–, U+2013) for prose.** Number ranges only, and even then prefer ASCII ("10 to 30").
- Plain ASCII hyphens are always fine.
- No fancy unicode bullets, curly quotes, smart apostrophes, or unicode arrows unless the user explicitly asks. Middle dot (·) is acceptable in nav separators (we use it in `<title>` tags).
- Do not write "this is so that...", "comprehensive", "robust", "seamless", "leverage", or any other AI-tell phrasing. Write like a human who knows what they are doing.
- The global rule lives at `C:\Users\ryanm\.claude\CLAUDE.md`. This file inherits from it.

## What this project is

OpenTaoAPI: open-source self-hosted Bittensor explorer + API. Positioned as the alternative to TaoStats, TaoMarketCap, and tao.app. The differentiator is **integration primitives** (webhooks, Server-Sent Events stream, embeddable SVG widgets) that hosted closed-source providers structurally cannot offer.

- Repo: `github.com/ryanmercier/OpenTaoAPI` (MIT)
- Live hosted demo: `https://opentao.rpmsystems.io` (Fly.io)
- Project root in WSL: `/home/ryanm/Code/Claude/OpenTaoAPI/`
- Current version: v0.8.0, 56 routes
- Tech: FastAPI + Bittensor SDK (`AsyncSubtensor`) + aiosqlite + vanilla JS frontend + lightweight-charts (vendored). No build step.

## What's shipped so far

REST API with 22 documented endpoints + 4 frontend HTML pages + 13 internal/static routes. Full breakdown:

- **Core data**: `/api/v1/price/tao`, `/api/v1/subnets`, `/api/v1/subnet/{n}/info`, `/neurons`, `/metagraph`, `/miners`, `/validators`
- **TaoStats-compat**: `/api/v1/miner/{coldkey}/{netuid}` (drop-in)
- **Portfolio**: `/api/v1/portfolio/{coldkey}`
- **Neuron lookup**: by UID, hotkey, coldkey
- **Emissions**: `/api/v1/emissions/{n}/{uid}`
- **Historical**: `/api/v1/history/{n}/price|snapshots|stats`, `/api/v1/subnet/{n}/candles?interval=5m|15m|1h|4h|1d`
- **Live stream**: `GET /api/v1/stream` (SSE, filter by netuid)
- **Webhooks**: `POST /api/v1/webhooks/subscribe`, `GET /api/v1/webhooks`, `GET/DELETE /api/v1/webhooks/{id}`
- **Embeds**: `GET /embed/subnet/{n}/sparkline` (inline SVG, no auth)
- **On-demand backfill**: `POST/GET /api/v1/subnet/{n}/backfill?days=30`

Frontend pages: `/` (subnets dashboard, default landing), `/subnet/{n}` (detail with candlestick chart + miners/validators tabs + embed snippet + alerts modal), `/webhooks` (management UI), `/portfolio/{coldkey}`.

## Hardening already in place

Don't redo any of these without checking first:

- Per-RPC timeouts in ChainClient (`settings.rpc_timeout=20s`) so a hung archive call can't block the event loop
- Poller supervisor that auto-restarts on unhandled exit (`_poller_supervisor` in `api/main.py`)
- `/health` returns HTTP 503 when stale so Docker/Fly/K8s healthchecks restart automatically
- `asyncio.Lock` around all DB write paths (poller + webhook evaluator share one aiosqlite connection)
- SSRF guard on `/webhooks/subscribe` (rejects loopback, RFC1918, link-local, cloud metadata services)
- Pydantic bounds on threshold/netuid/URL length
- 256-subscriber cap on the SSE broker
- Auto-trigger backfill on subnet detail page when candles are sparse, coalesced per-netuid so duplicate clicks reuse the existing job

## The next 10 days (talk for novelty search)

The user is presenting at novelty search. Three gaps to close before then, in priority order. **Do these in order, do not skip ahead.**

### Days 1 to 2: benchmarks

Pick 5 to 7 representative endpoints and run 100 to 1000 requests each against the live demo at `opentao.rpmsystems.io`. Measure p50/p95/p99 latency. Then run equivalent calls against TaoStats's free tier where possible.

Endpoints to benchmark:
- `/api/v1/subnets`
- `/api/v1/subnet/{n}/info`
- `/api/v1/subnet/{n}/miners`
- `/api/v1/portfolio/{coldkey}`
- `/api/v1/miner/{coldkey}/{n}` (TaoStats has the equivalent at `dash.taostats.io/api/miner/{coldkey}/{n}`)
- `/api/v1/subnet/{n}/candles?interval=1h&hours=168`

Honest framing the user already wants: warm cache (sub-50ms realistic for us thanks to the in-memory TTLCache) vs cold (`?refresh=true` is 10 to 20 s because of AsyncSubtensor sync). Where TaoStats requires auth, note as a structural difference rather than a latency comparison.

Save the benchmark script under `scripts/benchmark.py` and the results as a markdown table. Use `httpx.AsyncClient` with a semaphore. The user has TaoStats keys saved in earlier conversation history if you ask but be respectful of their 5 req/min free tier so don't burn keys.

### Day 3: README rewrite

Three structural changes:

1. **Lead with webhooks/SSE/embeds.** Current opening is "self-hosted alternative" which underplays the moat. Reframe as: integration primitives that hosted services structurally cannot match, and self-hosted is just how that becomes possible.
2. **Move the benchmark table to right after the pitch**, before the feature list. Numbers > adjectives.
3. **Move the existing comparison table to AFTER the benchmarks.** Currently it's buried near the bottom but is also positioned too high relative to the numbers.

The README is at `/home/ryanm/Code/Claude/OpenTaoAPI/README.md`. Do not regress on the em dash purge already done. Run `grep -n '[—–]' README.md` after every edit.

### Day 4: ARCHITECTURE.md

New file at the repo root. One page (under ~250 lines). Pick the three most non-obvious design decisions and explain each in a focused section:

1. **Supervisor + per-RPC timeout pattern**: why we wrap `_live_poller` in `_poller_supervisor`, why timeouts are at the chain-client level, what happens when the public Finney RPC hangs (poll cycle aborts fast, cache clears, supervisor counts the restart, `/health` flips to 503). The user explicitly asked this be surfaced rather than buried in env var docs.
2. **Snapshot broker fan-out**: why we have an in-process pub/sub, how the bounded queue + drop-oldest policy keeps a slow SSE client from backpressuring the poller, why we cap subscribers.
3. **On-demand backfill job coalescing**: why `BackfillJobs.start()` returns the existing job instead of starting a duplicate when the same subnet is requested twice, why the lock is per-netuid not global.

Also include a node failover paragraph explicitly: "to swap chain providers, set `SUBTENSOR_ENDPOINT=ws://...` or `ARCHIVE_ENDPOINT=...`. Failover is operator-managed; we do not implement automatic failover between RPCs because the public Finney + public archive setup is good enough for self-hosted use."

### After Day 4 (only if you have time)

The Bittensor audience will ask about correctness. Add a `tests/` directory with at least one regression test for the portfolio silent-drop bug (we just fixed that, see "recent fixes" below). pytest + httpx async client.

## Recent fixes (do not regress)

These are the bugs we fixed in the last few days. If your changes touch these areas, verify the fix still holds.

- **Portfolio silently dropped subnets** when the parallel `asyncio.gather` lost a subnet's dynamic_info. Now retries sequentially per-subnet and emits a degraded row (alpha visible, price 0) rather than dropping. `api/routes/portfolio.py`, search for `gather dropped SN`.
- **scalecodec vs bittensor 9.12 dep conflict**: bittensor pulls `bt-decode` (Rust SCALE) via `async-substrate-interface` which collides with the explicit `scalecodec` pin we used to have. Removed the pin; `_ss58_to_hex` in `api/routes/miner.py` probes scalecodec, substrateinterface, and `bittensor_wallet.Keypair` in order and returns `""` if none import. Do not re-add `scalecodec` to requirements.txt.
- **Em dash purge**: zero em dashes and zero en dashes in the entire repo. Run `grep -rn "[—–]" .` to verify before committing.
- **CoinMarketCap to TaoMarketCap**: the README used to compare against CoinMarketCap which is wrong. TaoMarketCap is the right counterparty and they have more features than the original table credited (sparklines, holder breakdowns, miner/validator tables, portfolio view). The corrected table is honest about that.

## File map (most important paths)

- `api/main.py` lifespan, supervisor wiring, broker, route registration. The "wire everything" file.
- `api/services/chain_client.py` AsyncSubtensor wrapper with per-RPC `_call(..., timeout)` shim.
- `api/services/database.py` aiosqlite + write lock + schema. `subnet_snapshots` and `webhook_subscriptions` tables.
- `api/services/broker.py` SSE fan-out broker with per-subscriber bounded queue.
- `api/services/backfill_jobs.py` per-netuid lock for on-demand backfills triggered by the UI.
- `api/services/calculations.py` emission math, alpha-to-tao conversion.
- `api/services/price_client.py` MEXC live + historical klines.
- `api/services/cache.py` in-memory TTL cache used by chain_client.
- `api/services/metagraph_compat.py` SDK version shim (older bittensor used short attr names).
- `api/routes/` one file per logical area. `portfolio.py` is the trickiest; `miner.py` is the TaoStats-compat shape.
- `api/models/schemas.py` pydantic models, all request/response shapes.
- `scripts/backfill.py` archive-node historical scraper. Auto-runs `backfill_prices` at the end unless `--skip-prices`.
- `scripts/backfill_prices.py` MEXC kline fill for `tao_price_usd` on rows that were inserted with 0.
- `frontend/subnets.html` default landing, sparklines + SSE live ticker.
- `frontend/subnet-detail.html` candlestick chart with auto-backfill, modal alerts, embed tab.
- `frontend/webhooks.html` create + list + delete subscriptions.
- `frontend/index.html` portfolio (demoted to `/portfolio`).
- `frontend/common.css` shared styles. `.btn`, `.btn-primary`, `.btn-danger`, `.tabs`, `.modal`, `.flash`, etc.
- `frontend/vendor/lightweight-charts.standalone.production.js` v4.2.3 vendored. Don't bump to v5 without verifying the API change for `addCandlestickSeries` to `addSeries(CandlestickSeries, ...)`.
- `docs/deploy-fly.md` Fly.io deployment runbook.
- `SECURITY.md` threat model.

## How to start the dev server

```bash
conda activate tao
cd ~/Code/Claude/OpenTaoAPI
uvicorn api.main:app --host 0.0.0.0 --port 8009 --log-level info
```

Use port 8009, not 8000. The user runs the production Docker container on 8000, and you'll fight it for the port if you don't pick a different one.

The first request after boot blocks for 10 to 30 seconds while the metagraph syncs. `/health` reports `stale=true` during that window which is correct.

## How to run benchmarks against the live demo

The hosted demo runs at `https://opentao.rpmsystems.io` on Fly.io with a 3 GB volume. It has the live poller running so historical data accumulates. Don't hammer it from a benchmark loop running on the same host without coordinating with the user; they're paying for the egress.

For benchmarks specifically, fine to run 1000 sequential requests at low concurrency (max 4) over a few minutes. Put benchmarks under `scripts/benchmark.py`. Save raw timings as CSV and the summary table in markdown. Reference the methodology in the README so reviewers can reproduce.

## User preferences

- Terse, direct, no fluff. Don't apologize unless something genuinely went wrong. When something went wrong, be specific about what and why.
- The user calls out AI tells immediately and is annoyed by them. If you find yourself writing a sentence with em dashes, "robust", "comprehensive", or three-clause "first... second... third..." structures, rewrite it.
- The user is a software engineer applying for Bittensor infra/analytics roles. The README + ARCHITECTURE.md doubles as a portfolio piece. Write everything as if Yuma Rao might read it.
- Do not invent features. If the user says X exists, verify it. The TaoMarketCap feature table earlier was wrong because the previous Claude assumed instead of checking.
- Run smoke tests after non-trivial changes. `python -c "from api.main import app; print(len(app.routes))"` is the cheapest one. `curl -s http://localhost:8009/health` is the second.

## What is explicitly NOT in scope

Do not start any of these without asking:

- A real test suite (mentioned as "if you have time" only)
- Switching from SQLite to Postgres
- Adding a paid tier or any auth layer
- Rewriting the frontend in React/Vue/anything else
- Replacing FastAPI
- Replacing aiosqlite with anything async-postgres
- Bumping bittensor to a new major
- Re-introducing scalecodec to requirements.txt

## Final checks before you ship anything

```bash
# em dash purge
grep -rn '[—–]' . --include='*.py' --include='*.md' --include='*.html' --include='*.css' --include='*.yml'

# imports clean
python -c 'from api.main import app; print(f"v{app.version}, {len(app.routes)} routes")'

# server boots
uvicorn api.main:app --host 0.0.0.0 --port 8009 &
sleep 30
curl -fsS http://localhost:8009/health
```

If any of those three fail, fix it before continuing.
