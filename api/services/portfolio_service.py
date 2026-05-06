"""Shared portfolio computation used by both the live route and the
background wallet poller.

The original logic lived in ``api/routes/portfolio.py`` as the
``get_portfolio`` handler. Pulling it out so the poller can reuse it
without going through HTTP. The route is now a thin wrapper.
"""
import asyncio
import logging
from collections import defaultdict
from typing import Tuple

from api.models.schemas import PortfolioResponse, PortfolioSubnet
from api.services.calculations import calculate_emission
from api.services.chain_client import ChainClient
from api.services.metagraph_compat import meta_get, meta_get_uid
from api.services.price_client import PriceClient

logger = logging.getLogger(__name__)


async def compute_portfolio(
    chain_client: ChainClient,
    price_client: PriceClient,
    coldkey: str,
) -> Tuple[PortfolioResponse, int]:
    """Build a full cross-subnet portfolio for a coldkey.

    Returns ``(PortfolioResponse, current_block)``. The block is captured
    in the same gather so a snapshot row can be keyed against it; without
    it the poller would have to do a second RPC just to know what block
    the data corresponds to.

    Raises ``RuntimeError`` if the initial chain calls fail; callers should
    map that to a 502 (route) or skip the cycle (poller).
    """
    try:
        balance, stakes, tao_price, current_block = await asyncio.gather(
            chain_client.get_balance(coldkey),
            chain_client.get_stake_info_for_coldkey(coldkey),
            price_client.get_tao_price(),
            chain_client.get_current_block(),
        )
    except Exception as e:
        raise RuntimeError(f"Chain query failed: {e}") from e

    free_tao = float(balance.tao) if hasattr(balance, "tao") else float(balance)

    if not stakes:
        return (
            PortfolioResponse(
                coldkey=coldkey,
                total_balance_tao=free_tao,
                free_balance_tao=free_tao,
                total_staked_tao=0.0,
                tao_price_usd=tao_price,
                total_balance_usd=free_tao * tao_price,
                subnet_count=0,
                subnets=[],
            ),
            current_block,
        )

    by_netuid = defaultdict(list)
    for s in stakes:
        by_netuid[s.netuid].append(s)

    netuids = list(by_netuid.keys())

    registered_netuids = [
        n for n in netuids if any(
            getattr(s, "is_registered", True) for s in by_netuid[n]
        )
    ]

    # return_exceptions=True so one hung RPC doesn't poison the whole batch.
    dyn_tasks = [chain_client.get_dynamic_info(n) for n in netuids]
    meta_tasks = [chain_client.get_metagraph(n) for n in registered_netuids]
    all_results = await asyncio.gather(
        *dyn_tasks, *meta_tasks, return_exceptions=True
    )

    dyn_results = all_results[: len(netuids)]
    meta_results = all_results[len(netuids):]

    dyn_by_netuid: dict[int, object] = {}
    for n, r in zip(netuids, dyn_results):
        if isinstance(r, Exception):
            logger.warning(
                "Portfolio: gather dropped SN%d (%s); retrying sequentially", n, r
            )
        else:
            dyn_by_netuid[n] = r

    # Sequential retry for any subnet that fell out of the gather. The
    # response cannot silently omit real stake positions.
    missing = [n for n in netuids if n not in dyn_by_netuid]
    for n in missing:
        try:
            dyn_by_netuid[n] = await chain_client.get_dynamic_info(n)
        except Exception as e:
            logger.error("Portfolio: dynamic_info unavailable for SN%d: %s", n, e)

    metas: dict[int, object] = {}
    for n, r in zip(registered_netuids, meta_results):
        if not isinstance(r, Exception):
            metas[n] = r

    total_staked_tao = 0.0
    subnets: list[PortfolioSubnet] = []

    for n in sorted(netuids):
        subnet_stakes = by_netuid[n]
        total_alpha = sum(float(s.stake) for s in subnet_stakes)
        hotkey_count = len(subnet_stakes)

        dyn = dyn_by_netuid.get(n)
        if dyn is None:
            logger.warning(
                "Portfolio: emitting SN%d with unknown price (%.4f alpha unpriced)",
                n, total_alpha,
            )
            subnets.append(PortfolioSubnet(
                netuid=n,
                name=f"SN {n}",
                symbol="",
                balance_alpha=total_alpha,
                balance_tao=0.0,
                price_tao=0.0,
                value_usd=0.0,
                hotkey_count=hotkey_count,
                daily_yield_tao=0.0,
                daily_yield_usd=0.0,
            ))
            continue

        tao_in = float(dyn.tao_in)
        alpha_in = float(dyn.alpha_in)
        price = tao_in / alpha_in if alpha_in > 0 else 1.0

        name = ""
        symbol = ""
        if hasattr(dyn, "subnet_name"):
            name = dyn.subnet_name or ""
        if not name and hasattr(dyn, "name"):
            name = dyn.name or ""
        if hasattr(dyn, "symbol"):
            symbol = dyn.symbol or ""

        if n == 0:
            name = name or "Staked TAO"
            symbol = symbol or "τ"
            price = 1.0

        total_tao = total_alpha * price if n != 0 else total_alpha
        total_staked_tao += total_tao

        daily_yield_tao = 0.0
        meta = metas.get(n)
        if meta:
            hotkey_to_uid = {meta.hotkeys[uid]: uid for uid in range(meta.n)}
            em_tao_in = 1.0 if n == 0 else tao_in
            em_alpha_in = 1.0 if n == 0 else alpha_in

            vp_vec = meta_get(meta, "validator_permit")
            alpha_stake_vec = meta_get(meta, "alpha_stake")
            if alpha_stake_vec is None:
                alpha_stake_vec = meta_get(meta, "S")

            for s in subnet_stakes:
                uid = hotkey_to_uid.get(s.hotkey_ss58)
                if uid is None or meta_get_uid(meta, "E", uid) <= 0:
                    continue

                em = calculate_emission(
                    meta_get_uid(meta, "E", uid), meta.tempo,
                    em_tao_in, em_alpha_in, tao_price,
                )

                is_validator = bool(vp_vec[uid]) if vp_vec is not None else False

                if is_validator:
                    total_stake = (
                        float(alpha_stake_vec[uid])
                        if alpha_stake_vec is not None else 0.0
                    )
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

    subnets.sort(key=lambda s: s.balance_tao, reverse=True)
    total_balance_tao = free_tao + total_staked_tao

    return (
        PortfolioResponse(
            coldkey=coldkey,
            total_balance_tao=total_balance_tao,
            free_balance_tao=free_tao,
            total_staked_tao=total_staked_tao,
            tao_price_usd=tao_price,
            total_balance_usd=total_balance_tao * tao_price,
            subnet_count=len(subnets),
            subnets=subnets,
        ),
        current_block,
    )
