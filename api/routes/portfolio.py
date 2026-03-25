import asyncio
from collections import defaultdict

from fastapi import APIRouter, HTTPException

from api.models.schemas import PortfolioResponse, PortfolioSubnet
from api.services.calculations import calculate_emission
from api.services.chain_client import ChainClient
from api.services.price_client import PriceClient

router = APIRouter(tags=["portfolio"])

_chain_client: ChainClient | None = None
_price_client: PriceClient | None = None


def init_portfolio_router(chain_client: ChainClient, price_client: PriceClient):
    global _chain_client, _price_client
    _chain_client = chain_client
    _price_client = price_client


@router.get("/portfolio/{coldkey}", response_model=PortfolioResponse)
async def get_portfolio(coldkey: str):
    """Full cross-subnet portfolio for a coldkey. Returns total balance, free/staked breakdown,
    per-subnet alpha balances with TAO conversion, and estimated daily yield from mining/validation."""
    try:
        balance, stakes, tao_price = await asyncio.gather(
            _chain_client.get_balance(coldkey),
            _chain_client.get_stake_info_for_coldkey(coldkey),
            _price_client.get_tao_price(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    if not stakes:
        free_tao = float(balance.tao)
        return PortfolioResponse(
            coldkey=coldkey,
            total_balance_tao=free_tao,
            free_balance_tao=free_tao,
            total_staked_tao=0.0,
            tao_price_usd=tao_price,
            total_balance_usd=free_tao * tao_price,
            subnet_count=0,
            subnets=[],
        )

    # Group stakes by netuid
    by_netuid = defaultdict(list)
    for s in stakes:
        by_netuid[s.netuid].append(s)

    netuids = list(by_netuid.keys())

    # Fetch dynamic info + metagraphs for registered subnets concurrently
    registered_netuids = [
        n for n in netuids if any(s.is_registered for s in by_netuid[n])
    ]

    try:
        dyn_tasks = [_chain_client.get_dynamic_info(n) for n in netuids]
        meta_tasks = [_chain_client.get_metagraph(n) for n in registered_netuids]
        all_results = await asyncio.gather(*dyn_tasks, *meta_tasks, return_exceptions=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch subnet info: {e}")

    dyn_results = all_results[:len(netuids)]
    meta_results = all_results[len(netuids):]

    dyn_by_netuid = {}
    for n, r in zip(netuids, dyn_results):
        if not isinstance(r, Exception):
            dyn_by_netuid[n] = r

    metas = {}
    for n, r in zip(registered_netuids, meta_results):
        if not isinstance(r, Exception):
            metas[n] = r

    free_tao = float(balance.tao)
    total_staked_tao = 0.0
    subnets = []

    for n in sorted(netuids):
        dyn = dyn_by_netuid.get(n)
        if dyn is None:
            continue

        subnet_stakes = by_netuid[n]

        tao_in = float(dyn.tao_in)
        alpha_in = float(dyn.alpha_in)
        price = tao_in / alpha_in if alpha_in > 0 else 1.0

        name = ""
        symbol = ""
        if hasattr(dyn, 'subnet_name'):
            name = dyn.subnet_name or ""
        if not name and hasattr(dyn, 'name'):
            name = dyn.name or ""
        if hasattr(dyn, 'symbol'):
            symbol = dyn.symbol or ""

        if n == 0:
            name = name or "Staked TAO"
            symbol = symbol or "τ"
            price = 1.0

        total_alpha = sum(float(s.stake) for s in subnet_stakes)
        total_tao = total_alpha * price if n != 0 else total_alpha
        total_staked_tao += total_tao
        hotkey_count = len(subnet_stakes)

        # Compute daily yield using metagraph emission data
        daily_yield_tao = 0.0
        meta = metas.get(n)
        if meta:
            hotkey_to_uid = {meta.hotkeys[uid]: uid for uid in range(meta.n)}
            # SN0 emission is already TAO, not alpha
            em_tao_in = 1.0 if n == 0 else tao_in
            em_alpha_in = 1.0 if n == 0 else alpha_in

            for s in subnet_stakes:
                uid = hotkey_to_uid.get(s.hotkey_ss58)
                if uid is None or float(meta.E[uid]) <= 0:
                    continue

                em = calculate_emission(
                    float(meta.E[uid]), meta.tempo,
                    em_tao_in, em_alpha_in, tao_price,
                )

                # meta.E[uid] is total emission for the neuron.
                # For validators, scale by this coldkey's stake share.
                # For miners, the full emission belongs to the miner operator.
                is_validator = (
                    hasattr(meta, 'validator_permit')
                    and bool(meta.validator_permit[uid])
                )
                if is_validator:
                    total_stake = float(meta.alpha_stake[uid])
                    my_stake = float(s.stake)
                    share = my_stake / total_stake if total_stake > 0 else 0.0
                    daily_yield_tao += em.daily_tao * share
                else:
                    daily_yield_tao += em.daily_tao

        subnets.append(PortfolioSubnet(
            netuid=n,
            name=name,
            symbol=symbol,
            balance_alpha=total_alpha,
            balance_tao=total_tao,
            price_tao=price,
            value_usd=total_tao * tao_price,
            hotkey_count=hotkey_count,
            daily_yield_tao=daily_yield_tao,
            daily_yield_usd=daily_yield_tao * tao_price,
        ))

    # Sort by balance_tao descending
    subnets.sort(key=lambda s: s.balance_tao, reverse=True)

    total_balance_tao = free_tao + total_staked_tao

    return PortfolioResponse(
        coldkey=coldkey,
        total_balance_tao=total_balance_tao,
        free_balance_tao=free_tao,
        total_staked_tao=total_staked_tao,
        tao_price_usd=tao_price,
        total_balance_usd=total_balance_tao * tao_price,
        subnet_count=len(subnets),
        subnets=subnets,
    )
