# Architecture

OpenTaoAPI is a single FastAPI process holding three long-lived background
tasks: a subnet snapshot poller, a wallet poller, and (optionally) a
paper-trading runner. Reads come from cached chain queries or a local
SQLite. The three design decisions worth calling out are below.

```
                +-----------------+
                | Bittensor chain |
                | (AsyncSubtensor)|
                +--------+--------+
                         |
                         v
        +----------------+----------------+
        |          ChainClient            |
        |  (per-RPC timeout, TTL cache)   |
        +-+-------+-------+----------+----+
          |       |       |          |
          v       v       v          v
    +-----+--+ +--+--+ +--+----+ +---+----+
    | _live  | |wallet| |paper | |  REST  |
    | poller | |poller| |runner| |+ SSE   |
    +---+----+ +--+---+ +--+---+ +---+----+
        |         |        |         |
        v         v        v         v
    +---+---------+--------+--+   +--+--+
    |     SQLite (aiosqlite)  |   |Brkr |
    |  + asyncio.Lock writes  |   |fan- |
    +-------------------------+   |out  |
                                  +-----+
```

## Supervisor + per-RPC timeout

Every chain call routes through `ChainClient._call(...)`, which wraps the
underlying SDK call in `asyncio.wait_for(..., timeout=settings.rpc_timeout)`
(default 20 s). A single hung RPC therefore cannot block the event loop:
the wait_for cancels, the cache stores nothing, and the caller raises a
clean TimeoutError that the poller catches.

The poller itself runs inside `_poller_supervisor`. If the poller task
ever exits (clean return or exception), the supervisor restarts it after
a short backoff and increments `poll_state.poller_restarts`, exposed via
`/health`. When the poller has been failing long enough that the most
recent successful insert is older than 2x the expected cadence,
`/health` flips to HTTP 503. Wired to a Docker / Fly / Kubernetes
liveness probe, this restarts the container automatically. We pay
nothing in steady state and recover without manual intervention when the
public Finney RPC has a bad day.

The wallet poller and paper-trader runner use the same
supervisor + per-cycle-timeout pattern, with one extra detail: per-wallet
exponential backoff so a flaky wallet doesn't print a traceback every
60 s, while other wallets keep polling.

## Snapshot broker fan-out

The live SSE stream serves every connected client from a single
in-process `SnapshotBroker`. Each subscribe call creates a bounded queue
(default 256 slots); when the broker publishes a new snapshot it does
`q.put_nowait(event)` per subscriber. If a queue is full, we drop the
oldest event and retry the new one once.

The reason for the bounded queue + drop-oldest policy: a slow SSE client
should not be able to backpressure the snapshot poller. Without the cap,
a client that stops draining (say, a laptop closes its lid) would build
unbounded memory inside our process. Without the drop-oldest, the
poller would block waiting for the slow client. With both, the slow
client gets stale data, fast clients see fresh data, and the poller
always proceeds.

We also cap total subscribers at 256. Beyond that, subscribe raises
`BrokerFull` and the SSE handler returns 503. Self-hosted instances
serving a small audience never hit this; if you need more fanout, run a
reverse-proxy that handles SSE replication.

## Backfill job coalescing

Backfill jobs are launched on demand from the subnet detail page when
the candle data is sparse. `BackfillJobs.start(netuid, days)` checks an
in-memory `dict[netuid, asyncio.Task]`. If a job is already running for
that netuid, it returns the existing task instead of starting a new one.
Two operators clicking the same subnet at the same time get one job and
both wait for the same result.

The lock is per-netuid, not global, so backfilling SN5 doesn't block
backfilling SN64. Both can run in parallel; both can be observed via
`GET /api/v1/subnet/{n}/backfill`. When a job finishes, the entry is
cleared and a fresh request can start a new backfill if needed.

## Node failover

To swap chain providers, set `SUBTENSOR_ENDPOINT=ws://your-node:9944`
or `ARCHIVE_ENDPOINT=wss://your-archive` in the environment. Failover is
operator-managed: we do not implement automatic failover between RPCs
because the public Finney + public archive setup is good enough for
self-hosted use. If you're running a validator node, point at it; the
RPC timeout pattern means a flaky public node degrades cleanly to "no
new snapshots for a few minutes" rather than a hung process.

## Trading runner separation

The paper-trading runner (`_paper_trader_runner`) lives in the same
process as the API. The live-trading runner (CLI-launched) does NOT.
This is the security boundary: the FastAPI process never sees a coldkey.
The CLI loads the wallet locally, decrypts on stdin, and writes trades
to the same SQLite file the API reads from. Concurrent SQLite access
is safe because aiosqlite serializes writes through one
`asyncio.Lock`, and the kernel's file lock prevents cross-process
collisions on the few rare overlapping writes.
