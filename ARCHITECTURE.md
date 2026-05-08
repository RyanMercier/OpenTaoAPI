# Architecture

One FastAPI process, three long-lived background tasks (subnet snapshot
poller, wallet poller, paper-trading runner), one SQLite file. Reads
come from cached chain queries or the local DB. Three things worth
calling out:

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

Every chain call goes through `ChainClient._call(...)`, which wraps the
SDK call in `asyncio.wait_for(..., timeout=settings.rpc_timeout)`
(default 20 s). A hung RPC can't block the event loop: wait_for cancels,
the cache stores nothing, and the poller catches the TimeoutError and
moves on.

The poller itself runs inside `_poller_supervisor`. If the poller task
exits for any reason, the supervisor restarts it after a short backoff
and bumps `poll_state.poller_restarts`, which is exposed via `/health`.
Once the poller has been failing long enough that the most recent
successful insert is older than 2x the expected cadence, `/health`
flips to HTTP 503. Wire that to a Docker / Fly / Kubernetes liveness
probe and the container restarts itself. Steady state costs nothing,
and a bad day on the public Finney RPC doesn't need a human.

The wallet poller and the paper-trader runner use the same
supervisor + per-cycle timeout pattern, plus per-wallet exponential
backoff so one flaky coldkey doesn't print a traceback every 60 s while
the others keep polling.

## Snapshot broker fan-out

The live SSE stream serves every connected client from one in-process
`SnapshotBroker`. Each subscribe call gets its own bounded queue
(default 256 slots). When a new snapshot lands, the broker calls
`q.put_nowait(event)` on each queue. If a queue is full we drop the
oldest event and retry the new one once.

The bounded queue + drop-oldest combination exists so a slow SSE client
can't backpressure the poller. A client that stops draining (laptop lid
closes, network hiccups) would otherwise either grow our memory
without bound or stall the poller while it waits. With drop-oldest the
slow client gets stale data, fast clients keep getting fresh data, and
the poller always moves forward.

There's also a hard cap of 256 subscribers per broker. Past that,
subscribe raises `BrokerFull` and the SSE handler returns 503.
Self-hosted instances serving a small audience won't hit this; if you
need more fanout, put a reverse proxy in front that handles SSE
replication.

## Backfill job coalescing

Backfill jobs kick off on demand from the subnet detail page when the
candle data is sparse. `BackfillJobs.start(netuid, days)` checks an
in-memory `dict[netuid, asyncio.Task]`. If a job is already running for
that netuid we return the existing task instead of starting a second
one, so two operators clicking the same subnet get one job and both
wait for the same result.

The lock is per-netuid, not global, so a backfill on SN5 doesn't block
one on SN64. Both run in parallel and either can be polled via
`GET /api/v1/subnet/{n}/backfill`. When a job finishes the entry
clears and the next request can start fresh.

## Node failover

To swap chain providers, set `SUBTENSOR_ENDPOINT=ws://your-node:9944`
or `ARCHIVE_ENDPOINT=wss://your-archive` in the environment. Failover
is operator-managed; there's no automatic failover between RPCs because
the public Finney + public archive setup is fine for self-hosted use.
If you're running a validator node, point at that. The RPC timeout
pattern means a flaky public node degrades to "no new snapshots for a
few minutes" rather than a hung process.

## Trading runner separation

The paper-trading runner (`_paper_trader_runner`) runs in-process with
the API. The live-trading runner does not; it's CLI-launched in its
own process. That's the security boundary. The FastAPI process never
sees a coldkey. The CLI loads the wallet locally, decrypts the key on
stdin, and writes trades to the same SQLite file the API reads from.
Concurrent SQLite access is safe because aiosqlite serializes writes
through one `asyncio.Lock`, and the kernel's file lock handles the rare
cross-process write overlaps.
