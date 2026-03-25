import asyncio

from fastapi import APIRouter, HTTPException, Query

from api.models.schemas import SubnetInfoResponse, SubnetNeuronSummary, SubnetNeuronsResponse
from api.services.calculations import calculate_emission
from api.services.chain_client import ChainClient
from api.services.price_client import PriceClient

router = APIRouter(tags=["subnet"])

_chain_client: ChainClient | None = None
_price_client: PriceClient | None = None


def init_subnet_router(chain_client: ChainClient, price_client: PriceClient):
    global _chain_client, _price_client
    _chain_client = chain_client
    _price_client = price_client


@router.get("/subnet/{netuid}/info", response_model=SubnetInfoResponse)
async def get_subnet_info(netuid: int):
    """Subnet hyperparameters, pool data, and basic stats."""
    try:
        meta, dyn = await asyncio.gather(
            _chain_client.get_metagraph(netuid),
            _chain_client.get_dynamic_info(netuid),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    return SubnetInfoResponse(
        netuid=netuid,
        name=getattr(dyn, 'subnet_name', '') or getattr(dyn, 'name', ''),
        symbol=getattr(dyn, 'symbol', ''),
        tempo=meta.tempo,
        block=meta.block,
        n=meta.n,
        max_n=getattr(meta, 'max_n', 0) or getattr(dyn, 'max_n', 0),
        emission_value=getattr(dyn, 'emission', 0.0) if hasattr(dyn, 'emission') else 0.0,
        tao_in=float(dyn.tao_in),
        alpha_in=float(dyn.alpha_in),
        price=float(dyn.price),
        total_stake=float(sum(meta.S)) if hasattr(meta, 'S') else 0.0,
    )


@router.get("/subnet/{netuid}/neurons", response_model=SubnetNeuronsResponse)
async def get_subnet_neurons(
    netuid: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=256),
):
    """Paginated list of all neurons in a subnet with key metrics."""
    try:
        meta = await _chain_client.get_metagraph(netuid)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    total = meta.n
    start = (page - 1) * per_page
    end = min(start + per_page, total)

    neurons = []
    for uid in range(start, end):
        axon = ""
        if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
            ax = meta.axons[uid]
            if hasattr(ax, 'ip') and hasattr(ax, 'port'):
                axon = f"{ax.ip}:{ax.port}"

        neurons.append(
            SubnetNeuronSummary(
                uid=uid,
                hotkey=meta.hotkeys[uid],
                coldkey=meta.coldkeys[uid],
                stake=float(meta.S[uid]),
                incentive=float(meta.I[uid]),
                consensus=float(meta.C[uid]),
                trust=float(meta.T[uid]),
                emission=float(meta.E[uid]),
                axon=axon,
            )
        )

    return SubnetNeuronsResponse(
        netuid=netuid,
        total=total,
        page=page,
        per_page=per_page,
        neurons=neurons,
    )


@router.get("/subnet/{netuid}/metagraph")
async def get_subnet_metagraph(
    netuid: int,
    refresh: bool = Query(False),
):
    """Full metagraph data for all neurons. Use ?refresh=true to bypass cache."""
    try:
        meta = await _chain_client.get_metagraph(netuid, force_refresh=refresh)
        dyn = await _chain_client.get_dynamic_info(netuid)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)
    rate = tao_in / alpha_in if alpha_in > 0 else 0.0

    neurons = []
    for uid in range(meta.n):
        axon = ""
        if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
            ax = meta.axons[uid]
            if hasattr(ax, 'ip') and hasattr(ax, 'port'):
                axon = f"{ax.ip}:{ax.port}"

        neurons.append({
            "uid": uid,
            "hotkey": meta.hotkeys[uid],
            "coldkey": meta.coldkeys[uid],
            "stake": float(meta.S[uid]),
            "incentive": float(meta.I[uid]),
            "consensus": float(meta.C[uid]),
            "trust": float(meta.T[uid]),
            "emission": float(meta.E[uid]),
            "dividends": float(meta.D[uid]) if hasattr(meta, 'D') else 0.0,
            "rank": float(meta.R[uid]) if hasattr(meta, 'R') else 0.0,
            "axon": axon,
            "active": bool(meta.active[uid]) if hasattr(meta, 'active') else True,
            "last_update": int(meta.last_update[uid]) if hasattr(meta, 'last_update') else 0,
            "validator_permit": bool(meta.validator_permit[uid]) if hasattr(meta, 'validator_permit') else False,
        })

    return {
        "netuid": netuid,
        "block": meta.block,
        "n": meta.n,
        "tempo": meta.tempo,
        "alpha_to_tao_rate": rate,
        "tao_in": tao_in,
        "alpha_in": alpha_in,
        "neurons": neurons,
    }


