"""Candidate scoring engine — evaluates whether a source item is worth listing.

Takes a scraped item + eBay price analysis and computes:
- Expected margin after all fees
- Demand score (how many sold listings)
- Risk score (seller reliability, category risk, price volatility)
- Overall score for ranking candidates
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config.settings import settings
from src.scrapers.base import ScrapedItem
from src.scrapers.ebay_finding import EbayPriceAnalysis


# JPY to USD conversion (approximate — should use live rate in production)
JPY_TO_USD = 0.0067  # 1 JPY ≈ 0.0067 USD (update regularly)

# eBay fee constants
EBAY_FINAL_VALUE_FEE = 0.13  # 13% for most categories
EBAY_PAYMENT_FEE = 0.029  # 2.9%
EBAY_PAYMENT_FIXED = 0.30  # $0.30 per transaction


@dataclass
class CandidateScore:
    """Scoring result for a potential arbitrage item."""

    # Inputs
    source_item: ScrapedItem
    ebay_analysis: EbayPriceAnalysis

    # Computed values
    source_price_usd: float = 0.0
    estimated_sale_price: float = 0.0
    estimated_fees: float = 0.0
    estimated_shipping: float = 0.0
    estimated_proxy_fee: float = 0.0
    estimated_total_cost: float = 0.0
    estimated_profit: float = 0.0
    estimated_margin_percent: float = 0.0

    # Scores (0-100)
    margin_score: float = 0.0
    demand_score: float = 0.0
    risk_score: float = 0.0
    overall_score: float = 0.0

    # Decision
    is_viable: bool = False
    rejection_reasons: list[str] = None  # type: ignore

    def __post_init__(self):
        if self.rejection_reasons is None:
            self.rejection_reasons = []


class CandidateScorer:
    """Scores scraped items for arbitrage viability."""

    def __init__(
        self,
        min_margin_percent: float = 0.0,
        max_source_price_usd: float = 0.0,
        default_shipping: float = 0.0,
        proxy_fee: float = 0.0,
        jpy_to_usd: float = JPY_TO_USD,
    ):
        self.min_margin_percent = min_margin_percent or settings.min_margin_percent
        self.max_source_price_usd = max_source_price_usd or settings.max_source_price_usd
        self.default_shipping = default_shipping or settings.default_shipping_cost
        self.proxy_fee = proxy_fee or settings.proxy_service_fee
        self.jpy_to_usd = jpy_to_usd

    def score(
        self,
        item: ScrapedItem,
        ebay_analysis: EbayPriceAnalysis,
    ) -> CandidateScore:
        """Score a single item against eBay market data."""
        result = CandidateScore(source_item=item, ebay_analysis=ebay_analysis)

        # Convert source price to USD
        if item.currency == "JPY":
            result.source_price_usd = item.price * self.jpy_to_usd
        else:
            result.source_price_usd = item.price

        # Estimated sale price (use median of sold listings, or avg if no median)
        result.estimated_sale_price = ebay_analysis.median_price or ebay_analysis.avg_price

        # Cost breakdown
        result.estimated_proxy_fee = self.proxy_fee
        result.estimated_shipping = self.default_shipping

        # eBay fees on the sale price
        ebay_fees = (
            result.estimated_sale_price * (EBAY_FINAL_VALUE_FEE + EBAY_PAYMENT_FEE)
            + EBAY_PAYMENT_FIXED
        )
        result.estimated_fees = ebay_fees

        # Total cost
        result.estimated_total_cost = (
            result.source_price_usd
            + result.estimated_proxy_fee
            + result.estimated_shipping
        )

        # Profit
        result.estimated_profit = result.estimated_sale_price - result.estimated_total_cost - result.estimated_fees

        # Margin %
        if result.estimated_sale_price > 0:
            result.estimated_margin_percent = (
                result.estimated_profit / result.estimated_sale_price
            ) * 100

        # --- Scoring ---

        # Margin score (0-100): higher margin = better
        result.margin_score = min(100, max(0, result.estimated_margin_percent * 0.5))

        # Demand score (0-100): based on number of sold listings
        sold_count = ebay_analysis.total_sold
        if sold_count >= 100:
            result.demand_score = 100
        elif sold_count >= 50:
            result.demand_score = 80
        elif sold_count >= 20:
            result.demand_score = 60
        elif sold_count >= 10:
            result.demand_score = 40
        elif sold_count >= 3:
            result.demand_score = 20
        else:
            result.demand_score = 5

        # Risk score (0-100): lower risk = higher score
        risk = 50.0  # baseline

        # Seller rating bonus/penalty
        if item.seller_rating >= 4.5:
            risk += 15
        elif item.seller_rating >= 4.0:
            risk += 5
        elif item.seller_rating > 0 and item.seller_rating < 3.0:
            risk -= 20

        # Price volatility risk
        if ebay_analysis.max_price > 0 and ebay_analysis.min_price > 0:
            volatility = (ebay_analysis.max_price - ebay_analysis.min_price) / ebay_analysis.max_price
            if volatility > 0.7:
                risk -= 15  # high volatility = risky
            elif volatility < 0.3:
                risk += 10  # stable prices = less risky

        # High source price = more capital at risk
        if result.source_price_usd > 200:
            risk -= 10
        if result.source_price_usd > 400:
            risk -= 20

        result.risk_score = min(100, max(0, risk))

        # Overall score: weighted combination
        result.overall_score = (
            result.margin_score * 0.45
            + result.demand_score * 0.30
            + result.risk_score * 0.25
        )

        # Viability check
        result.is_viable = self._check_viability(result)

        return result

    def _check_viability(self, score: CandidateScore) -> bool:
        """Check if a score meets minimum thresholds."""
        reasons = []

        if score.estimated_margin_percent < self.min_margin_percent:
            reasons.append(
                f"Margin {score.estimated_margin_percent:.0f}% < min {self.min_margin_percent:.0f}%"
            )

        if score.source_price_usd > self.max_source_price_usd:
            reasons.append(
                f"Source price ${score.source_price_usd:.0f} > max ${self.max_source_price_usd:.0f}"
            )

        if score.estimated_sale_price <= 0:
            reasons.append("No eBay price data (no sold listings found)")

        if score.estimated_profit < 10:
            reasons.append(f"Profit ${score.estimated_profit:.0f} too low")

        if score.ebay_analysis.total_sold < 3:
            reasons.append(f"Only {score.ebay_analysis.total_sold} sold listings (low demand)")

        score.rejection_reasons = reasons
        return len(reasons) == 0

    def rank_candidates(self, scores: list[CandidateScore]) -> list[CandidateScore]:
        """Rank candidates by overall score, viable first."""
        viable = [s for s in scores if s.is_viable]
        not_viable = [s for s in scores if not s.is_viable]

        viable.sort(key=lambda s: s.overall_score, reverse=True)
        not_viable.sort(key=lambda s: s.overall_score, reverse=True)

        return viable + not_viable
