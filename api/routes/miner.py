import asyncio
import logging

from fastapi import APIRouter, HTTPException

from api.models.schemas import (
    AlphaBalance,
    ColdkeyInfo,
    HotkeyInfo,
    MinerData,
    MinerResponse,
    Pagination,
)
from api.services.calculations import alpha_to_tao, to_rao_string
from api.services.chain_client import ChainClient
from api.services.metagraph_compat import meta_get, meta_get_uid
from api.services.price_client import PriceClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["miner"])

_chain_client: ChainClient | None = None
_price_client: PriceClient | None = None


def init_miner_router(chain_client: ChainClient, price_client: PriceClient):
    global _chain_client, _price_client
    _chain_client = chain_client
    _price_client = price_client


def _ss58_to_hex(ss58: str) -> str:
    try:
        from scalecodec.utils.ss58 import ss58_decode
        return "0x" + ss58_decode(ss58)
    except Exception:
        return ""


def _get_axon(meta, uid: int) -> str:
    if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
        ax = meta.axons[uid]
        if hasattr(ax, 'ip') and hasattr(ax, 'port') and ax.ip != "0.0.0.0":
            return f"{ax.ip}:{ax.port}"
    return ""


@router.get("/miner/{coldkey}/{netuid}", response_model=MinerResponse)
async def get_miner(coldkey: str, netuid: int):
    """TaoStats-compatible miner endpoint.

    Returns coldkey balance, alpha balances across all subnets, and hotkey
    details for the requested subnet.
    """
    try:
        balance, stakes, meta, dyn = await asyncio.gather(
            _chain_client.get_balance(coldkey),
            _chain_client.get_stake_info_for_coldkey(coldkey),
            _chain_client.get_metagraph(netuid),
            _chain_client.get_dynamic_info(netuid),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)

    # Collect all netuids and fetch their dynamic info for cross-subnet totals
    all_netuids = {s.netuid for s in stakes}
    dyn_by_netuid = {netuid: dyn}

    other_netuids = [n for n in all_netuids if n != netuid]
    if other_netuids:
        try:
            other_dyns = await asyncio.gather(
                *[_chain_client.get_dynamic_info(n) for n in other_netuids]
            )
            for n, d in zip(other_netuids, other_dyns):
                dyn_by_netuid[n] = d
        except Exception:
            pass

    # Compute total staked TAO across all subnets
    total_staked_tao = 0.0
    for s in stakes:
        stake_alpha = float(s.stake)
        if s.netuid == 0:
            total_staked_tao += stake_alpha
        elif s.netuid in dyn_by_netuid:
            sn_dyn = dyn_by_netuid[s.netuid]
            total_staked_tao += alpha_to_tao(
                stake_alpha, float(sn_dyn.tao_in), float(sn_dyn.alpha_in)
            )
        else:
            total_staked_tao += alpha_to_tao(stake_alpha, tao_in, alpha_in)

    # Build hotkey -> UID lookup for the target subnet
    hotkey_to_uid = {meta.hotkeys[uid]: uid for uid in range(meta.n)}

    # Rank neurons by incentive
    incentive_list = sorted(
        [(uid, meta_get_uid(meta, "I", uid)) for uid in range(meta.n)],
        key=lambda x: x[1],
        reverse=True,
    )
    uid_to_rank = {uid: rank for rank, (uid, _) in enumerate(incentive_list, 1)}

    # Process stakes on the target subnet
    subnet_stakes = [s for s in stakes if s.netuid == netuid]
    coldkey_hex = _ss58_to_hex(coldkey)
    coldkey_info = ColdkeyInfo(ss58=coldkey, hex=coldkey_hex)

    hotkeys_data = []
    alpha_balances_data = []
    mining_staked_tao = 0.0
    mining_emission_tao = 0.0

    for s in subnet_stakes:
        hotkey = s.hotkey_ss58
        stake_alpha = float(s.stake)
        stake_as_tao = alpha_to_tao(stake_alpha, tao_in, alpha_in)

        alpha_balances_data.append(AlphaBalance(
            balance=to_rao_string(stake_alpha),
            balance_as_tao=to_rao_string(stake_as_tao),
            hotkey=hotkey,
            coldkey=coldkey,
            netuid=netuid,
        ))

        if hotkey not in hotkey_to_uid:
            continue

        uid = hotkey_to_uid[hotkey]
        mining_staked_tao += stake_as_tao

        incentive = meta_get_uid(meta, "I", uid)
        consensus = meta_get_uid(meta, "C", uid)
        trust = meta_get_uid(meta, "T", uid)
        emission_alpha = meta_get_uid(meta, "E", uid)
        emission_tao = alpha_to_tao(emission_alpha, tao_in, alpha_in)
        mining_emission_tao += emission_tao

        vp = meta_get(meta, "validator_permit")
        is_validator = bool(vp[uid]) if vp is not None else False

        hotkeys_data.append(HotkeyInfo(
            hotkey=ColdkeyInfo(ss58=hotkey, hex=_ss58_to_hex(hotkey)),
            coldkey=coldkey_info,
            netuid=netuid,
            uid=uid,
            immune=False,
            in_danger=False,
            deregistered=False,
            deregistration_timestamp=None,
            alpha_balance=to_rao_string(stake_alpha),
            alpha_balance_as_tao=to_rao_string(stake_as_tao),
            trust=str(trust),
            consensus=str(consensus),
            incentive=str(incentive),
            mech_incentive=[str(incentive)],
            emission=to_rao_string(emission_alpha),
            total_emission=to_rao_string(emission_alpha),
            total_emission_as_tao=to_rao_string(emission_tao),
            axon=_get_axon(meta, uid),
            registration_block=0,
            miner_rank=uid_to_rank.get(uid) if not is_validator else None,
            validator_rank=uid_to_rank.get(uid) if is_validator else None,
        ))

    avg_emission = mining_emission_tao / len(hotkeys_data) if hotkeys_data else 0.0
    free_bal = float(balance.tao)

    return MinerResponse(
        pagination=Pagination(
            current_page=1, per_page=1, total_items=1, total_pages=1,
        ),
        data=[MinerData(
            coldkey=coldkey_info,
            total_balance=to_rao_string(free_bal + total_staked_tao),
            free_balance=to_rao_string(free_bal),
            total_staked_balance_as_tao=to_rao_string(total_staked_tao),
            total_staked_mining_balance_as_tao=to_rao_string(mining_staked_tao),
            total_staked_non_mining_balance_as_tao=to_rao_string(
                total_staked_tao - mining_staked_tao
            ),
            active_subnets=len(all_netuids),
            total_active_hotkeys=len(hotkeys_data),
            total_immune_hotkeys=0,
            total_hotkeys_in_danger=0,
            total_immune_hotkeys_during_period=0,
            total_hotkeys_in_danger_during_period=0,
            total_deregistered_hotkeys=0,
            total_mining_emission_as_tao=to_rao_string(mining_emission_tao),
            average_mining_emission_as_tao_per_hotkey=to_rao_string(avg_emission),
            hotkeys=hotkeys_data,
            alpha_balances=alpha_balances_data,
        )],
    )
