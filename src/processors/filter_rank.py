"""Composite filtering and ranking system.

This is the final decision layer that takes all the data from:
- Product identification (brand, model, rarity)
- Pricing engine (profit, margin, ROI)
- Market tracker (trends, velocity, competition)
- Logistics (shipping, stockout risk)

And produces a ranked list of items to list, with clear go/no-go decisions.

The key insight: We're not just ranking by profit. We're ranking by
CAPITAL EFFICIENCY — how much profit we generate per dollar deployed per day.

A $30 profit item that sells in 3 days on $20 capital = $0.50/day/$
A $200 profit item that sells in 30 days on $150 capital = $0.044/day/$

The first item is 11x more capital efficient. We should list 50 of those
instead of 1 of the second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config.settings import settings
from src.processors.pricing import ProfitAnalysis, MarketPriceData, CATEGORY_PRICING
from src.processors.product_id import ProductIdentity, CATEGORY_TAXONOMY


@dataclass
class RankedListing:
    """A listing candidate with full analysis and ranking."""

    # Identity
    product: ProductIdentity | None = None
    source_title: str = ""
    source_url: str = ""
    source_price_jpy: float = 0.0

    # Analysis
    profit: ProfitAnalysis | None = None
    market: MarketPriceData | None = None

    # Scores (0-100 each)
    profit_score: float = 0.0
    velocity_score: float = 0.0
    risk_score: float = 0.0
    capital_efficiency_score: float = 0.0
    rarity_score: float = 0.0
    competition_score: float = 0.0
    data_quality_score: float = 0.0

    # Composite
    composite_score: float = 0.0

    # Decision
    should_list: bool = False
    priority: str = "skip"  # "high", "medium", "low", "skip"
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class CompositeFilterRank:
    """Filters and ranks listing candidates using composite scoring."""

    def __init__(
        self,
        min_composite_score: float = 40.0,
        max_active_listings: int = 100,
        max_capital_deployed: float = 5000.0,
        diversification_target: int = 5,  # min categories to spread across
    ):
        self.min_composite_score = min_composite_score
        self.max_active_listings = max_active_listings
        self.max_capital_deployed = max_capital_deployed
        self.diversification_target = diversification_target

    def rank(
        self,
        candidates: list[tuple[ProductIdentity, ProfitAnalysis, MarketPriceData]],
        current_portfolio: dict[str, int] | None = None,
    ) -> list[RankedListing]:
        """Rank a list of candidates and return sorted, filtered results.

        Args:
            candidates: List of (product_identity, profit_analysis, market_data) tuples
            current_portfolio: Current active listings by category {category: count}

        Returns:
            Sorted list of RankedListing, best first
        """
        if current_portfolio is None:
            current_portfolio = {}

        ranked: list[RankedListing] = []

        for product, profit, market in candidates:
            listing = self._evaluate(product, profit, market, current_portfolio)
            ranked.append(listing)

        # Sort by composite score (descending)
        ranked.sort(key=lambda r: r.composite_score, reverse=True)

        # Apply portfolio-level filters
        ranked = self._apply_portfolio_filters(ranked, current_portfolio)

        return ranked

    def _evaluate(
        self,
        product: ProductIdentity,
        profit: ProfitAnalysis,
        market: MarketPriceData,
        portfolio: dict[str, int],
    ) -> RankedListing:
        """Evaluate a single candidate."""
        listing = RankedListing(
            product=product,
            source_price_jpy=profit.source_cost / 0.0067 if profit.source_cost > 0 else 0,
            profit=profit,
            market=market,
        )

        # Individual scores
        listing.profit_score = profit.profit_score
        listing.velocity_score = profit.velocity_score
        listing.risk_score = profit.risk_score
        listing.capital_efficiency_score = profit.capital_efficiency_score
        listing.rarity_score = product.rarity_score
        listing.competition_score = self._score_competition(market)
        listing.data_quality_score = self._score_data_quality(market)

        # Warnings (non-fatal issues)
        listing.warnings = self._generate_warnings(product, profit, market, portfolio)

        # Composite score with category-specific weighting
        weights = self._get_category_weights(product.category)
        listing.composite_score = (
            listing.profit_score * weights["profit"]
            + listing.velocity_score * weights["velocity"]
            + listing.risk_score * weights["risk"]
            + listing.capital_efficiency_score * weights["capital_efficiency"]
            + listing.rarity_score * weights["rarity"]
            + listing.competition_score * weights["competition"]
            + listing.data_quality_score * weights["data_quality"]
        )

        # Decision
        listing.should_list = self._should_list(listing, profit)
        listing.priority = self._get_priority(listing)
        listing.reasons = profit.rejection_reasons

        return listing

    def _get_category_weights(self, category: str) -> dict[str, float]:
        """Get scoring weights based on category dynamics.

        Different categories value different things:
        - Trading cards: velocity matters most (fast turnover)
        - Vintage cameras: profit matters most (high value, patient buyers)
        - Streetwear: capital efficiency matters most (trends change fast)
        """
        weights = {
            "default": {
                "profit": 0.25,
                "velocity": 0.20,
                "risk": 0.15,
                "capital_efficiency": 0.20,
                "rarity": 0.10,
                "competition": 0.05,
                "data_quality": 0.05,
            },
            "trading_cards": {
                "profit": 0.20,
                "velocity": 0.30,  # fast turnover is key
                "risk": 0.15,
                "capital_efficiency": 0.20,
                "rarity": 0.10,
                "competition": 0.03,
                "data_quality": 0.02,
            },
            "streetwear": {
                "profit": 0.20,
                "velocity": 0.25,  # trends move fast
                "risk": 0.15,
                "capital_efficiency": 0.25,  # don't tie up capital
                "rarity": 0.10,
                "competition": 0.03,
                "data_quality": 0.02,
            },
            "vintage_camera": {
                "profit": 0.35,  # high value items, profit matters most
                "velocity": 0.10,  # patient buyers
                "risk": 0.15,
                "capital_efficiency": 0.15,
                "rarity": 0.15,
                "competition": 0.05,
                "data_quality": 0.05,
            },
            "anime_figure": {
                "profit": 0.25,
                "velocity": 0.20,
                "risk": 0.15,
                "capital_efficiency": 0.20,
                "rarity": 0.12,
                "competition": 0.05,
                "data_quality": 0.03,
            },
            "retro_games": {
                "profit": 0.25,
                "velocity": 0.25,
                "risk": 0.10,
                "capital_efficiency": 0.20,
                "rarity": 0.12,
                "competition": 0.05,
                "data_quality": 0.03,
            },
        }
        return weights.get(category, weights["default"])

    def _score_competition(self, market: MarketPriceData) -> float:
        """Score competition level 0-100 (higher = less competition = better)."""
        if market.competition_level == "low":
            return 90
        elif market.competition_level == "medium":
            return 60
        else:
            return 30

    def _score_data_quality(self, market: MarketPriceData) -> float:
        """Score data quality 0-100 based on how much market data we have."""
        if market.num_sold_listings >= 50:
            return 100
        elif market.num_sold_listings >= 20:
            return 80
        elif market.num_sold_listings >= 10:
            return 60
        elif market.num_sold_listings >= 5:
            return 40
        elif market.num_sold_listings >= 3:
            return 25
        else:
            return 10

    def _generate_warnings(
        self,
        product: ProductIdentity,
        profit: ProfitAnalysis,
        market: MarketPriceData,
        portfolio: dict[str, int],
    ) -> list[str]:
        """Generate non-fatal warnings about a listing."""
        warnings = []

        # Portfolio concentration
        cat_count = portfolio.get(product.category, 0)
        if cat_count > 10:
            warnings.append(
                f"Portfolio heavy in {product.category} ({cat_count} active listings)"
            )

        # Price trend
        if market.price_trend == "falling" and market.price_trend_pct < -15:
            warnings.append(
                f"Prices falling {market.price_trend_pct:.0f}% — consider waiting"
            )

        # High stockout risk
        if profit.stockout_probability > 0.10:
            warnings.append(
                f"Stockout risk {profit.stockout_probability:.0%} — consider buying source item first"
            )

        # Counterfeit risk
        cat_pricing = CATEGORY_PRICING.get(product.category, {})
        if cat_pricing.get("counterfeit_risk") == "high" and not product.brand:
            warnings.append("High counterfeit risk category + no brand identified — verify authenticity")

        # Low data confidence
        if market.num_sold_listings < 5:
            warnings.append(
                f"Only {market.num_sold_listings} sold listings — price estimate uncertain"
            )

        # High capital
        if profit.capital_deployed > 200:
            warnings.append(
                f"High capital at risk: ${profit.capital_deployed:.0f}"
            )

        # Slow mover
        if profit.expected_days_to_sell > 21:
            warnings.append(
                f"Slow mover: {profit.expected_days_to_sell:.0f} days to sell"
            )

        return warnings

    def _should_list(self, listing: RankedListing, profit: ProfitAnalysis) -> bool:
        """Final go/no-go decision."""
        # Must be viable from profit analysis
        if not profit.is_viable:
            return False

        # Must meet minimum composite score
        if listing.composite_score < self.min_composite_score:
            return False

        # Must have minimum data quality
        if listing.data_quality_score < 20:
            return False

        # Must not have critical warnings
        critical_warnings = [
            w for w in listing.warnings
            if "counterfeit" in w.lower() or "verify authenticity" in w.lower()
        ]
        if critical_warnings and not listing.product.brand:
            return False

        return True

    def _get_priority(self, listing: RankedListing) -> str:
        """Get listing priority."""
        if not listing.should_list:
            return "skip"
        if listing.composite_score >= 75:
            return "high"
        elif listing.composite_score >= 55:
            return "medium"
        else:
            return "low"

    def _apply_portfolio_filters(
        self,
        ranked: list[RankedListing],
        portfolio: dict[str, int],
    ) -> list[RankedListing]:
        """Apply portfolio-level constraints.

        - Don't let any single category dominate
        - Respect max active listings
        - Respect max capital deployed
        """
        result: list[RankedListing] = []
        category_counts: dict[str, int] = dict(portfolio)
        total_capital = 0.0
        total_listings = sum(portfolio.values())

        for listing in ranked:
            if not listing.should_list:
                continue

            cat = listing.product.category if listing.product else "unknown"
            cat_count = category_counts.get(cat, 0)

            # Max 30% of listings in one category
            max_per_category = max(5, int(self.max_active_listings * 0.3))
            if cat_count >= max_per_category:
                listing.warnings.append(
                    f"Category {cat} at capacity ({cat_count}/{max_per_category})"
                )
                listing.should_list = False
                listing.priority = "skip"
                continue

            # Max total listings
            if total_listings >= self.max_active_listings:
                listing.warnings.append("Max active listings reached")
                listing.should_list = False
                listing.priority = "skip"
                continue

            # Max capital
            capital = listing.profit.capital_deployed if listing.profit else 0
            if total_capital + capital > self.max_capital_deployed:
                listing.warnings.append(
                    f"Would exceed capital limit (${total_capital + capital:.0f}/${self.max_capital_deployed:.0f})"
                )
                listing.should_list = False
                listing.priority = "skip"
                continue

            result.append(listing)
            category_counts[cat] = cat_count + 1
            total_capital += capital
            total_listings += 1

        return result

    def get_portfolio_summary(
        self, ranked: list[RankedListing]
    ) -> dict[str, Any]:
        """Get summary of the current portfolio state."""
        listed = [r for r in ranked if r.should_list]

        by_category: dict[str, int] = {}
        total_profit = 0.0
        total_capital = 0.0

        for r in listed:
            cat = r.product.category if r.product else "unknown"
            by_category[cat] = by_category.get(cat, 0) + 1
            if r.profit:
                total_profit += r.profit.expected_profit_adjusted
                total_capital += r.profit.capital_deployed

        return {
            "total_candidates": len(ranked),
            "total_to_list": len(listed),
            "high_priority": sum(1 for r in listed if r.priority == "high"),
            "medium_priority": sum(1 for r in listed if r.priority == "medium"),
            "low_priority": sum(1 for r in listed if r.priority == "low"),
            "by_category": by_category,
            "total_expected_profit": total_profit,
            "total_capital_deployed": total_capital,
            "avg_composite_score": (
                sum(r.composite_score for r in listed) / len(listed) if listed else 0
            ),
        }