@router.get("/subnet/{netuid}/miners")
async def get_subnet_miners(
    netuid: int,
    sort: str = Query("uid", pattern="^(uid|incentive|stake|emission|daily_alpha|daily_tao|daily_usd)$"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(256, ge=1, le=512),
):
    """Miners table with computed daily emission. Filters to non-validator neurons."""
    try:
        meta, dyn, tao_price = await asyncio.gather(
            _chain_client.get_metagraph(netuid),
            _chain_client.get_dynamic_info(netuid),
            _price_client.get_tao_price(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)
    rate = tao_in / alpha_in if alpha_in > 0 else 1.0
    subnet_name = getattr(dyn, 'subnet_name', '') or getattr(dyn, 'name', '')
    symbol = getattr(dyn, 'symbol', '')

    miners = []
    for uid in range(meta.n):
        is_validator = (
            hasattr(meta, 'validator_permit')
            and bool(meta.validator_permit[uid])
        )
        if is_validator:
            continue

        e = float(meta.E[uid])
        incentive = float(meta.I[uid])
        stake = float(meta.alpha_stake[uid]) if hasattr(meta, 'alpha_stake') else float(meta.S[uid])

        if e > 0:
            em = calculate_emission(e, meta.tempo, tao_in, alpha_in, tao_price)
            daily_alpha = em.daily_alpha
            daily_tao = em.daily_tao
            daily_usd = em.daily_usd
        else:
            daily_alpha = 0.0
            daily_tao = 0.0
            daily_usd = 0.0

        axon = ""
        if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
            ax = meta.axons[uid]
            if hasattr(ax, 'ip') and hasattr(ax, 'port') and ax.ip != "0.0.0.0":
                axon = f"{ax.ip}:{ax.port}"

        miners.append({
            "uid": uid,
            "hotkey": meta.hotkeys[uid],
            "coldkey": meta.coldkeys[uid],
            "axon": axon,
            "incentive": incentive,
            "stake": stake,
            "stake_as_tao": stake * rate,
            "emission_alpha": e,
            "daily_alpha": daily_alpha,
            "daily_tao": daily_tao,
            "daily_usd": daily_usd,
            "trust": float(meta.T[uid]),
        })

    # Sort
    reverse = order == "desc"
    sort_key = sort if sort != "emission" else "emission_alpha"
    miners.sort(key=lambda m: m.get(sort_key, 0), reverse=reverse)

    # Assign rank by incentive (descending)
    by_incentive = sorted(miners, key=lambda m: m["incentive"], reverse=True)
    rank_map = {}
    for i, m in enumerate(by_incentive):
        rank_map[m["uid"]] = i + 1
    for m in miners:
        m["rank"] = rank_map[m["uid"]]

    total = len(miners)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    page_miners = miners[start:end]

    return {
        "netuid": netuid,
        "subnet_name": subnet_name,
        "symbol": symbol,
        "alpha_to_tao_rate": rate,
        "tao_price_usd": tao_price,
        "total_miners": total,
        "page": page,
        "per_page": per_page,
        "miners": page_miners,
    }


@router.get("/subnet/{netuid}/validators")
async def get_subnet_validators(
    netuid: int,
    sort: str = Query("uid", pattern="^(uid|stake|dividends|vtrust|dominance|emission|daily_alpha|daily_tao)$"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(256, ge=1, le=512),
):
    """Validators table with stake, dividends, and daily emission."""
    try:
        meta, dyn, tao_price = await asyncio.gather(
            _chain_client.get_metagraph(netuid),
            _chain_client.get_dynamic_info(netuid),
            _price_client.get_tao_price(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)
    rate = tao_in / alpha_in if alpha_in > 0 else 1.0
    subnet_name = getattr(dyn, 'subnet_name', '') or getattr(dyn, 'name', '')
    symbol = getattr(dyn, 'symbol', '')

    # Total alpha stake across all validators (for dominance %)
    total_alpha_all = 0.0

    validators = []
    for uid in range(meta.n):
        is_validator = (
            hasattr(meta, 'validator_permit')
            and bool(meta.validator_permit[uid])
        )
        if not is_validator:
            continue

        e = float(meta.E[uid])
        dividends = float(meta.D[uid]) if hasattr(meta, 'D') else 0.0
        vtrust = float(meta.Tv[uid]) if hasattr(meta, 'Tv') else 0.0
        stake = float(meta.alpha_stake[uid]) if hasattr(meta, 'alpha_stake') else 0.0
        tao_stake = float(meta.tao_stake[uid]) if hasattr(meta, 'tao_stake') else 0.0
        last_update = int(meta.last_update[uid]) if hasattr(meta, 'last_update') else 0

        total_alpha_all += stake

        if e > 0:
            em = calculate_emission(e, meta.tempo, tao_in, alpha_in, tao_price)
            daily_alpha = em.daily_alpha
            daily_tao = em.daily_tao
            daily_usd = em.daily_usd
        else:
            daily_alpha = 0.0
            daily_tao = 0.0
            daily_usd = 0.0

        axon = ""
        if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
            ax = meta.axons[uid]
            if hasattr(ax, 'ip') and hasattr(ax, 'port') and ax.ip != "0.0.0.0":
                axon = f"{ax.ip}:{ax.port}"

        total_stake_tao = tao_stake + (stake * rate)  # TAO stake + alpha converted to TAO

        validators.append({
            "uid": uid,
            "hotkey": meta.hotkeys[uid],
            "coldkey": meta.coldkeys[uid],
            "axon": axon,
            "dividends": dividends,
            "vtrust": vtrust,
            "stake": stake,
            "tao_stake": tao_stake,
            "total_stake_tao": total_stake_tao,
            "emission_alpha": e,
            "daily_alpha": daily_alpha,
            "daily_tao": daily_tao,
            "daily_usd": daily_usd,
            "last_update": int(meta.block - last_update) if last_update > 0 else 0,
            "trust": float(meta.T[uid]),
            "consensus": float(meta.C[uid]),
        })

    # Compute dominance %
    for v in validators:
        v["dominance"] = (v["stake"] / total_alpha_all * 100) if total_alpha_all > 0 else 0.0

    # Sort
    reverse = order == "desc"
    sort_key = sort if sort != "emission" else "emission_alpha"
    validators.sort(key=lambda v: v.get(sort_key, 0), reverse=reverse)

    # Assign rank by stake (descending)
    by_stake = sorted(validators, key=lambda v: v["stake"], reverse=True)
    rank_map = {}
    for i, v in enumerate(by_stake):
        rank_map[v["uid"]] = i + 1
    for v in validators:
        v["rank"] = rank_map[v["uid"]]

    total = len(validators)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    page_vals = validators[start:end]

    return {
        "netuid": netuid,
        "subnet_name": subnet_name,
        "symbol": symbol,
        "alpha_to_tao_rate": rate,
        "tao_price_usd": tao_price,
        "total_validators": total,
        "page": page,
        "per_page": per_page,
        "validators": page_vals,
    }


@router.get("/subnets")
async def get_all_subnets(
    sort: str = Query("market_cap", pattern="^(netuid|name|price|market_cap|emission|supply|volume)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """All subnets overview, ranked by market cap by default."""
    try:
        all_sn, tao_price = await asyncio.gather(
            _chain_client.get_all_subnets_info(),
            _price_client.get_tao_price(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    # Compute total pending emission across all subnets for emission share %
    total_pending = sum(
        float(sn.pending_alpha_emission) * float(sn.price)
        for sn in all_sn if sn.netuid != 0
    )

    subnets = []
    for sn in all_sn:
        price = float(sn.price)
        tao_in = float(sn.tao_in)
        alpha_in = float(sn.alpha_in)
        alpha_out = float(sn.alpha_out)

        # Market cap = tao_in for dynamic subnets (AMM pool TAO side)
        # For SN0, use tao_in directly
        market_cap_tao = tao_in
        market_cap_usd = market_cap_tao * tao_price

        # Circulating supply = alpha_out (tokens outside the pool)
        supply = alpha_out

        # Emission share: this subnet's pending emission as % of total
        pending = float(sn.pending_alpha_emission)
        if sn.netuid == 0:
            emission_pct = 0.0
        elif total_pending > 0:
            emission_pct = (pending * price) / total_pending * 100
        else:
            emission_pct = 0.0

        # Volume
        volume = float(sn.subnet_volume) if hasattr(sn, 'subnet_volume') else 0.0
        volume_tao = volume * price if sn.netuid != 0 else volume

        subnets.append({
            "netuid": sn.netuid,
            "name": sn.subnet_name or f"SN {sn.netuid}",
            "symbol": str(sn.symbol) if hasattr(sn, 'symbol') else "",
            "price": price,
            "price_usd": price * tao_price,
            "emission_pct": round(emission_pct, 2),
            "market_cap_tao": market_cap_tao,
            "market_cap_usd": market_cap_usd,
            "supply": supply,
            "supply_pct": 0,  # placeholder
            "volume_tao": volume_tao,
            "volume_usd": volume_tao * tao_price,
            "tempo": sn.tempo,
            "is_dynamic": sn.is_dynamic if hasattr(sn, 'is_dynamic') else True,
        })

    # Sort
    reverse = order == "desc"
    key_map = {
        "netuid": "netuid",
        "name": "name",
        "price": "price",
        "market_cap": "market_cap_tao",
        "emission": "emission_pct",
        "supply": "supply",
        "volume": "volume_tao",
    }
    sort_key = key_map.get(sort, "market_cap_tao")
    subnets.sort(
        key=lambda s: (s.get(sort_key, 0) if isinstance(s.get(sort_key, 0), (int, float)) else 0),
        reverse=reverse,
    )

    # Assign rank
    for i, s in enumerate(subnets):
        s["rank"] = i + 1

    # Summary
    sum_prices = sum(s["price"] for s in subnets)

    return {
        "total_subnets": len(subnets),
        "sum_subnet_prices": sum_prices,
        "sum_subnet_prices_usd": sum_prices * tao_price,
        "tao_price_usd": tao_price,
        "subnets": subnets,
    }
