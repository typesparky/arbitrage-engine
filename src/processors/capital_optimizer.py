"""Capital efficiency optimizer — manages the portfolio as a whole.

Key insight: The goal is NOT to maximize profit per item.
The goal is to maximize TOTAL PROFIT PER DAY given fixed capital.

This is a portfolio optimization problem:
- We have $X capital
- We can list N items at a time
- Each item ties up capital for Y days
- We want to maximize: sum(profit_i) / time_period

The optimizer:
1. Allocates capital across categories based on velocity × margin
2. Determines optimal number of active listings per category
3. Rebalances as items sell and capital frees up
4. Identifies when to hold capital vs deploy it
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.processors.pricing import CATEGORY_PRICING


# Category performance profiles (from market research)
# Format: (avg_margin_pct, avg_days_to_sell, sell_through_rate, capital_per_item)
CATEGORY_PROFILES = {
    "anime_figure":    (0.35, 7,  0.75, 50),
    "vintage_camera":  (0.30, 14, 0.60, 120),
    "japanese_knife":  (0.40, 10, 0.70, 60),
    "streetwear":      (0.35, 5,  0.80, 55),
    "trading_cards":   (0.50, 3,  0.85, 25),
    "vintage_electronics": (0.28, 21, 0.50, 80),
    "japanese_ceramics":   (0.45, 18, 0.45, 40),
    "retro_games":     (0.40, 6,  0.75, 35),
    "vinyl_records":   (0.35, 12, 0.65, 30),
    "vintage_toys":    (0.40, 14, 0.55, 45),
}


@dataclass
class CategoryAllocation:
    """Capital allocation for a category."""

    category: str
    capital_allocated: float = 0.0
    num_listings: int = 0
    expected_daily_profit: float = 0.0
    expected_monthly_profit: float = 0.0
    capital_turns_per_month: float = 0.0  # how many times capital rotates
    efficiency: float = 0.0  # profit per day per dollar


@dataclass
class PortfolioPlan:
    """Optimal portfolio allocation plan."""

    total_capital: float
    reserve_capital: float  # keep in reserve for opportunistic buys
    deployable_capital: float

    allocations: list[CategoryAllocation] = field(default_factory=list)

    total_listings: int = 0
    total_expected_daily_profit: float = 0.0
    total_expected_monthly_profit: float = 0.0
    overall_capital_efficiency: float = 0.0

    # Recommendations
    recommendations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class CapitalOptimizer:
    """Optimizes capital allocation across categories for maximum daily profit."""

    def __init__(
        self,
        total_capital: float = 2000.0,
        reserve_pct: float = 0.15,  # keep 15% in reserve
        max_single_category_pct: float = 0.35,  # max 35% in one category
        min_category_allocation: float = 100.0,  # minimum $100 per category
    ):
        self.total_capital = total_capital
        self.reserve_pct = reserve_pct
        self.max_single_category_pct = max_single_category_pct
        self.min_category_allocation = min_category_allocation

    def optimize(
        self,
        current_portfolio: dict[str, int] | None = None,
        category_performance: dict[str, tuple[float, float]] | None = None,
    ) -> PortfolioPlan:
        """Generate optimal capital allocation.

        Args:
            current_portfolio: {category: num_active_listings}
            category_performance: {category: (avg_profit, avg_days)} from real data

        Returns:
            PortfolioPlan with optimal allocations
        """
        plan = PortfolioPlan(
            total_capital=self.total_capital,
            reserve_capital=self.total_capital * self.reserve_pct,
            deployable_capital=self.total_capital * (1 - self.reserve_pct),
        )

        # Step 1: Calculate efficiency score for each category
        efficiencies: list[tuple[str, float]] = []
        for cat, profile in CATEGORY_PROFILES.items():
            margin_pct, days_to_sell, sell_through, capital_per_item = profile

            # Use real performance data if available
            if category_performance and cat in category_performance:
                real_profit, real_days = category_performance[cat]
                if real_days > 0 and real_profit > 0:
                    margin_pct = real_profit / capital_per_item
                    days_to_sell = real_days

            # Efficiency = margin / days_to_sell
            # This gives us profit per day per dollar
            daily_return = margin_pct / max(days_to_sell, 1)
            efficiency = daily_return * sell_through  # adjust for sell-through rate

            efficiencies.append((cat, efficiency))

        # Step 2: Sort by efficiency (highest first)
        efficiencies.sort(key=lambda x: x[1], reverse=True)

        # Step 3: Allocate capital using weighted distribution
        total_efficiency = sum(e for _, e in efficiencies)
        if total_efficiency == 0:
            return plan

        deployable = plan.deployable_capital

        for cat, efficiency in efficiencies:
            # Weight by efficiency
            weight = efficiency / total_efficiency

            # Apply max concentration limit
            max_capital = deployable * self.max_single_category_pct
            allocated = min(deployable * weight, max_capital)

            # Skip if below minimum
            if allocated < self.min_category_allocation:
                continue

            profile = CATEGORY_PROFILES.get(cat)
            if not profile:
                continue

            _, days_to_sell, sell_through, capital_per_item = profile

            # Calculate how many items we can list
            num_listings = max(1, int(allocated / max(capital_per_item, 1)))
            actual_allocation = num_listings * capital_per_item

            # Expected profit per cycle (per item sold)
            margin_pct = profile[0]
            profit_per_item = capital_per_item * margin_pct

            # Turns per month
            turns_per_month = 30.0 / max(days_to_sell, 1) * sell_through

            # Expected monthly profit from this category
            monthly_profit = profit_per_item * num_listings * turns_per_month

            allocation = CategoryAllocation(
                category=cat,
                capital_allocated=actual_allocation,
                num_listings=num_listings,
                expected_daily_profit=monthly_profit / 30,
                expected_monthly_profit=monthly_profit,
                capital_turns_per_month=turns_per_month,
                efficiency=efficiency,
            )

            plan.allocations.append(allocation)
            deployable -= actual_allocation

        # Step 4: Calculate totals
        plan.total_listings = sum(a.num_listings for a in plan.allocations)
        plan.total_expected_daily_profit = sum(
            a.expected_daily_profit for a in plan.allocations
        )
        plan.total_expected_monthly_profit = sum(
            a.expected_monthly_profit for a in plan.allocations
        )

        deployed = sum(a.capital_allocated for a in plan.allocations)
        if deployed > 0:
            plan.overall_capital_efficiency = (
                plan.total_expected_daily_profit / deployed
            )

        # Step 5: Generate recommendations
        plan.recommendations = self._generate_recommendations(plan)
        plan.warnings = self._generate_warnings(plan)

        return plan

    def _generate_recommendations(self, plan: PortfolioPlan) -> list[str]:
        """Generate actionable recommendations."""
        recs = []

        if not plan.allocations:
            return ["No viable categories found — adjust min_allocation or capital"]

        # Top performing category
        top = max(plan.allocations, key=lambda a: a.efficiency)
        recs.append(
            f"Focus on {top.category}: {top.capital_turns_per_month:.1f}x monthly turnover "
            f"at {CATEGORY_PROFILES[top.category][0]:.0%} margin"
        )

        # Underallocated high-efficiency categories
        for alloc in plan.allocations:
            if alloc.capital_allocated < plan.deployable_capital * 0.1:
                recs.append(
                    f"Consider increasing {alloc.category} allocation "
                    f"(currently ${alloc.capital_allocated:.0f}, efficiency: {alloc.efficiency:.2f})"
                )

        # Monthly projection
        recs.append(
            f"Projected monthly profit: ${plan.total_expected_monthly_profit:.0f} "
            f"on ${sum(a.capital_allocated for a in plan.allocations):.0f} deployed capital "
            f"({plan.total_expected_monthly_profit / max(sum(a.capital_allocated for a in plan.allocations), 1) * 100:.0f}% monthly return)"
        )

        # Capital reserve
        recs.append(
            f"Keep ${plan.reserve_capital:.0f} in reserve for opportunistic buys"
        )

        return recs

    def _generate_warnings(self, plan: PortfolioPlan) -> list[str]:
        """Generate warnings about the portfolio."""
        warnings = []

        # Concentration risk
        for alloc in plan.allocations:
            pct = (
                alloc.capital_allocated
                / max(sum(a.capital_alloced for a in plan.allocations), 1)
            )
            if pct > 0.4:
                warnings.append(
                    f"{alloc.category} is {pct:.0%} of portfolio — concentration risk"
                )

        # Low liquidity categories
        slow_cats = [
            a.category for a in plan.allocations
            if CATEGORY_PROFILES.get(a.category, (0, 0, 0, 0))[1] > 20
        ]
        if slow_cats:
            warnings.append(
                f"Slow-moving categories (20+ days to sell): {', '.join(slow_cats)}"
            )

        # Overall efficiency
        if plan.overall_capital_efficiency < 0.02:  # less than 2% daily return
            warnings.append(
                f"Low overall efficiency ({plan.overall_capital_efficiency:.2%}/day) — "
                "consider focusing on higher-velocity categories"
            )

        return warnings

    def get_rebalance_actions(
        self,
        plan: PortfolioPlan,
        current_portfolio: dict[str, int],
        current_capital_deployed: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Get specific rebalance actions to move from current to optimal.

        Returns list of actions like:
        - {"action": "increase", "category": "trading_cards", "add_listings": 3}
        - {"action": "decrease", "category": "vintage_electronics", "remove_listings": 2}
        """
        actions = []

        for alloc in plan.allocations:
            cat = alloc.category
            current_count = current_portfolio.get(cat, 0)
            current_capital = current_capital_deployed.get(cat, 0)
            target_count = alloc.num_listings
            target_capital = alloc.capital_allocated

            if target_count > current_count:
                actions.append({
                    "action": "increase",
                    "category": cat,
                    "add_listings": target_count - current_count,
                    "add_capital": target_capital - current_capital,
                    "reason": f"Increase from {current_count} to {target_count} listings",
                })
            elif target_count < current_count:
                actions.append({
                    "action": "decrease",
                    "category": cat,
                    "remove_listings": current_count - target_count,
                    "free_capital": current_capital - target_capital,
                    "reason": f"Reduce from {current_count} to {target_count} listings",
                })

        # Categories to exit entirely
        for cat, count in current_portfolio.items():
            if count > 0 and not any(a.category == cat for a in plan.allocations):
                actions.append({
                    "action": "exit",
                    "category": cat,
                    "remove_listings": count,
                    "reason": f"Exit {cat} — low efficiency",
                })

        return actions
