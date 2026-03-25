import asyncio

from fastapi import APIRouter, HTTPException

from api.models.schemas import NeuronResponse
from api.services.calculations import alpha_to_tao, calculate_emission
from api.services.chain_client import ChainClient
from api.services.price_client import PriceClient

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
    stake = float(meta.alpha_stake[uid]) if hasattr(meta, 'alpha_stake') else float(meta.S[uid])
    emission_alpha = float(meta.E[uid])

    em = calculate_emission(emission_alpha, meta.tempo, tao_in, alpha_in, tao_price)

    axon = ""
    if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
        ax = meta.axons[uid]
        if hasattr(ax, 'ip') and hasattr(ax, 'port'):
            axon = f"{ax.ip}:{ax.port}"

    is_validator = False
    if hasattr(meta, 'validator_permit'):
        is_validator = bool(meta.validator_permit[uid])

    return NeuronResponse(
        netuid=meta.netuid,
        uid=uid,
        hotkey=meta.hotkeys[uid],
        coldkey=meta.coldkeys[uid],
        stake=stake,
        stake_as_tao=alpha_to_tao(stake, tao_in, alpha_in),
        incentive=float(meta.I[uid]),
        consensus=float(meta.C[uid]),
        trust=float(meta.T[uid]),
        emission_per_epoch=emission_alpha,
        emission_per_epoch_as_tao=alpha_to_tao(emission_alpha, tao_in, alpha_in),
        daily_alpha=em.daily_alpha,
        daily_tao=em.daily_tao,
        daily_usd=em.daily_usd,
        axon=axon,
        active=bool(meta.active[uid]) if hasattr(meta, 'active') else True,
        last_update=int(meta.last_update[uid]) if hasattr(meta, 'last_update') else 0,
        validator_permit=is_validator,
        dividends=float(meta.D[uid]) if hasattr(meta, 'D') else 0.0,
        rank=float(meta.R[uid]) if hasattr(meta, 'R') else 0.0,
    )


@router.get("/neuron/{netuid}/{uid}", response_model=NeuronResponse)
async def get_neuron(netuid: int, uid: int):
    """Single neuron details by subnet and UID. Includes stake, incentive, emission, and daily yield."""
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
        except Exception:
            pass
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

    # Group stakes by netuid for efficiency
    netuids = set(s.netuid for s in stakes)
    for netuid in netuids:
        try:
            meta, dyn = await asyncio.gather(
                _chain_client.get_metagraph(netuid),
                _chain_client.get_dynamic_info(netuid),
            )
        except Exception:
            continue

        # Build hotkey->uid map
        hotkey_to_uid = {meta.hotkeys[uid]: uid for uid in range(meta.n)}

        for stake_info in stakes:
            if stake_info.netuid != netuid:
                continue
            hk = stake_info.hotkey_ss58
            if hk in hotkey_to_uid:
                uid = hotkey_to_uid[hk]
                results.append(_build_neuron_response(meta, dyn, uid, tao_price))

    return results
