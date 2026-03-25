from pydantic import BaseModel
from typing import Optional


# --- TaoStats-compatible miner endpoint models ---

class ColdkeyInfo(BaseModel):
    ss58: str
    hex: str


class HotkeyInfo(BaseModel):
    hotkey: ColdkeyInfo
    coldkey: ColdkeyInfo
    netuid: int
    uid: int
    immune: bool
    in_danger: bool
    deregistered: bool
    deregistration_timestamp: Optional[str] = None
    alpha_balance: str  # RAO string
    alpha_balance_as_tao: str  # RAO string
    trust: str  # float as string
    consensus: str  # float as string
    incentive: str  # float as string
    mech_incentive: list[str]
    emission: str  # alpha per epoch, RAO string
    total_emission: str  # cumulative alpha, RAO string
    total_emission_as_tao: str  # cumulative tao equivalent, RAO string
    axon: str  # ip:port
    registration_block: int
    miner_rank: Optional[int] = None
    validator_rank: Optional[int] = None


class AlphaBalance(BaseModel):
    balance: str  # RAO string (alpha)
    balance_as_tao: str  # RAO string (tao equivalent)
    hotkey: str  # ss58
    coldkey: str  # ss58
    netuid: int


class Pagination(BaseModel):
    current_page: int
    per_page: int
    total_items: int
    total_pages: int
    next_page: Optional[int] = None
    prev_page: Optional[int] = None


class MinerData(BaseModel):
    coldkey: ColdkeyInfo
    total_balance: str  # RAO string
    free_balance: str  # RAO string
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


# --- Price endpoint ---

class PriceResponse(BaseModel):
    symbol: str = "TAO/USDT"
    price: float
    cached: bool = True


# --- Emission endpoint ---

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


# --- Neuron endpoint ---

class NeuronResponse(BaseModel):
    netuid: int
    uid: int
    hotkey: str
    coldkey: str
    stake: float  # alpha
    stake_as_tao: float
    incentive: float
    consensus: float
    trust: float
    emission_per_epoch: float  # alpha
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


# --- Subnet endpoints ---

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
    price: float  # alpha to tao rate
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


# --- Portfolio endpoint ---

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
