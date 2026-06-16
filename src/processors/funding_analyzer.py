"""Funding rate analysis for Hotstuff perps.

Funding rates on Hotstuff are charged hourly (8h rate shown by API).
This module analyzes:
  1. Current funding rates (are they unusually high/low?)
  2. Funding rate history (trending up/down?)
  3. Expected funding capture for a given position size
  4. Funding rate divergences across venues

Key insight: Very high funding rates (e.g. X-PERP at 0.0125) mean
shorts collect massive payments from longs. This creates an opportunity:
short the perp on Hotstuff (collect funding) + long spot/perp on CEX (hedge).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.scrapers.hotstuff import HotstuffClient, HotstuffTicker


@dataclass
class FundingAnalysis:
    """Analysis of funding rate for a single instrument."""
    symbol: str
    funding_8h: float           # Current 8-hour rate
    funding_hourly: float       # Hourly rate (= 8h / 8)
    funding_annualized_pct: float  # As APY percentage
    premium_pct: float          # (mark - index) / index * 100

    # Capture estimates (per $10k notional)
    hourly_capture_per_10k: float   # USD/hour collected if short
    daily_capture_per_10k: float    # USD/day
    weekly_capture_per_10k: float   # USD/week
    monthly_capture_per_10k: float  # USD/month (30d)

    # Signals
    is_extreme: bool            # Is funding unusually high?
    direction: str              # "collect" (short perp) or "avoid"
    score: float = 0.0          # 0-100 opportunity score

    def summary(self) -> str:
        emoji = "💰" if self.is_extreme else "📊"
        return (
            f"{emoji} {self.symbol}: fund_8h={self.funding_8h:+.6f} "
            f"({self.funding_annualized_pct:+.1f}% APR) | "
            f"premium={self.premium_pct:+.4f}% | "
            f"capture/${10}k={self.hourly_capture_per_10k:.2f}/h "
            f"{self.daily_capture_per_10k:.2f}/d | "
            f"score={self.score:.0f}"
        )


class FundingAnalyzer:
    """Analyzes funding rates across Hotstuff perps."""

    # Thresholds
    HIGH_FUNDING_8H = 0.001       # 0.1% per 8h = ~109% APR
    EXTREME_FUNDING_8H = 0.005    # 0.5% per 8h = ~547% APR
    LOW_FUNDING_8H = -0.001       # Negative funding

    def __init__(self, hotstuff_client: "HotstuffClient"):
        self.hotstuff = hotstuff_client

    async def analyze_all(self, tickers: list["HotstuffTicker"]) -> list[FundingAnalysis]:
        """Analyze funding rates for all tickers."""
        analyses = []
        for t in tickers:
            analysis = self._analyze_single(t)
            analyses.append(analysis)

        # Sort by score (best opportunities first)
        analyses.sort(key=lambda a: a.score, reverse=True)
        return analyses

    def _analyze_single(self, ticker: "HotstuffTicker") -> FundingAnalysis:
        """Analyze funding for a single perp."""
        fund_8h = ticker.funding_rate
        fund_h = fund_8h / 8.0

        # Annualize: 3 payments/day * 365 days = 1095 periods
        fund_apr = fund_8h * 3 * 365 * 100  # as percentage

        # Capture per $10k (if short and funding positive)
        notional = 10_000
        hourly_cap = notional * fund_h
        daily_cap = hourly_cap * 24
        weekly_cap = daily_cap * 7
        monthly_cap = daily_cap * 30

        # Determine if extreme
        is_extreme = abs(fund_8h) >= self.HIGH_FUNDING_8H
        direction = "collect" if fund_8h > 0 else "pay"

        # Score
        score = 0.0
        abs_8h = abs(fund_8h)

        # Funding magnitude (up to 50 pts)
        if abs_8h >= self.EXTREME_FUNDING_8H:
            score += 50
        elif abs_8h >= self.HIGH_FUNDING_8H:
            score += 30 + (abs_8h - self.HIGH_FUNDING_8H) / (self.EXTREME_FUNDING_8H - self.HIGH_FUNDING_8H) * 20
        else:
            score += abs_8h / self.HIGH_FUNDING_8H * 30

        # Liquidity bonus (up to 30 pts)
        if ticker.volume_24h > 1_000_000:
            score += 20
        elif ticker.volume_24h > 100_000:
            score += 10

        if ticker.open_interest > 500:
            score += 10
        elif ticker.open_interest > 50:
            score += 5

        # Premium alignment (up to 20 pts)
        # If perp trades at premium AND funding is positive → stronger signal
        if ticker.premium_pct > 0 and fund_8h > 0:
            score += min(20, ticker.premium_pct * 50)
        elif ticker.premium_pct < 0 and fund_8h < 0:
            score += min(20, abs(ticker.premium_pct) * 50)

        return FundingAnalysis(
            symbol=ticker.symbol,
            funding_8h=fund_8h,
            funding_hourly=fund_h,
            funding_annualized_pct=fund_apr,
            premium_pct=ticker.premium_pct,
            hourly_capture_per_10k=hourly_cap,
            daily_capture_per_10k=daily_cap,
            weekly_capture_per_10k=weekly_cap,
            monthly_capture_per_10k=monthly_cap,
            is_extreme=is_extreme,
            direction=direction,
            score=score,
        )

    async def get_funding_history(
        self, symbol: str, hours: int = 24
    ) -> list[dict]:
        """Fetch recent hourly funding rate history via chart API.

        The chart API gives us mark price history; we can infer funding
        from mark-index spread changes. For exact funding, we'd need
        the account-level funding_history endpoint (requires wallet).
        """
        # Use chart to get hourly candles
        to_ts = int(time.time() * 1000)
        from_ts = to_ts - hours * 3600 * 1000

        mark_chart = await self.hotstuff.get_chart(symbol, "mark", "60", from_ts, to_ts)
        index_chart = await self.hotstuff.get_chart(symbol, "index", "60", from_ts, to_ts)

        history = []
        if mark_chart and index_chart:
            # Align by timestamp
            index_by_ts = {c["time"]: c for c in index_chart}
            for mc in mark_chart:
                ts = mc.get("time")
                if ts in index_by_ts:
                    ic = index_by_ts[ts]
                    mark_close = mc.get("close", 0)
                    idx_close = ic.get("close", 0)
                    premium = (mark_close - idx_close) / idx_close * 100 if idx_close > 0 else 0
                    history.append({
                        "timestamp": ts,
                        "mark": mark_close,
                        "index": idx_close,
                        "premium_pct": premium,
                        "volume": mc.get("volume", 0),
                    })

        return history
