"""Pricing intelligence engine — market-aware profit optimization.

This is the brain that decides:
1. What price to list at on the target platform
2. Whether an item is worth listing at all
3. How to maximize profit per dollar of capital deployed
4. When to adjust prices based on market movement

Key insight: We don't just maximize margin %. We maximize:
  profit_per_day = expected_profit / expected_days_to_sell

A $50 profit item that sells in 2 days beats a $100 profit item that takes 30 days.
Capital turnover is everything.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from src.config.settings import settings


# --- Fee structures (2025 rates) ---

EBAY_FEES = {
    # Final value fee by category (percentage)
    "final_value_fee": {
        "default": 0.13,           # 13% most categories
        "electronics": 0.12,       # 12% consumer electronics
        "clothing": 0.15,          # 15% clothing & accessories
        "collectibles": 0.13,      # 13% collectibles & art
        "toys": 0.13,              # 13% toys & hobbies
        "cameras": 0.12,           # 12% cameras & photo
        "music": 0.13,             # 13% music (records)
        "trading_cards": 0.13,     # 13% trading cards
    },
    "payment_processing_pct": 0.029,  # 2.9%
    "payment_processing_fixed": 0.30,  # $0.30 per transaction
    "insertion_fee": 0.0,              # Free for most sellers (250 free listings/mo)
    "promoted_listing_min": 0.02,      # 2% minimum for promoted listings
}

MERCARI_JP_FEES = {
    "selling_fee": 0.10,  # 10% of sale price
    "payment_fee": 0.03,  # 3% payment processing
}

PROXY_SERVICE_FEES = {
    "buyee": {
        "receiving_fee": 200,     # ¥200 per item received
        "consolidation_fee": 500,  # ¥500 per package consolidation
        "payment_fee_pct": 0.03,   # 3% payment fee
    },
    "zenmarket": {
        "service_fee": 300,        # ¥300 flat per item
        "payment_fee_pct": 0.03,
    },
    "fromjapan": {
        "service_fee": 400,        # ¥400 flat per item
        "payment_fee_pct": 0.03,
    },
}

# Shipping cost estimates (Japan → US) by weight
SHIPPING_TIERS = [
    # (max_weight_kg, epacket, dhl_express, fedex, ems)
    (0.1,   8,  25, 28, 15),
    (0.25,  12, 30, 33, 18),
    (0.5,   15, 38, 42, 22),
    (1.0,   20, 48, 52, 28),
    (2.0,   28, 65, 70, 38),
    (5.0,   45, 95, 100, 55),
    (10.0,  70, 140, 150, 80),
    (20.0, 110, 200, 210, 120),
    (float('inf'), 150, 280, 290, 160),
]

# Category-specific data for pricing decisions
CATEGORY_PRICING = {
    "anime_figure": {
        "avg_days_to_sell": 7,
        "price_volatility": 0.25,     # 25% std dev in prices
        "seasonality": "high",         # spikes around anime releases
        "counterfeit_risk": "medium",
        "sweet_spot_price_usd": (50, 200),  # fastest selling range
        "max_competition_price": 100,  # below this, lots of competition
        "premium_above_retail": 1.5,   # 1.5x retail = premium pricing
    },
    "vintage_camera": {
        "avg_days_to_sell": 14,
        "price_volatility": 0.35,
        "seasonality": "low",
        "counterfeit_risk": "low",
        "sweet_spot_price_usd": (80, 500),
        "max_competition_price": 150,
        "premium_above_retail": 1.2,
    },
    "japanese_knife": {
        "avg_days_to_sell": 10,
        "price_volatility": 0.20,
        "seasonality": "medium",       # gift seasons
        "counterfeit_risk": "medium",
        "sweet_spot_price_usd": (60, 300),
        "max_competition_price": 100,
        "premium_above_retail": 1.3,
    },
    "streetwear": {
        "avg_days_to_sell": 5,
        "price_volatility": 0.40,
        "seasonality": "high",         # drops, collabs
        "counterfeit_risk": "high",
        "sweet_spot_price_usd": (50, 300),
        "max_competition_price": 80,
        "premium_above_retail": 2.0,
    },
    "trading_cards": {
        "avg_days_to_sell": 3,
        "price_volatility": 0.50,
        "seasonality": "high",         # set releases
        "counterfeit_risk": "high",
        "sweet_spot_price_usd": (10, 200),
        "max_competition_price": 50,
        "premium_above_retail": 3.0,
    },
    "vintage_electronics": {
        "avg_days_to_sell": 21,
        "price_volatility": 0.30,
        "seasonality": "low",
        "counterfeit_risk": "low",
        "sweet_spot_price_usd": (50, 400),
        "max_competition_price": 120,
        "premium_above_retail": 1.3,
    },
    "japanese_ceramics": {
        "avg_days_to_sell": 18,
        "price_volatility": 0.35,
        "seasonality": "medium",
        "counterfeit_risk": "low",
        "sweet_spot_price_usd": (30, 200),
        "max_competition_price": 80,
        "premium_above_retail": 1.5,
    },
    "retro_games": {
        "avg_days_to_sell": 6,
        "price_volatility": 0.40,
        "seasonality": "medium",
        "counterfeit_risk": "medium",
        "sweet_spot_price_usd": (20, 300),
        "max_competition_price": 60,
        "premium_above_retail": 2.0,
    },
    "vinyl_records": {
        "avg_days_to_sell": 12,
        "price_volatility": 0.35,
        "seasonality": "medium",       # Record Store Day etc
        "counterfeit_risk": "low",
        "sweet_spot_price_usd": (20, 150),
        "max_competition_price": 70,
        "premium_above_retail": 1.8,
    },
    "vintage_toys": {
        "avg_days_to_sell": 14,
        "price_volatility": 0.35,
        "seasonality": "high",         # holiday season
        "counterfeit_risk": "medium",
        "sweet_spot_price_usd": (30, 250),
        "max_competition_price": 80,
        "premium_above_retail": 1.8,
    },
}


@dataclass
class MarketPriceData:
    """Aggregated market price data for a product."""

    # Source prices
    source_price_jpy: float = 0.0
    source_price_usd: float = 0.0

    # Target market prices (from sold listings)
    target_median_price: float = 0.0
    target_avg_price: float = 0.0
    target_min_price: float = 0.0
    target_max_price: float = 0.0
    target_25th_percentile: float = 0.0
    target_75th_percentile: float = 0.0
    num_sold_listings: int = 0

    # Price trends
    price_trend: str = "stable"  # "rising", "falling", "stable"
    price_trend_pct: float = 0.0  # % change over last 30 days

    # Demand signals
    avg_days_to_sell: float = 0.0
    sell_through_rate: float = 0.0  # % of listings that sell
    watchers_per_listing: float = 0.0

    # Competition
    num_active_listings: int = 0
    competition_level: str = "medium"  # "low", "medium", "high"


@dataclass
class FeeBreakdown:
    """Complete fee breakdown for a transaction."""

    # Source platform fees
    source_selling_fee: float = 0.0
    source_payment_fee: float = 0.0

    # Proxy service fees
    proxy_service_fee: float = 0.0
    proxy_payment_fee: float = 0.0

    # Shipping
    shipping_cost: float = 0.0
    insurance_cost: float = 0.0
    customs_duties: float = 0.0

    # Target platform fees
    target_final_value_fee: float = 0.0
    target_payment_fee: float = 0.0
    target_insertion_fee: float = 0.0
    target_promoted_listing_fee: float = 0.0

    # LLM costs
    llm_cost: float = 0.0

    @property
    def total_fees(self) -> float:
        return (
            self.source_selling_fee + self.source_payment_fee
            + self.proxy_service_fee + self.proxy_payment_fee
            + self.shipping_cost + self.insurance_cost + self.customs_duties
            + self.target_final_value_fee + self.target_payment_fee
            + self.target_insertion_fee + self.target_promoted_listing_fee
            + self.llm_cost
        )


@dataclass
class ProfitAnalysis:
    """Complete profit analysis for a potential listing."""

    # Revenue
    listing_price: float = 0.0
    expected_sale_price: float = 0.0  # may differ from listing (best offers)

    # Costs
    source_cost: float = 0.0
    fees: FeeBreakdown = field(default_factory=FeeBreakdown)

    # Profit
    gross_profit: float = 0.0
    gross_margin_pct: float = 0.0
    net_profit: float = 0.0
    net_margin_pct: float = 0.0
    roi_pct: float = 0.0  # return on investment

    # Capital efficiency
    capital_deployed: float = 0.0
    expected_days_to_sell: float = 0.0
    profit_per_day: float = 0.0
    annualized_roi: float = 0.0

    # Risk-adjusted
    stockout_probability: float = 0.0
    expected_profit_adjusted: float = 0.0  # profit × (1 - stockout_prob)

    # Scoring
    profit_score: float = 0.0       # 0-100
    velocity_score: float = 0.0     # 0-100
    risk_score: float = 0.0         # 0-100
    capital_efficiency_score: float = 0.0  # 0-100
    composite_score: float = 0.0    # 0-100

    # Decision
    is_viable: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    recommendation: str = ""  # "strong_buy", "buy", "hold", "skip"


class PricingEngine:
    """Market-aware pricing and profit optimization engine."""

    def __init__(
        self,
        jpy_to_usd: float = 0.0067,
        proxy_service: str = "buyee",
        shipping_method: str = "epacket",
        promoted_listing: bool = False,
    ):
        self.jpy_to_usd = jpy_to_usd
        self.proxy_service = proxy_service
        self.shipping_method = shipping_method
        self.promoted_listing = promoted_listing

    def analyze(
        self,
        source_price_jpy: float,
        market_data: MarketPriceData,
        category: str = "",
        item_weight_kg: float = 0.3,
        item_condition: str = "good",
        is_rare: bool = False,
    ) -> ProfitAnalysis:
        """Full profit analysis for a potential listing.

        This is the core decision function. It computes:
        1. All fees (source, proxy, shipping, target)
        2. Optimal listing price
        3. Expected profit and margin
        4. Capital efficiency (profit per day)
        5. Risk-adjusted expected profit
        6. Composite score for ranking
        """
        analysis = ProfitAnalysis()

        # Convert source price
        analysis.source_cost = source_price_jpy * self.jpy_to_usd
        market_data.source_price_jpy = source_price_jpy
        market_data.source_price_usd = analysis.source_cost

        # Calculate all fees
        analysis.fees = self._calculate_fees(
            analysis.source_cost,
            market_data,
            category,
            item_weight_kg,
        )

        # Determine optimal listing price
        analysis.listing_price = self._optimize_listing_price(
            market_data, category, item_condition, is_rare
        )
        analysis.expected_sale_price = analysis.listing_price * 0.92  # ~8% below listing (best offers)

        # Calculate profit
        analysis.gross_profit = analysis.expected_sale_price - analysis.source_cost
        analysis.net_profit = analysis.gross_profit - analysis.fees.total_fees

        if analysis.expected_sale_price > 0:
            analysis.gross_margin_pct = (analysis.gross_profit / analysis.expected_sale_price) * 100
            analysis.net_margin_pct = (analysis.net_profit / analysis.expected_sale_price) * 100

        if analysis.source_cost > 0:
            analysis.roi_pct = (analysis.net_profit / analysis.source_cost) * 100

        # Capital efficiency
        analysis.capital_deployed = analysis.source_cost + analysis.fees.proxy_service_fee
        cat_pricing = CATEGORY_PRICING.get(category, {})
        analysis.expected_days_to_sell = cat_pricing.get("avg_days_to_sell", 14)

        if market_data.avg_days_to_sell > 0:
            analysis.expected_days_to_sell = market_data.avg_days_to_sell

        if analysis.expected_days_to_sell > 0:
            analysis.profit_per_day = analysis.net_profit / analysis.expected_days_to_sell
            analysis.annualized_roi = analysis.profit_per_day * 365 / max(analysis.capital_deployed, 0.01) * 100

        # Risk adjustment
        analysis.stockout_probability = self._estimate_stockout_risk(market_data, category)
        analysis.expected_profit_adjusted = analysis.net_profit * (1 - analysis.stockout_probability)

        # Scoring
        analysis.profit_score = self._score_profit(analysis)
        analysis.velocity_score = self._score_velocity(analysis, market_data)
        analysis.risk_score = self._score_risk(analysis, market_data, category)
        analysis.capital_efficiency_score = self._score_capital_efficiency(analysis)

        # Composite: weighted combination
        # Profit matters most, but velocity (turnover) is critical for capital efficiency
        analysis.composite_score = (
            analysis.profit_score * 0.30
            + analysis.velocity_score * 0.25
            + analysis.risk_score * 0.20
            + analysis.capital_efficiency_score * 0.25
        )

        # Viability check
        analysis.is_viable, analysis.rejection_reasons = self._check_viability(analysis, category)

        # Recommendation
        analysis.recommendation = self._get_recommendation(analysis)

        return analysis

    def _calculate_fees(
        self,
        source_cost_usd: float,
        market_data: MarketPriceData,
        category: str,
        weight_kg: float,
    ) -> FeeBreakdown:
        """Calculate all fees for a transaction."""
        fees = FeeBreakdown()
        listing_price = market_data.target_median_price or 0

        # Source platform fees (Mercari JP)
        mercari = MERCARI_JP_FEES
        source_sale_price_usd = source_cost_usd  # What we pay on Mercari
        fees.source_selling_fee = source_sale_price_usd * mercari["selling_fee"]
        fees.source_payment_fee = source_sale_price_usd * mercari["payment_fee"]

        # Proxy service fees
        proxy = PROXY_SERVICE_FEES.get(self.proxy_service, PROXY_SERVICE_FEES["buyee"])
        if "receiving_fee" in proxy:
            fees.proxy_service_fee = proxy["receiving_fee"] * self.jpy_to_usd
            if "consolidation_fee" in proxy:
                fees.proxy_service_fee += proxy["consolidation_fee"] * self.jpy_to_usd
        else:
            fees.proxy_service_fee = proxy["service_fee"] * self.jpy_to_usd
        fees.proxy_payment_fee = source_sale_price_usd * proxy["payment_fee_pct"]

        # Shipping
        fees.shipping_cost = self._estimate_shipping(weight_kg, self.shipping_method)
        if listing_price > 100:
            fees.insurance_cost = min(listing_price * 0.02, 25)  # 2% insurance, max $25

        # Customs (usually $0 for items under $800 via de minimis)
        if listing_price > 800:
            fees.customs_duties = listing_price * 0.03  # ~3% average duty

        # Target platform fees (eBay)
        ebay_fees = EBAY_FEES
        cat_fee = ebay_fees["final_value_fee"].get(category, ebay_fees["final_value_fee"]["default"])
        fees.target_final_value_fee = listing_price * cat_fee
        fees.target_payment_fee = listing_price * ebay_fees["payment_processing_pct"] + ebay_fees["payment_processing_fixed"]
        fees.target_insertion_fee = ebay_fees["insertion_fee"]

        if self.promoted_listing:
            fees.target_promoted_listing_fee = listing_price * ebay_fees["promoted_listing_min"]

        # LLM cost per listing (translation + enrichment)
        fees.llm_cost = 0.03  # ~$0.03 per listing with Claude Sonnet

        return fees

    def _optimize_listing_price(
        self,
        market_data: MarketPriceData,
        category: str,
        condition: str,
        is_rare: bool,
    ) -> float:
        """Determine optimal listing price.

        Strategy:
        - Start at 75th percentile of sold listings (competitive but profitable)
        - Adjust for condition (used = -15% vs new)
        - Premium for rare/limited items (+20-50%)
        - Avoid pricing in high-competition zones
        - Round to psychologically appealing price ($X.99 or $X.95)
        """
        if market_data.target_median_price <= 0:
            return 0

        # Base price: 75th percentile (sells faster than median, better profit than max)
        base_price = market_data.target_75th_percentile or market_data.target_median_price

        # If we have enough data, use actual percentiles
        if market_data.num_sold_listings >= 10:
            base_price = market_data.target_75th_percentile or market_data.target_median_price
        elif market_data.num_sold_listings >= 3:
            base_price = market_data.target_median_price * 1.1  # slight premium
        else:
            # Limited data — be conservative
            base_price = market_data.target_median_price

        # Condition adjustment
        condition_multipliers = {
            "new": 1.15,
            "like_new": 1.05,
            "good": 1.0,
            "fair": 0.85,
            "poor": 0.70,
        }
        base_price *= condition_multipliers.get(condition, 1.0)

        # Rarity premium
        if is_rare:
            base_price *= 1.3

        # Category-specific adjustments
        cat_pricing = CATEGORY_PRICING.get(category, {})
        sweet_spot = cat_pricing.get("sweet_spot_price_usd", (0, float('inf')))

        # If price is in the "sweet spot" range, it'll sell faster
        if sweet_spot[0] <= base_price <= sweet_spot[1]:
            pass  # good, no adjustment
        elif base_price < sweet_spot[0]:
            # Below sweet spot — might be too cheap, could mean low quality
            base_price = max(base_price, sweet_spot[0] * 0.8)
        elif base_price > sweet_spot[1]:
            # Above sweet spot — will take longer to sell
            pass  # that's fine, we want the profit

        # Competition check
        max_comp = cat_pricing.get("max_competition_price", 0)
        if max_comp > 0 and base_price < max_comp:
            # High competition zone — either go lower (race to bottom, bad)
            # or go higher (position as premium)
            if not is_rare:
                base_price = max_comp * 1.2  # position above competition

        # Psychological pricing
        base_price = self._psychological_price(base_price)

        return round(base_price, 2)

    def _psychological_price(self, price: float) -> float:
        """Apply psychological pricing (charm pricing)."""
        if price < 10:
            return int(price) - 0.01  # $9.99 style
        elif price < 100:
            return int(price) - 0.01  # $49.99 style
        elif price < 500:
            return int(price / 5) * 5 - 0.01  # $199.99 style
        else:
            return int(price / 10) * 10 - 0.01  # $599.99 style

    def _estimate_shipping(self, weight_kg: float, method: str) -> float:
        """Estimate shipping cost from Japan to US."""
        method_idx = {"epacket": 0, "dhl_express": 1, "fedex": 2, "ems": 3}
        idx = method_idx.get(method, 0)

        for max_weight, *costs in SHIPPING_TIERS:
            if weight_kg <= max_weight:
                return costs[idx]

        return SHIPPING_TIERS[-1][idx]

    def _estimate_stockout_risk(
        self, market_data: MarketPriceData, category: str
    ) -> float:
        """Estimate probability that source item sells before we can fulfill.

        Factors:
        - How many watchers the source listing has
        - How long the source listing has been up
        - Category velocity (fast-moving categories = higher risk)
        - Our polling interval
        """
        base_risk = 0.03  # 3% base (matches the Japanese guy's experience)

        # More watchers = higher risk
        if market_data.watchers_per_listing > 10:
            base_risk += 0.05
        elif market_data.watchers_per_listing > 5:
            base_risk += 0.02

        # Fast-moving categories
        cat_pricing = CATEGORY_PRICING.get(category, {})
        avg_days = cat_pricing.get("avg_days_to_sell", 14)
        if avg_days <= 5:
            base_risk += 0.05  # fast-moving = higher stockout risk
        elif avg_days <= 10:
            base_risk += 0.02

        # High sell-through = higher risk
        if market_data.sell_through_rate > 0.8:
            base_risk += 0.03

        return min(0.30, base_risk)  # cap at 30%

    def _score_profit(self, analysis: ProfitAnalysis) -> float:
        """Score profit potential 0-100."""
        if analysis.net_profit <= 0:
            return 0

        # Score based on absolute profit and margin
        profit_score = min(50, analysis.net_profit * 2)  # $25 profit = 50 points
        margin_score = min(50, analysis.net_margin_pct * 0.5)  # 100% margin = 50 points

        return min(100, profit_score + margin_score)

    def _score_velocity(
        self, analysis: ProfitAnalysis, market_data: MarketPriceData
    ) -> float:
        """Score how quickly the item will sell 0-100."""
        days = analysis.expected_days_to_sell
        if days <= 0:
            return 50

        # Faster = better
        if days <= 3:
            return 100
        elif days <= 7:
            return 85
        elif days <= 14:
            return 65
        elif days <= 21:
            return 45
        elif days <= 30:
            return 25
        else:
            return 10

    def _score_risk(
        self, analysis: ProfitAnalysis, market_data: MarketPriceData, category: str
    ) -> float:
        """Score risk level 0-100 (higher = less risky)."""
        score = 70.0  # baseline

        # Stockout risk
        score -= analysis.stockout_probability * 100

        # Price volatility
        cat_pricing = CATEGORY_PRICING.get(category, {})
        volatility = cat_pricing.get("price_volatility", 0.3)
        score -= volatility * 30

        # Counterfeit risk
        counterfeit_risk = cat_pricing.get("counterfeit_risk", "medium")
        if counterfeit_risk == "high":
            score -= 15
        elif counterfeit_risk == "medium":
            score -= 5

        # Data confidence
        if market_data.num_sold_listings < 5:
            score -= 10  # low data = higher risk

        # High capital at risk
        if analysis.capital_deployed > 200:
            score -= 10

        return max(0, min(100, score))

    def _score_capital_efficiency(self, analysis: ProfitAnalysis) -> float:
        """Score capital efficiency 0-100.

        This is the key differentiator: profit per day per dollar deployed.
        """
        if analysis.profit_per_day <= 0:
            return 0

        # $10/day on $50 capital = excellent
        # $5/day on $200 capital = mediocre
        efficiency = analysis.profit_per_day / max(analysis.capital_deployed, 0.01)

        if efficiency > 0.5:  # 50%+ daily return on capital
            return 100
        elif efficiency > 0.3:
            return 85
        elif efficiency > 0.15:
            return 65
        elif efficiency > 0.08:
            return 45
        elif efficiency > 0.03:
            return 25
        else:
            return 10

    def _check_viability(
        self, analysis: ProfitAnalysis, category: str
    ) -> tuple[bool, list[str]]:
        """Check if a listing is viable. Returns (is_viable, reasons)."""
        reasons = []

        if analysis.net_profit < 15:
            reasons.append(f"Net profit ${analysis.net_profit:.2f} < $15 minimum")

        if analysis.net_margin_pct < 20:
            reasons.append(f"Net margin {analysis.net_margin_pct:.0f}% < 20% minimum")

        if analysis.roi_pct < 30:
            reasons.append(f"ROI {analysis.roi_pct:.0f}% < 30% minimum")

        if analysis.expected_days_to_sell > 30:
            reasons.append(f"Days to sell {analysis.expected_days_to_sell:.0f} > 30 max")

        if analysis.stockout_probability > 0.15:
            reasons.append(f"Stockout risk {analysis.stockout_probability:.0%} > 15% max")

        if analysis.capital_deployed > settings.max_source_price_usd:
            reasons.append(
                f"Capital ${analysis.capital_deployed:.0f} > ${settings.max_source_price_usf:.0f} max"
            )

        # Category-specific checks
        cat_pricing = CATEGORY_PRICING.get(category, {})
        price_range = cat_pricing.get("price_range_usd", (0, float('inf')))
        if analysis.listing_price > 0:
            if analysis.listing_price < price_range[0]:
                reasons.append(f"Price ${analysis.listing_price:.0f} below category minimum ${price_range[0]}")
            if analysis.listing_price > price_range[1]:
                reasons.append(f"Price ${analysis.listing_price:.0f} above category maximum ${price_range[1]}")

        return len(reasons) == 0, reasons

    def _get_recommendation(self, analysis: ProfitAnalysis) -> str:
        """Get buy/hold/skip recommendation."""
        if not analysis.is_viable:
            return "skip"
        if analysis.composite_score >= 75:
            return "strong_buy"
        elif analysis.composite_score >= 55:
            return "buy"
        elif analysis.composite_score >= 40:
            return "hold"
        else:
            return "skip"
