"""Hotstuff fee calculator with tier-based maker/taker fees and rebate tiers.

Hotstuff has a complex fee structure:
  - Taker fees: 0.020% - 0.025% based on 14d rolling volume
  - Maker fees: negative (rebates) from -0.002% to -0.014%
  - Additional maker rebate tiers for high market makers (up to -0.013%)
  - Final maker fee = min(rebate_tier_rate, maker_fee_tier_rate)

Volume weighting:
  Total Volume = Perp Volume + 2 * Spot Volume + 2 * Stable-Spot Volume

Funding rates:
  - Charged hourly (24 times/day)
  - 8-hour rate shown by API, hourly = rate / 8
  - payment = position_size * oracle_price * funding_rate
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Fee tiers (taker, maker) keyed by 14d volume threshold in USD ──────────
FEE_TIERS: list[tuple[float, float, float]] = [
    # (volume_threshold_usd, taker_bps, maker_bps — negative = rebate)
    (0,        0.025, -0.0020),  # Standard: 0 - 1M
    (1_000_000,  0.024, -0.0025),  # VIP 1: > 1M
    (5_000_000,  0.023, -0.0030),  # VIP 2: > 5M
    (20_000_000, 0.022, -0.0035),  # VIP 3: > 20M
    (100_000_000, 0.021, -0.0050),  # VIP 4: > 100M
    (250_000_000, 0.020, -0.0070),  # VIP 5: > 250M
    (500_000_000, 0.020, -0.0140),  # VIP 6: > 500M
]

# Maker rebate tiers: (% of total maker volume, rebate_bps)
MAKER_REBATE_TIERS: list[tuple[float, float]] = [
    (0.025, -0.010),  # Tier 0: > 2.5% of maker volume
    (0.050, -0.011),  # Tier 1: > 5%
    (0.075, -0.012),  # Tier 2: > 7.5%
    (0.100, -0.013),  # Tier 3: > 10%
]


@dataclass
class FeeTier:
    """Resolved fee tier for a given volume level."""
    volume_usd: float
    taker_fee_bps: float      # e.g. 0.025 = 0.025%
    maker_fee_bps: float      # e.g. -0.002 = -0.002% (rebate)
    effective_maker_bps: float  # final after rebate tier consideration
    tier_name: str


@dataclass
class TradeFee:
    """Fee breakdown for a single trade."""
    notional: float
    is_maker: bool
    fee_bps: float
    fee_usd: float
    is_rebate: bool  # True if we receive money


@dataclass
class ArbFeeAnalysis:
    """Complete fee analysis for a round-trip arbitrage trade."""
    leg1: TradeFee
    leg2: TradeFee
    total_fees_usd: float
    total_fees_bps: float
    funding_8h: float          # 8-hour funding rate
    funding_hourly: float      # hourly funding rate
    funding_annualized_apr: float  # estimated annualized funding
    net_edge_bps: float        # spread_bps - total_fees_bps
    is_profitable: bool

    def summary(self) -> str:
        dir1 = "maker" if self.leg1.is_maker else "taker"
        dir2 = "maker" if self.leg2.is_maker else "taker"
        return (
            f"Fee Analysis: leg1={dir1} {self.leg1.fee_bps:+.4f}% (${self.leg1.fee_usd:.4f}) | "
            f"leg2={dir2} {self.leg2.fee_bps:+.4f}% (${self.leg2.fee_usd:.4f}) | "
            f"total=${self.total_fees_usd:.4f} ({self.total_fees_bps:.4f}bps) | "
            f"funding_8h={self.funding_8h:+.6f} | "
            f"edge={self.net_edge_bps:+.4f}bps | "
            f"{'PROFITABLE' if self.is_profitable else 'NOT PROFITABLE'}"
        )


def get_fee_tier(volume_14d_usd: float, maker_share_pct: float = 0.0) -> FeeTier:
    """Get the applicable fee tier for a given 14-day volume.

    Args:
        volume_14d_usd: 14-day rolling total volume in USD
        maker_share_pct: share of maker volume (0.0 to 1.0) for rebate tier
    """
    taker_bps = FEE_TIERS[0][1]
    maker_bps = FEE_TIERS[0][2]
    tier_name = "Standard"

    for threshold, taker, maker in FEE_TIERS:
        if volume_14d_usd >= threshold:
            taker_bps = taker
            maker_bps = maker
            tier_name = {
                0: "Standard", 1_000_000: "VIP 1", 5_000_000: "VIP 2",
                20_000_000: "VIP 3", 100_000_000: "VIP 4", 250_000_000: "VIP 5",
                500_000_000: "VIP 6",
            }.get(threshold, "Standard")

    # Apply maker rebate tier (min of rebate tier and maker fee tier — more negative wins)
    effective_maker = maker_bps
    for share_threshold, rebate_bps in MAKER_REBATE_TIERS:
        if maker_share_pct >= share_threshold:
            effective_maker = min(effective_maker, rebate_bps)

    return FeeTier(
        volume_usd=volume_14d_usd,
        taker_fee_bps=taker_bps,
        maker_fee_bps=maker_bps,
        effective_maker_bps=effective_maker,
        tier_name=tier_name,
    )


def trade_fee(notional: float, is_maker: bool, tier: FeeTier) -> TradeFee:
    """Calculate fee for a single trade."""
    bps = tier.effective_maker_bps if is_maker else tier.taker_fee_bps
    fee_usd = notional * bps / 10_000
    return TradeFee(
        notional=notional,
        is_maker=is_maker,
        fee_bps=bps,
        fee_usd=fee_usd,
        is_rebate=bps < 0,
    )


def analyze_arb(
    spread_bps: float,
    notional: float,
    funding_8h: float,
    volume_14d: float = 0,
    maker_share_pct: float = 0.0,
    leg1_is_maker: bool = True,
    leg2_is_maker: bool = True,
    holding_hours: float = 1.0,
) -> ArbFeeAnalysis:
    """Analyze whether an arbitrage opportunity is profitable after fees.

    Args:
        spread_bps: gross price divergence in basis points (e.g. 50 = 0.5%)
        notional: trade size in USD
        funding_8h: 8-hour funding rate from API (positive = longs pay shorts)
        volume_14d: your 14-day rolling volume for tier determination
        maker_share_pct: your share of maker volume for rebate tier
        leg1_is_maker: whether the Hotstuff leg is a maker order
        leg2_is_maker: whether the other venue leg is a maker order
        holding_hours: expected holding period for funding estimation
    """
    tier = get_fee_tier(volume_14d, maker_share_pct)

    # Leg on Hotstuff
    leg1 = trade_fee(notional, leg1_is_maker, tier)
    # Leg on other venue (estimate as taker since most CEXes charge more)
    # Using Hotstuff taker as conservative estimate — caller can override
    other_tier = FeeTier(0, 0.030, -0.005, -0.005, "Other Venue (est.)")
    leg2 = trade_fee(notional, leg2_is_maker, other_tier)

    total_fees = leg1.fee_usd + leg2.fee_usd
    if leg1.is_rebate:
        total_fees = leg2.fee_usd  # leg1 gives money back
    if leg2.is_rebate:
        total_fees = leg1.fee_usd

    total_bps_spent = total_fees / notional * 10_000 if notional > 0 else 0

    # Funding estimation
    hourly_fund = funding_8h / 8.0
    funding_payment = notional * hourly_fund * holding_hours
    # If we're short on Hotstuff and funding positive → we receive funding
    funding_apr = hourly_fund * 24 * 365 * 100  # as percentage

    # Net: spread earns money, fees cost money, funding may add/subtract
    # Assume we go short on Hotstuff (higher price) and long on other (lower price)
    # If funding positive, short receives funding → additional profit
    gross_edge_usd = notional * spread_bps / 10_000
    net_edge_usd = gross_edge_usd - abs(total_fees) + funding_payment
    net_edge_bps = net_edge_usd / notional * 10_000 if notional > 0 else 0

    return ArbFeeAnalysis(
        leg1=leg1,
        leg2=leg2,
        total_fees_usd=total_fees,
        total_fees_bps=total_bps_spent,
        funding_8h=funding_8h,
        funding_hourly=hourly_fund,
        funding_annualized_apr=funding_apr,
        net_edge_bps=net_edge_bps,
        is_profitable=net_edge_usd > 0,
    )
