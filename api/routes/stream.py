import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from api.services.broker import BrokerFull, SnapshotBroker

router = APIRouter(tags=["stream"])

_broker: Optional[SnapshotBroker] = None

HEARTBEAT_SECONDS = 15


def init_stream_router(broker: SnapshotBroker):
    global _broker
    _broker = broker


@router.get(
    "/stream",
    summary="Live SSE feed of snapshot inserts",
)
async def stream_snapshots(
    request: Request,
    netuid: list[int] = Query(default=[]),
):
    """Server-Sent Events stream of every snapshot row the live poller
    inserts. Each event body is the snapshot as JSON. Pass
    ``?netuid=1&netuid=2`` (repeatable) to filter; omit the parameter
    to receive every subnet. Heartbeat comment ``: ping`` every 15 s so
    proxies and load balancers keep the connection open.

    **Capacity:** the server accepts up to 256 concurrent subscribers. If
    that cap is hit, the connection stays open just long enough to emit
    one ``event: error\\ndata: subscriber_cap_reached`` SSE record and
    then closes. Clients should reconnect with backoff."""
    if _broker is None:
        raise HTTPException(status_code=503, detail="Broker not initialised")

    wanted: set[int] | None = set(netuid) if netuid else None

    async def event_source():
        try:
            ctx = _broker.subscribe()
            q = await ctx.__aenter__()
        except BrokerFull:
            # Emit a single well-formed SSE error event so well-behaved
            # clients can see the reason before the stream closes.
            yield b"event: error\ndata: subscriber_cap_reached\n\n"
            return
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    continue
                if wanted is not None and event.get("netuid") not in wanted:
                    continue
                yield f"data: {json.dumps(event)}\n\n".encode()
        finally:
            await ctx.__aexit__(None, None, None)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
