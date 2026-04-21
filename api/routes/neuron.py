import asyncio
import logging

from fastapi import APIRouter, HTTPException

from api.models.schemas import NeuronResponse
from api.services.calculations import alpha_to_tao, calculate_emission
from api.services.chain_client import ChainClient
from api.services.metagraph_compat import meta_get, meta_get_uid
from api.services.price_client import PriceClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["neuron"])

_chain_client: ChainClient | None = None
_price_client: PriceClient | None = None


def init_neuron_router(chain_client: ChainClient, price_client: PriceClient):
    global _chain_client, _price_client
    _chain_client = chain_client
    _price_client = price_client


def _build_neuron_response(meta, dyn, uid: int, tao_price: float) -> NeuronResponse:
    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)

    alpha_stake = meta_get(meta, "alpha_stake")
    if alpha_stake is None:
        alpha_stake = meta_get(meta, "S")
    stake = float(alpha_stake[uid]) if alpha_stake is not None else 0.0
    emission_alpha = meta_get_uid(meta, "E", uid)

    em = calculate_emission(emission_alpha, meta.tempo, tao_in, alpha_in, tao_price)

    axon = ""
    if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
        ax = meta.axons[uid]
        if hasattr(ax, 'ip') and hasattr(ax, 'port'):
            axon = f"{ax.ip}:{ax.port}"

    is_validator = False
    vp = meta_get(meta, "validator_permit")
    if vp is not None:
        is_validator = bool(vp[uid])

    active_vec = meta_get(meta, "active")
    last_update_vec = meta_get(meta, "last_update")

    return NeuronResponse(
        netuid=meta.netuid,
        uid=uid,
        hotkey=meta.hotkeys[uid],
        coldkey=meta.coldkeys[uid],
        stake=stake,
        stake_as_tao=alpha_to_tao(stake, tao_in, alpha_in),
        incentive=meta_get_uid(meta, "I", uid),
        consensus=meta_get_uid(meta, "C", uid),
        trust=meta_get_uid(meta, "T", uid),
        emission_per_epoch=emission_alpha,
        emission_per_epoch_as_tao=alpha_to_tao(emission_alpha, tao_in, alpha_in),
        daily_alpha=em.daily_alpha,
        daily_tao=em.daily_tao,
        daily_usd=em.daily_usd,
        axon=axon,
        active=bool(active_vec[uid]) if active_vec is not None else True,
        last_update=int(last_update_vec[uid]) if last_update_vec is not None else 0,
        validator_permit=is_validator,
        dividends=meta_get_uid(meta, "D", uid),
        rank=meta_get_uid(meta, "R", uid),
    )


# Specific routes MUST come before the parameterized /neuron/{netuid}/{uid}
# route, otherwise FastAPI matches "coldkey" and "hotkey" as {netuid}.

@router.get("/neuron/hotkey/{hotkey}", response_model=list[NeuronResponse])
async def get_neuron_by_hotkey(hotkey: str):
    """Find neurons by hotkey across all subnets."""
    try:
        all_subnets = await _chain_client.get_all_subnets_info()
        netuids = [s.netuid for s in all_subnets if hasattr(s, 'netuid')]
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch subnet list")

    tao_price = await _price_client.get_tao_price()

    async def _check_subnet(netuid: int):
        try:
            meta, dyn = await asyncio.gather(
                _chain_client.get_metagraph(netuid),
                _chain_client.get_dynamic_info(netuid),
            )
            for uid in range(meta.n):
                if meta.hotkeys[uid] == hotkey:
                    return _build_neuron_response(meta, dyn, uid, tao_price)
        except Exception as e:
            logger.debug("Subnet %d skipped during hotkey lookup: %s", netuid, e)
        return None

    checks = await asyncio.gather(*[_check_subnet(n) for n in netuids])
    results = [r for r in checks if r is not None]

    if not results:
        raise HTTPException(status_code=404, detail=f"Hotkey {hotkey} not found")

    return results


@router.get("/neuron/coldkey/{coldkey}", response_model=list[NeuronResponse])
async def get_neurons_by_coldkey(coldkey: str):
    """Find all neurons owned by a coldkey across all subnets."""
    try:
        stakes = await _chain_client.get_stake_info_for_coldkey(coldkey)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to get stake info: {e}")

    if not stakes:
        raise HTTPException(status_code=404, detail=f"No stakes found for coldkey {coldkey}")

    tao_price = await _price_client.get_tao_price()
    results = []

    netuids = set(s.netuid for s in stakes)
    for netuid in netuids:
        try:
            meta, dyn = await asyncio.gather(
                _chain_client.get_metagraph(netuid),
                _chain_client.get_dynamic_info(netuid),
            )
        except Exception as e:
            logger.warning("Skipping subnet %d for coldkey lookup: %s", netuid, e)
            continue

        hotkey_to_uid = {meta.hotkeys[uid]: uid for uid in range(meta.n)}

        for stake_info in stakes:
            if stake_info.netuid != netuid:
                continue
            hk = stake_info.hotkey_ss58
            if hk in hotkey_to_uid:
                uid = hotkey_to_uid[hk]
                results.append(_build_neuron_response(meta, dyn, uid, tao_price))

    return results


@router.get("/neuron/{netuid}/{uid}", response_model=NeuronResponse)
async def get_neuron(netuid: int, uid: int):
    """Single neuron details by subnet and UID."""
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

    return _build_neuron_response(meta, dyn, uid, tao_price)
