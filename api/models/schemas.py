from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

WebhookMetric = Literal["alpha_price_tao", "tao_in", "alpha_in", "market_cap_tao"]
WebhookDirection = Literal["above", "below", "cross_up", "cross_down"]


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


class PortfolioHistoryPoint(BaseModel):
    block: int
    timestamp: str
    total_balance_tao: float | None = None
    free_balance_tao: float | None = None
    total_staked_tao: float | None = None
    tao_price_usd: float | None = None
    total_balance_usd: float | None = None
    subnet_count: int | None = None


class PortfolioHistoryResponse(BaseModel):
    coldkey: str
    hours: int
    points: list[PortfolioHistoryPoint]


class TrackedWallet(BaseModel):
    id: int
    coldkey_ss58: str
    label: str | None = None
    created_at: str
    last_polled_at: str | None = None
    poll_interval_seconds: int
    active: bool


class TrackedWalletWithLatest(TrackedWallet):
    latest_block: int | None = None
    latest_timestamp: str | None = None
    total_balance_tao: float | None = None
    total_balance_usd: float | None = None
    total_staked_tao: float | None = None
    free_balance_tao: float | None = None
    subnet_count: int | None = None


class TrackWalletRequest(BaseModel):
    coldkey: str = Field(..., min_length=2, max_length=80, description="SS58 coldkey")
    label: str | None = Field(default=None, max_length=80)
    poll_interval_seconds: int = Field(default=300, ge=60, le=86400)


class PaperPortfolioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    initial_capital_tao: float = Field(default=100.0, gt=0, le=1_000_000)
    strategies: list[str] = Field(
        default_factory=lambda: ["stake_velocity", "mean_reversion", "momentum"],
        description="Registered strategy keys. drain_exit is always added by the runner."
    )
    poll_interval_seconds: int = Field(default=1800, ge=60, le=86400)
    max_positions: int = Field(default=10, ge=1, le=100)
    max_single_position_pct: float = Field(default=0.20, gt=0, le=1.0)
    reserve_pct: float = Field(default=0.20, ge=0.0, lt=1.0)
    max_position_pct_of_pool: float = Field(default=0.02, gt=0, le=0.5)
    max_slippage_pct: float = Field(default=0.03, gt=0, le=0.5)
    num_hotkeys: int = Field(default=1, ge=1, le=64)
    external_strategy_paths: list[str] = Field(default_factory=list)


class PaperPortfolio(BaseModel):
    id: int
    name: str
    initial_capital_tao: float
    active: bool
    created_at: str
    last_cycle_at: str | None = None
    free_tao: float | None = None
    peak_value: float | None = None
    strategies: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)


class PaperPosition(BaseModel):
    netuid: int
    entry_block: int
    entry_time: str
    entry_price: float
    alpha_amount: float
    tao_invested: float
    strategy: str
    hotkey_id: int


class PaperTrade(BaseModel):
    id: str
    timestamp: str
    block: int
    netuid: int
    direction: str
    strategy: str
    tao_amount: float
    alpha_amount: float
    spot_price: float
    effective_price: float
    slippage_pct: float
    signal_strength: float | None = None
    hotkey_id: int | None = None
    entry_price: float | None = None
    pnl_tao: float | None = None
    pnl_pct: float | None = None
    hold_duration_hours: float | None = None
    entry_strategy: str | None = None


class PaperValuePoint(BaseModel):
    timestamp: str
    free_tao: float
    total_value_tao: float
    total_pnl_tao: float
    drawdown_pct: float
    num_open_positions: int
    benchmark_value_tao: float | None = None  # pool-weighted buy-and-hold


class PaperValueHistory(BaseModel):
    portfolio_id: int
    hours: int
    points: list[PaperValuePoint]
    benchmark_universe: list[int] = []
    benchmark_anchor_timestamp: str | None = None


class StrategyDescriptor(BaseModel):
    name: str
    source: str
    doc: str = ""


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


class WebhookSubscribeRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="http(s) URL to POST on threshold crossings")
    metric: WebhookMetric = Field(..., description="Which metric to watch")
    threshold: float = Field(..., ge=-1e15, le=1e15)
    direction: WebhookDirection = Field(
        ..., description="above/below fire whenever the value is on that side after being on the other; cross_up/cross_down fire only on the crossing event"
    )
    netuid: int | None = Field(
        default=None, ge=0, le=65535, description="Scope to a single subnet; omit for all"
    )


class WebhookSubscribeResponse(BaseModel):
    id: int
    url: str
    metric: WebhookMetric
    threshold: float
    direction: WebhookDirection
    netuid: int | None
    created_at: str
    active: bool
    last_value: float | None = None
    last_fired_at: str | None = None
