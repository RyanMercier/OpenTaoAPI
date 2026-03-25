import asyncio

from fastapi import APIRouter, HTTPException

from api.models.schemas import EmissionResponse
from api.services.calculations import calculate_emission
from api.services.chain_client import ChainClient
from api.services.price_client import PriceClient

router = APIRouter(tags=["emissions"])

_chain_client: ChainClient | None = None
_price_client: PriceClient | None = None


def init_emissions_router(chain_client: ChainClient, price_client: PriceClient):
    global _chain_client, _price_client
    _chain_client = chain_client
    _price_client = price_client


@router.get("/emissions/{netuid}/{uid}", response_model=EmissionResponse)
async def get_emissions(netuid: int, uid: int):
    """Emission breakdown for a neuron: alpha/block, TAO/block, daily/monthly estimates in alpha, TAO, and USD."""
    try:
        meta, dyn, tao_price = await asyncio.gather(
            _chain_client.get_metagraph(netuid),
            _chain_client.get_dynamic_info(netuid),
            _price_client.get_tao_price(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    if uid < 0 or uid >= meta.n:
        raise HTTPException(status_code=404, detail=f"UID {uid} not found in subnet {netuid}")

    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)
    rate = tao_in / alpha_in if alpha_in > 0 else 0.0

    em = calculate_emission(
        meta_e_uid=float(meta.E[uid]),
        tempo=meta.tempo,
        tao_in=tao_in,
        alpha_in=alpha_in,
        tao_price_usd=tao_price,
    )

    return EmissionResponse(
        netuid=netuid,
        uid=uid,
        hotkey=meta.hotkeys[uid],
        alpha_per_epoch=em.alpha_per_epoch,
        alpha_per_block=em.alpha_per_block,
        tao_per_block=em.tao_per_block,
        daily_alpha=em.daily_alpha,
        daily_tao=em.daily_tao,
        daily_usd=em.daily_usd,
        monthly_tao=em.monthly_tao,
        monthly_usd=em.monthly_usd,
        alpha_to_tao_rate=rate,
        tao_price_usd=tao_price,
    )
