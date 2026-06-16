"""Market price tracker — collects and analyzes sold listing data across platforms.

Builds a price history database that the pricing engine uses to:
1. Determine fair market value for any product
2. Detect price trends (rising/falling)
3. Calculate price volatility
4. Estimate time-to-sell
5. Identify seasonal patterns

Data sources:
- eBay Finding API (sold listings) — free, no auth needed
- eBay Browse API (active listings) — for competition analysis
- Mercari JP API (sold listings) — for source price validation
- Yahoo Auctions JP API — for additional source data
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import EbaySoldListing, PriceSnapshot, SourceListing


@dataclass
class PriceStatistics:
    """Statistical summary of price data."""

    count: int = 0
    mean: float = 0.0
    median: float = 0.0
    std_dev: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    percentile_25: float = 0.0
    percentile_75: float = 0.0

    # Trend
    trend: str = "stable"  # "rising", "falling", "stable"
    trend_pct: float = 0.0  # % change over period

    # Velocity
    avg_days_to_sell: float = 0.0
    sell_through_rate: float = 0.0

    @property
    def price_range(self) -> float:
        return self.max_price - self.min_price if self.max_price > self.min_price else 0

    @property
    def coefficient_of_variation(self) -> float:
        """Lower = more predictable prices."""
        return self.std_dev / self.mean if self.mean > 0 else 0


@dataclass
class MarketSnapshot:
    """Complete market snapshot for a product/query."""

    query: str
    platform: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Price stats
    sold_stats: PriceStatistics = field(default_factory=PriceStatistics)
    active_stats: PriceStatistics = field(default_factory=PriceStatistics)

    # Volume
    total_sold_30d: int = 0
    total_sold_90d: int = 0
    total_active: int = 0

    # Competition
    num_sellers: int = 0
    competition_level: str = "medium"

    # Signals
    avg_watchers: float = 0.0
    avg_seller_rating: float = 0.0

    # Recommendation
    suggested_price: float = 0.0
    price_confidence: float = 0.0  # 0-1 based on data quality


class MarketPriceTracker:
    """Tracks and analyzes market prices across platforms."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def get_price_stats(
        self,
        query: str,
        platform: str = "ebay",
        days: int = 90,
    ) -> PriceStatistics:
        """Get price statistics for a query from cached data."""
        async with self.session_factory() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            stmt = select(EbaySoldListing).where(
                and_(
                    EbaySoldListing.search_query == query,
                    EbaySoldListing.captured_at >= cutoff,
                )
            )
            result = await session.execute(stmt)
            listings = result.scalars().all()

            if not listings:
                return PriceStatistics()

            prices = [l.price for l in listings if l.price > 0]
            if not prices:
                return PriceStatistics()

            stats = self._calculate_stats(prices)

            # Calculate trend (compare recent 30d vs previous 30d)
            stats.trend, stats.trend_pct = self._calculate_trend(listings)

            return stats

    async def get_market_snapshot(
        self,
        query: str,
        platform: str = "ebay",
    ) -> MarketSnapshot:
        """Get complete market snapshot for a product."""
        snapshot = MarketSnapshot(query=query, platform=platform)

        async with self.session_factory() as session:
            # Sold listings (90 days)
            cutoff_90d = datetime.now(timezone.utc) - timedelta(days=90)
            stmt = select(EbaySoldListing).where(
                and_(
                    EbaySoldListing.search_query == query,
                    EbaySoldListing.captured_at >= cutoff_90d,
                )
            )
            result = await session.execute(stmt)
            sold_listings = result.scalars().all()

            if sold_listings:
                prices = [l.price for l in sold_listings if l.price > 0]
                snapshot.sold_stats = self._calculate_stats(prices)
                snapshot.sold_stats.trend, snapshot.sold_stats.trend_pct = (
                    self._calculate_trend(sold_listings)
                )
                snapshot.total_sold_90d = len(sold_listings)

                # 30-day count
                cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
                snapshot.total_sold_30d = sum(
                    1 for l in sold_listings if l.sold_at and l.sold_at >= cutoff_30d
                )

                # Unique sellers
                snapshot.num_sellers = len(set(l.external_id for l in sold_listings))

            # Active listings (competition)
            # This would come from a separate active listings table
            # For now, estimate from sold data
            snapshot.total_active = snapshot.total_sold_30d * 3  # rough estimate

            # Competition level
            if snapshot.total_active < 10:
                snapshot.competition_level = "low"
            elif snapshot.total_active < 50:
                snapshot.competition_level = "medium"
            else:
                snapshot.competition_level = "high"

            # Suggested price
            if snapshot.sold_stats.median > 0:
                snapshot.suggested_price = (
                    snapshot.sold_stats.percentile_75 or snapshot.sold_stats.median
                )
                # Confidence based on data volume
                if snapshot.total_sold_30d >= 20:
                    snapshot.price_confidence = 0.9
                elif snapshot.total_sold_30d >= 10:
                    snapshot.price_confidence = 0.7
                elif snapshot.total_sold_30d >= 5:
                    snapshot.price_confidence = 0.5
                else:
                    snapshot.price_confidence = 0.3

        return snapshot

    async def record_sold_listing(
        self,
        query: str,
        external_id: str,
        title: str,
        price: float,
        shipping_cost: float = 0,
        condition: str = "",
        sold_at: datetime | None = None,
        url: str = "",
    ) -> None:
        """Record a sold listing for future analysis."""
        async with self.session_factory() as session:
            # Check if already recorded
            stmt = select(EbaySoldListing).where(
                EbaySoldListing.external_id == external_id
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                return  # already tracked

            listing = EbaySoldListing(
                external_id=external_id,
                search_query=query,
                title=title,
                price=price,
                shipping_cost=shipping_cost,
                condition=condition,
                sold_at=sold_at or datetime.now(timezone.utc),
                url=url,
            )
            session.add(listing)

    async def record_source_price(
        self,
        source_listing_id: int,
        price_jpy: float,
        price_usd: float,
        is_available: bool = True,
    ) -> None:
        """Record a price snapshot for a source listing."""
        async with self.session_factory() as session:
            snapshot = PriceSnapshot(
                source_listing_id=source_listing_id,
                price_original=price_jpy,
                price_usd=price_usd,
                is_available=is_available,
            )
            session.add(snapshot)

    def _calculate_stats(self, prices: list[float]) -> PriceStatistics:
        """Calculate statistics from a list of prices."""
        if not prices:
            return PriceStatistics()

        sorted_prices = sorted(prices)
        n = len(sorted_prices)

        mean = statistics.mean(sorted_prices)
        median = statistics.median(sorted_prices)
        std_dev = statistics.stdev(sorted_prices) if n > 1 else 0

        p25_idx = max(0, int(n * 0.25) - 1)
        p75_idx = min(n - 1, int(n * 0.75))

        return PriceStatistics(
            count=n,
            mean=mean,
            median=median,
            std_dev=std_dev,
            min_price=sorted_prices[0],
            max_price=sorted_prices[-1],
            percentile_25=sorted_prices[p25_idx],
            percentile_75=sorted_prices[p75_idx],
        )

    def _calculate_trend(
        self, listings: list[EbaySoldListing]
    ) -> tuple[str, float]:
        """Calculate price trend from listings with dates."""
        # Split into recent (last 30d) and previous (30-60d)
        now = datetime.now(timezone.utc)
        cutoff_recent = now - timedelta(days=30)
        cutoff_prev = now - timedelta(days=60)

        recent_prices = [
            l.price for l in listings
            if l.sold_at and l.sold_at >= cutoff_recent and l.price > 0
        ]
        prev_prices = [
            l.price for l in listings
            if l.sold_at and cutoff_prev <= l.sold_at < cutoff_recent and l.price > 0
        ]

        if not recent_prices or not prev_prices:
            return "stable", 0.0

        recent_avg = statistics.mean(recent_prices)
        prev_avg = statistics.mean(prev_prices)

        if prev_avg == 0:
            return "stable", 0.0

        change_pct = ((recent_avg - prev_avg) / prev_avg) * 100

        if change_pct > 10:
            return "rising", change_pct
        elif change_pct < -10:
            return "falling", change_pct
        else:
            return "stable", change_pct
