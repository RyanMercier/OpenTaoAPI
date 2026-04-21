from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from api.services.database import Database

router = APIRouter(tags=["embed"])

_db: Optional[Database] = None


def init_embed_router(db: Database):
    global _db
    _db = db


@router.get(
    "/embed/subnet/{netuid}/sparkline",
    summary="Inline SVG sparkline (no auth, embeddable)",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
async def sparkline(
    netuid: int,
    hours: int = Query(24, ge=1, le=8760),
    w: int = Query(240, ge=60, le=1200),
    h: int = Query(60, ge=20, le=600),
    stroke: str = Query("#00d4aa"),
):
    """Drop-in ``<img src="...">`` sparkline for subnet landing pages.
    No API key, cached for 60 s so heavy embedding doesn't thrash the
    database. Returns ``image/svg+xml``."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    rows = await _db.get_price_history(netuid, hours=hours, limit=500)
    if not rows:
        svg = _empty_svg(w, h)
    else:
        prices = [r["alpha_price_tao"] for r in rows if r["alpha_price_tao"] is not None]
        svg = _build_svg(prices, w, h, stroke) if prices else _empty_svg(w, h)

    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=60"},
    )


def _empty_svg(w: int, h: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
        f'<text x="{w//2}" y="{h//2}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="10" fill="#888">no data</text>'
        f'</svg>'
    )


def _build_svg(prices: list[float], w: int, h: int, stroke: str) -> str:
    lo, hi = min(prices), max(prices)
    span = hi - lo if hi > lo else 1.0
    pad = 2  # keep the stroke from clipping at the edges
    usable_h = h - pad * 2

    def y(price: float) -> float:
        # Invert: higher price -> lower y in SVG coordinates.
        return pad + usable_h * (1 - (price - lo) / span)

    n = len(prices)
    if n == 1:
        # Flat line across the middle for a single data point.
        points = f"0,{h/2:.2f} {w},{h/2:.2f}"
    else:
        step = w / (n - 1)
        points = " ".join(f"{i * step:.2f},{y(p):.2f}" for i, p in enumerate(prices))

    area = f"0,{h} {points} {w},{h}"
    # Strip leading "#" so the browser accepts the inline fill color without
    # URL-encoding gymnastics. Fall back to the default if input is weird.
    safe_stroke = stroke if all(c.isalnum() or c in "#()%,." for c in stroke) else "#00d4aa"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
        f'<polygon points="{area}" fill="{safe_stroke}" fill-opacity="0.1"/>'
        f'<polyline points="{points}" fill="none" stroke="{safe_stroke}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )
