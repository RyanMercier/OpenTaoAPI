import asyncio

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
from api.services.price_client import PriceClient

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


@router.get("/miner/{coldkey}/{netuid}", response_model=MinerResponse)
async def get_miner(coldkey: str, netuid: int):
    """TaoStats-compatible miner endpoint. Returns coldkey balance, alpha balances across all subnets,
    and hotkey details (UID, stake, emission, incentive, mining rank) for the requested subnet."""
    try:
        # Fetch balance, all stakes, and target subnet data concurrently
        balance, stakes, meta, dyn = await asyncio.gather(
            _chain_client.get_balance(coldkey),
            _chain_client.get_stake_info_for_coldkey(coldkey),
            _chain_client.get_metagraph(netuid),
            _chain_client.get_dynamic_info(netuid),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chain query failed: {e}")

    # Fetch dynamic info for ALL subnets this coldkey has stakes in
    # so we can convert each alpha balance to TAO correctly
    all_netuids = set(s.netuid for s in stakes)
    dyn_by_netuid = {netuid: dyn}  # we already have the requested one

    other_netuids = [n for n in all_netuids if n != netuid]
    if other_netuids:
        try:
            other_dyns = await asyncio.gather(
                *[_chain_client.get_dynamic_info(n) for n in other_netuids]
            )
            for n, d in zip(other_netuids, other_dyns):
                dyn_by_netuid[n] = d
        except Exception:
            pass  # best-effort for other subnets

    tao_in = float(dyn.tao_in)
    alpha_in = float(dyn.alpha_in)

    # Build hotkey->uid lookup for the target subnet
    hotkey_to_uid = {}
    for uid in range(meta.n):
        hotkey_to_uid[meta.hotkeys[uid]] = uid

    # Compute total staked balance across ALL subnets (converted to TAO)
    # SN0 (root subnet) stakes are in TAO directly — rate is 1:1
    total_staked_tao_all_subnets = 0.0
    for stake_info in stakes:
        sn = stake_info.netuid
        stake_alpha = float(stake_info.stake)
        if sn == 0:
            # Root subnet: alpha IS TAO, no conversion needed
            total_staked_tao_all_subnets += stake_alpha
        elif sn in dyn_by_netuid:
            sn_dyn = dyn_by_netuid[sn]
            sn_tao_in = float(sn_dyn.tao_in)
            sn_alpha_in = float(sn_dyn.alpha_in)
            total_staked_tao_all_subnets += alpha_to_tao(
                stake_alpha, sn_tao_in, sn_alpha_in
            )
        else:
            # Fallback: use requested subnet's rate (imprecise)
            total_staked_tao_all_subnets += alpha_to_tao(
                stake_alpha, tao_in, alpha_in
            )

    # Filter stakes for the requested netuid
    subnet_stakes = [s for s in stakes if s.netuid == netuid]

    coldkey_hex = _ss58_to_hex(coldkey)
    coldkey_info = ColdkeyInfo(ss58=coldkey, hex=coldkey_hex)

    # Sort neurons by incentive to compute miner_rank
    incentive_list = []
    for uid in range(meta.n):
        incentive_list.append((uid, float(meta.I[uid])))
    incentive_list.sort(key=lambda x: x[1], reverse=True)
    uid_to_rank = {}
    for rank, (uid, _) in enumerate(incentive_list, start=1):
        uid_to_rank[uid] = rank

    hotkeys_data = []
    alpha_balances_data = []
    subnet_staked_tao = 0.0
    total_mining_staked_tao = 0.0
    total_mining_emission_tao = 0.0

    for stake_info in subnet_stakes:
        hk = stake_info.hotkey_ss58
        stake_alpha = float(stake_info.stake)
        stake_as_tao = alpha_to_tao(stake_alpha, tao_in, alpha_in)

        subnet_staked_tao += stake_as_tao

        alpha_balances_data.append(
            AlphaBalance(
                balance=to_rao_string(stake_alpha),
                balance_as_tao=to_rao_string(stake_as_tao),
                hotkey=hk,
                coldkey=coldkey,
                netuid=netuid,
            )
        )

        # Check if this hotkey is registered in the metagraph
        if hk in hotkey_to_uid:
            uid = hotkey_to_uid[hk]
            total_mining_staked_tao += stake_as_tao

            incentive = float(meta.I[uid])
            consensus = float(meta.C[uid])
            trust = float(meta.T[uid])
            emission_alpha = float(meta.E[uid])
            emission_tao = alpha_to_tao(emission_alpha, tao_in, alpha_in)
            total_mining_emission_tao += emission_tao

            # Axon info
            axon = ""
            if hasattr(meta, 'axons') and meta.axons and uid < len(meta.axons):
                ax = meta.axons[uid]
                if hasattr(ax, 'ip') and hasattr(ax, 'port'):
                    axon = f"{ax.ip}:{ax.port}"

            # Determine validator vs miner
            is_validator = False
            if hasattr(meta, 'validator_permit'):
                is_validator = bool(meta.validator_permit[uid])

            hotkeys_data.append(
                HotkeyInfo(
                    hotkey=ColdkeyInfo(ss58=hk, hex=_ss58_to_hex(hk)),
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
                    axon=axon,
                    registration_block=0,
                    miner_rank=uid_to_rank.get(uid) if not is_validator else None,
                    validator_rank=uid_to_rank.get(uid) if is_validator else None,
                )
            )

    avg_mining_emission = (
        total_mining_emission_tao / len(hotkeys_data) if hotkeys_data else 0.0
    )

    free_bal = float(balance.tao)
    total_bal = free_bal + total_staked_tao_all_subnets

    miner_data = MinerData(
        coldkey=coldkey_info,
        total_balance=to_rao_string(total_bal),
        free_balance=to_rao_string(free_bal),
        total_staked_balance_as_tao=to_rao_string(total_staked_tao_all_subnets),
        total_staked_mining_balance_as_tao=to_rao_string(total_mining_staked_tao),
        total_staked_non_mining_balance_as_tao=to_rao_string(
            total_staked_tao_all_subnets - total_mining_staked_tao
        ),
        active_subnets=len(all_netuids),
        total_active_hotkeys=len(hotkeys_data),
        total_immune_hotkeys=sum(1 for h in hotkeys_data if h.immune),
        total_hotkeys_in_danger=sum(1 for h in hotkeys_data if h.in_danger),
        total_immune_hotkeys_during_period=0,
        total_hotkeys_in_danger_during_period=0,
        total_deregistered_hotkeys=0,
        total_mining_emission_as_tao=to_rao_string(total_mining_emission_tao),
        average_mining_emission_as_tao_per_hotkey=to_rao_string(avg_mining_emission),
        hotkeys=hotkeys_data,
        alpha_balances=alpha_balances_data,
    )

    return MinerResponse(
        pagination=Pagination(
            current_page=1,
            per_page=1,
            total_items=1,
            total_pages=1,
            next_page=None,
            prev_page=None,
        ),
        data=[miner_data],
    )
