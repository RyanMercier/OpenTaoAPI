import ipaddress
import socket
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

from api.models.schemas import WebhookSubscribeRequest, WebhookSubscribeResponse
from api.services.database import Database

router = APIRouter(tags=["webhooks"])

_db: Optional[Database] = None

VALID_METRICS = {"alpha_price_tao", "tao_in", "alpha_in", "market_cap_tao"}
VALID_DIRECTIONS = {"above", "below", "cross_up", "cross_down"}
MAX_URL_LEN = 2048


def init_webhooks_router(db: Database):
    global _db
    _db = db


def _require_db() -> Database:
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return _db


def _validate_webhook_url(url: str) -> None:
    """Reject URLs that resolve to private, loopback, or metadata-service
    addresses. This is defense-in-depth against SSRF; the endpoint is
    already documented as self-host-only, but a single misconfigured proxy
    shouldn't turn it into a credentials-harvesting tool."""
    if len(url) > MAX_URL_LEN:
        raise HTTPException(status_code=400, detail=f"url too long (max {MAX_URL_LEN})")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="url has no hostname")

    # Block AWS IMDS and friends by hostname regardless of DNS result.
    if host.lower() in {"metadata.google.internal", "metadata.goog"}:
        raise HTTPException(status_code=400, detail="url targets a metadata service")

    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"could not resolve host {host}")

    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HTTPException(
                status_code=400,
                detail=f"url resolves to a non-public address ({ip})",
            )


def _row_to_response(row: dict) -> WebhookSubscribeResponse:
    return WebhookSubscribeResponse(
        id=row["id"],
        url=row["url"],
        metric=row["metric"],
        threshold=row["threshold"],
        direction=row["direction"],
        netuid=row["netuid"],
        created_at=row["created_at"],
        active=bool(row["active"]),
        last_value=row.get("last_value"),
        last_fired_at=row.get("last_fired_at"),
    )


@router.post(
    "/webhooks/subscribe",
    response_model=WebhookSubscribeResponse,
    summary="Subscribe a webhook to threshold crossings",
)
async def subscribe(req: WebhookSubscribeRequest):
    """Register an outbound HTTP POST that fires when ``metric`` crosses
    ``threshold`` in the given ``direction`` (``above``, ``below``,
    ``cross_up``, ``cross_down``). Supported metrics:
    ``alpha_price_tao``, ``tao_in``, ``alpha_in``, ``market_cap_tao``.
    Set ``netuid`` to scope to a single subnet, or omit for all subnets.

    Fires are edge-triggered: one POST per crossing, not one per poll.

    **Security:** the endpoint accepts arbitrary public URLs, so do not
    expose this route publicly without an auth proxy in front. Private,
    loopback, link-local, and cloud metadata addresses are rejected as a
    defense-in-depth measure."""
    db = _require_db()
    if req.metric not in VALID_METRICS:
        raise HTTPException(
            status_code=400,
            detail=f"metric must be one of {sorted(VALID_METRICS)}",
        )
    if req.direction not in VALID_DIRECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"direction must be one of {sorted(VALID_DIRECTIONS)}",
        )
    _validate_webhook_url(req.url)
    if req.netuid is not None and req.netuid < 0:
        raise HTTPException(status_code=400, detail="netuid must be >= 0")

    created_at = datetime.now(timezone.utc).isoformat()
    sub_id = await db.create_webhook(
        url=req.url,
        metric=req.metric,
        threshold=req.threshold,
        direction=req.direction,
        netuid=req.netuid,
        created_at=created_at,
    )
    row = await db.get_webhook(sub_id)
    return _row_to_response(row)


@router.get(
    "/webhooks",
    response_model=list[WebhookSubscribeResponse],
    summary="List all active webhook subscriptions",
)
async def list_webhooks():
    rows = await _require_db().get_active_webhooks()
    return [_row_to_response(r) for r in rows]


@router.get(
    "/webhooks/{sub_id}",
    response_model=WebhookSubscribeResponse,
    summary="Look up a webhook subscription",
)
async def get_webhook(sub_id: int):
    row = await _require_db().get_webhook(sub_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Webhook {sub_id} not found")
    return _row_to_response(row)


@router.delete(
    "/webhooks/{sub_id}",
    summary="Deactivate a webhook subscription",
)
async def delete_webhook(sub_id: int):
    removed = await _require_db().deactivate_webhook(sub_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Webhook {sub_id} not found")
    return {"id": sub_id, "active": False}
