from dataclasses import dataclass

BLOCKS_PER_DAY = 7200


@dataclass
class EmissionBreakdown:
    alpha_per_epoch: float
    alpha_per_block: float
    tao_per_block: float
    daily_alpha: float
    daily_tao: float
    daily_usd: float
    monthly_tao: float
    monthly_usd: float


def calculate_emission(
    meta_e_uid: float,
    tempo: int,
    tao_in: float,
    alpha_in: float,
    tao_price_usd: float,
) -> EmissionBreakdown:
    alpha_to_tao_rate = tao_in / alpha_in if alpha_in > 0 else 0.0

    alpha_per_epoch = meta_e_uid
    alpha_per_block = alpha_per_epoch / tempo if tempo > 0 else 0.0
    tao_per_block = alpha_per_block * alpha_to_tao_rate

    daily_alpha = alpha_per_block * BLOCKS_PER_DAY
    daily_tao = daily_alpha * alpha_to_tao_rate
    daily_usd = daily_tao * tao_price_usd
    monthly_tao = daily_tao * 30
    monthly_usd = daily_usd * 30

    return EmissionBreakdown(
        alpha_per_epoch=alpha_per_epoch,
        alpha_per_block=alpha_per_block,
        tao_per_block=tao_per_block,
        daily_alpha=daily_alpha,
        daily_tao=daily_tao,
        daily_usd=daily_usd,
        monthly_tao=monthly_tao,
        monthly_usd=monthly_usd,
    )


def alpha_to_tao(alpha_amount: float, tao_in: float, alpha_in: float) -> float:
    if alpha_in <= 0 or tao_in <= 0:
        return alpha_amount
    return alpha_amount * (tao_in / alpha_in)


def to_rao_string(value: float) -> str:
    return str(int(value * 1e9))


def balance_rao_string(balance) -> str:
    """Convert a bittensor Balance object to RAO string."""
    return str(int(balance.rao))
