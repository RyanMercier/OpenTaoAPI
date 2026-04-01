from __future__ import annotations

from pydantic import BaseModel


class ColdkeyInfo(BaseModel):
    ss58: str
    hex: str


class HotkeyInfo(BaseModel):
    """TaoStats-compatible hotkey detail within a miner response."""
    hotkey: ColdkeyInfo
    coldkey: ColdkeyInfo
    netuid: int
    uid: int
    immune: bool
    in_danger: bool
    deregistered: bool
    deregistration_timestamp: str | None = None
    alpha_balance: str
    alpha_balance_as_tao: str
    trust: str
    consensus: str
    incentive: str
    mech_incentive: list[str]
    emission: str
    total_emission: str
    total_emission_as_tao: str
    axon: str
    registration_block: int
    miner_rank: int | None = None
    validator_rank: int | None = None


class AlphaBalance(BaseModel):
    balance: str
    balance_as_tao: str
    hotkey: str
    coldkey: str
    netuid: int


class Pagination(BaseModel):
    current_page: int
    per_page: int
    total_items: int
    total_pages: int
    next_page: int | None = None
    prev_page: int | None = None


class MinerData(BaseModel):
    """TaoStats-compatible miner response body."""
    coldkey: ColdkeyInfo
    total_balance: str
    free_balance: str
    total_staked_balance_as_tao: str
    total_staked_mining_balance_as_tao: str
    total_staked_non_mining_balance_as_tao: str
    active_subnets: int
    total_active_hotkeys: int
    total_immune_hotkeys: int
    total_hotkeys_in_danger: int
    total_immune_hotkeys_during_period: int
    total_hotkeys_in_danger_during_period: int
    total_deregistered_hotkeys: int
    total_mining_emission_as_tao: str
    average_mining_emission_as_tao_per_hotkey: str
    hotkeys: list[HotkeyInfo]
    alpha_balances: list[AlphaBalance]


class MinerResponse(BaseModel):
    pagination: Pagination
    data: list[MinerData]


class PriceResponse(BaseModel):
    symbol: str = "TAO/USDT"
    price: float
    cached: bool = True


class EmissionResponse(BaseModel):
    netuid: int
    uid: int
    hotkey: str
    alpha_per_epoch: float
    alpha_per_block: float
    tao_per_block: float
    daily_alpha: float
    daily_tao: float
    daily_usd: float
    monthly_tao: float
    monthly_usd: float
    alpha_to_tao_rate: float
    tao_price_usd: float


class NeuronResponse(BaseModel):
    netuid: int
    uid: int
    hotkey: str
    coldkey: str
    stake: float
    stake_as_tao: float
    incentive: float
    consensus: float
    trust: float
    emission_per_epoch: float
    emission_per_epoch_as_tao: float
    daily_alpha: float
    daily_tao: float
    daily_usd: float
    axon: str
    active: bool
    last_update: int
    validator_permit: bool
    dividends: float
    rank: float


class SubnetInfoResponse(BaseModel):
    netuid: int
    name: str
    symbol: str
    tempo: int
    block: int
    n: int
    max_n: int
    emission_value: float
    tao_in: float
    alpha_in: float
    price: float
    total_stake: float


class SubnetNeuronSummary(BaseModel):
    uid: int
    hotkey: str
    coldkey: str
    stake: float
    incentive: float
    consensus: float
    trust: float
    emission: float
    axon: str


class SubnetNeuronsResponse(BaseModel):
    netuid: int
    total: int
    page: int
    per_page: int
    neurons: list[SubnetNeuronSummary]


class PortfolioSubnet(BaseModel):
    netuid: int
    name: str
    symbol: str
    balance_alpha: float
    balance_tao: float
    price_tao: float
    value_usd: float
    hotkey_count: int
    daily_yield_tao: float
    daily_yield_usd: float


class PortfolioResponse(BaseModel):
    coldkey: str
    total_balance_tao: float
    free_balance_tao: float
    total_staked_tao: float
    tao_price_usd: float
    total_balance_usd: float
    subnet_count: int
    subnets: list[PortfolioSubnet]


class PricePoint(BaseModel):
    block: int
    timestamp: str
    alpha_price_tao: float | None = None
    tao_price_usd: float | None = None


class SnapshotPoint(BaseModel):
    block: int
    timestamp: str
    netuid: int
    alpha_price_tao: float | None = None
    tao_price_usd: float | None = None
    tao_in: float | None = None
    alpha_in: float | None = None
    total_stake: float | None = None
    emission_rate: float | None = None
    validator_count: int | None = None
    neuron_count: int | None = None


class HistoryStatsResponse(BaseModel):
    netuid: int
    earliest_block: int | None = None
    latest_block: int | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    total_snapshots: int
