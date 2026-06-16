"""Streamlit dashboard for the Arbitrage Engine.

Run with: streamlit run scripts/dashboard.py

Pages:
1. Portfolio Overview — active listings, revenue, margins, category breakdown
2. Discovery Results — latest scan results, ranked candidates, filters
3. Pricing Analysis — fee breakdowns, profit analysis, price optimization
4. Capital Allocation — portfolio plan, category distribution, efficiency
5. Market Data — price trends, sold listing history, competition
6. Settings — configuration, thresholds, platform status
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import settings
from src.models.database import (
    Base,
    SourceListing,
    TargetListing,
    PriceSnapshot,
    EbaySoldListing,
    SyncLog,
    ListingStatus,
    SourcePlatform,
    TargetPlatform,
)
from src.processors.pricing import (
    PricingEngine,
    MarketPriceData,
    CATEGORY_PRICING,
    CATEGORY_TAXONOMY,
    EBAY_FEES,
    SHIPPING_TIERS,
)
from src.processors.capital_optimizer import CapitalOptimizer, CATEGORY_PROFILES
from src.processors.product_id import ProductIdentifier, CATEGORY_TAXONOMY as CAT_TAX


# --- Page config ---
st.set_page_config(
    page_title="Arbitrage Engine",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Database setup ---
@st.cache_resource
def get_engine():
    engine = create_async_engine(settings.database_url, echo=False)
    return engine

@st.cache_resource
def get_session_factory():
    engine = get_engine()
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_session():
    factory = get_session_factory()
    async with factory() as session:
        yield session


# --- Data fetching helpers ---

async def fetch_portfolio_summary(session):
    """Get portfolio summary stats."""
    # Source listings
    total_sources = await session.execute(select(func.count(SourceListing.id)))
    active_sources = await session.execute(
        select(func.count(SourceListing.id)).where(
            SourceListing.status == ListingStatus.LISTED
        )
    )
    discovered = await session.execute(
        select(func.count(SourceListing.id)).where(
            SourceListing.status == ListingStatus.DISCOVERED
        )
    )

    # Target listings
    total_targets = await session.execute(select(func.count(TargetListing.id)))
    active_targets = await session.execute(
        select(func.count(TargetListing.id)).where(
            TargetListing.status == ListingStatus.LISTED
        )
    )
    sold_targets = await session.execute(
        select(func.count(TargetListing.id)).where(
            TargetListing.status == ListingStatus.SOLD
        )
    )
    delisted = await session.execute(
        select(func.count(TargetListing.id)).where(
            TargetListing.status == ListingStatus.SOURCE_GONE
        )
    )

    # Financials
    total_estimated_profit = await session.execute(
        select(func.sum(TargetListing.estimated_profit)).where(
            TargetListing.status == ListingStatus.LISTED
        )
    )
    total_actual_profit = await session.execute(
        select(func.sum(TargetListing.actual_profit)).where(
            TargetListing.status == ListingStatus.SOLD
        )
    )

    # By category
    category_counts = await session.execute(
        select(SourceListing.category, func.count(SourceListing.id))
        .where(SourceListing.status == ListingStatus.LISTED)
        .group_by(SourceListing.category)
    )

    return {
        "total_sources": total_sources.scalar() or 0,
        "active_sources": active_sources.scalar() or 0,
        "discovered": discovered.scalar() or 0,
        "total_targets": total_targets.scalar() or 0,
        "active_targets": active_targets.scalar() or 0,
        "sold": sold_targets.scalar() or 0,
        "delisted": delisted.scalar() or 0,
        "total_estimated_profit": total_estimated_profit.scalar() or 0,
        "total_actual_profit": total_actual_profit.scalar() or 0,
        "by_category": dict(category_counts.all()),
    }


async def fetch_active_listings(session, limit=50):
    """Get active target listings with source data."""
    stmt = (
        select(TargetListing, SourceListing)
        .join(SourceListing, TargetListing.source_listing_id == SourceListing.id)
        .where(TargetListing.status == ListingStatus.LISTED)
        .order_by(TargetListing.listed_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.all()


async def fetch_discovery_candidates(session, limit=50):
    """Get recent discovered candidates."""
    stmt = (
        select(SourceListing)
        .where(SourceListing.status.in_([ListingStatus.DISCOVERED, ListingStatus.CANDIDATE]))
        .order_by(SourceListing.first_seen_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_sold_history(session, limit=50):
    """Get sold listing history."""
    stmt = (
        select(TargetListing, SourceListing)
        .join(SourceListing, TargetListing.source_listing_id == SourceListing.id)
        .where(TargetListing.status == ListingStatus.SOLD)
        .order_by(TargetListing.sold_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.all()


async def fetch_sync_logs(session, limit=20):
    """Get recent sync logs."""
    stmt = select(SyncLog).order_by(SyncLog.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_price_history(session, listing_id):
    """Get price history for a source listing."""
    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.source_listing_id == listing_id)
        .order_by(PriceSnapshot.captured_at.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# --- Charts ---

def render_portfolio_overview(summary):
    """Render portfolio overview cards and charts."""
    st.title("📊 Portfolio Overview")

    # KPI cards
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.metric("Active Listings", summary["active_targets"], f"{summary['total_targets']} total")
    with col2:
        st.metric("Items Sold", summary["sold"])
    with col3:
        st.metric("Est. Profit (Active)", f"${summary['total_estimated_profit']:.0f}")
    with col4:
        st.metric("Actual Profit (Sold)", f"${summary['total_actual_profit']:.0f}")
    with col5:
        failure_rate = (
            summary["delisted"] / max(summary["total_targets"], 1) * 100
        )
        st.metric("Failure Rate", f"{failure_rate:.1f}%", f"{summary['delisted']} delisted")

    st.divider()

    # Category breakdown
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Category Distribution")
        if summary["by_category"]:
            cat_df = pd.DataFrame(
                list(summary["by_category"].items()),
                columns=["Category", "Count"],
            )
            cat_df = cat_df.sort_values("Count", ascending=True)
            st.bar_chart(cat_df.set_index("Category"))
        else:
            st.info("No active listings yet")

    with col_right:
        st.subheader("Pipeline Funnel")
        funnel_data = pd.DataFrame({
            "Stage": ["Discovered", "Candidates", "Listed", "Sold", "Delisted"],
            "Count": [
                summary["discovered"],
                summary["active_sources"],
                summary["active_targets"],
                summary["sold"],
                summary["delisted"],
            ],
        })
        st.bar_chart(funnel_data.set_index("Stage"))


def render_active_listings(listings):
    """Render active listings table."""
    st.subheader("Active Listings")

    if not listings:
        st.info("No active listings")
        return

    rows = []
    for target, source in listings:
        rows.append({
            "Title": source.title_original[:60] if source.title_original else "N/A",
            "Category": source.category or "Unknown",
            "Source Price": f"¥{source.price_original:,.0f}",
            "Listed Price": f"${target.price:.2f}",
            "Est. Profit": f"${target.estimated_profit:.2f}",
            "Listed At": target.listed_at.strftime("%Y-%m-%d %H:%M") if target.listed_at else "N/A",
            "Platform": target.target_platform.value if target.target_platform else "N/A",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_discovery_results(candidates):
    """Render discovery candidates."""
    st.title("🔍 Discovery Results")

    if not candidates:
        st.info("No candidates discovered yet. Run discovery first.")
        return

    rows = []
    for c in candidates:
        rows.append({
            "Title": c.title_original[:60] if c.title_original else "N/A",
            "Category": c.category or "Unknown",
            "Price": f"¥{c.price_original:,.0f}",
            "Condition": c.condition or "N/A",
            "Seller Rating": f"{c.seller_rating:.1f}" if c.seller_rating else "N/A",
            "Status": c.status.value if c.status else "N/A",
            "First Seen": c.first_seen_at.strftime("%Y-%m-%d %H:%M") if c.first_seen_at else "N/A",
        })

    df = pd.DataFrame(rows)

    # Filters
    col1, col2 = st.columns(2)
    with col1:
        category_filter = st.multiselect(
            "Filter by Category",
            options=df["Category"].unique().tolist(),
            default=[],
        )
    with col2:
        status_filter = st.multiselect(
            "Filter by Status",
            options=df["Status"].unique().tolist(),
            default=[],
        )

    if category_filter:
        df = df[df["Category"].isin(category_filter)]
    if status_filter:
        df = df[df["Status"].isin(status_filter)]

    st.dataframe(df, use_container_width=True, hide_index=True)


def render_pricing_analysis():
    """Render pricing analysis tools."""
    st.title("💰 Pricing Analysis")

    # Fee breakdown calculator
    st.subheader("Fee Calculator")

    col1, col2, col3 = st.columns(3)

    with col1:
        source_price_jpy = st.number_input("Source Price (JPY)", min_value=0, value=5000, step=500)
        category = st.selectbox(
            "Category",
            options=list(CATEGORY_PRICING.keys()),
            index=0,
        )
        condition = st.selectbox(
            "Condition",
            options=["new", "like_new", "good", "fair", "poor"],
            index=2,
        )

    with col2:
        ebay_median = st.number_input("eBay Median Sold Price ($)", min_value=0.0, value=150.0, step=5.0)
        weight_kg = st.number_input("Weight (kg)", min_value=0.1, value=0.3, step=0.1)
        shipping_method = st.selectbox(
            "Shipping Method",
            options=["epacket", "ems", "dhl_express", "fedex"],
            index=0,
        )

    with col3:
        is_rare = st.checkbox("Rare/Limited Edition", value=False)
        promoted = st.checkbox("Promoted Listing", value=False)
        proxy_service = st.selectbox(
            "Proxy Service",
            options=["buyee", "zenmarket", "fromjapan"],
            index=0,
        )

    # Run analysis
    if st.button("Calculate", type="primary"):
        pricing = PricingEngine(
            proxy_service=proxy_service,
            shipping_method=shipping_method,
            promoted_listing=promoted,
        )

        market_data = MarketPriceData(
            source_price_jpy=source_price_jpy,
            source_price_usd=source_price_jpy * pricing.jpy_to_usd,
            target_median_price=ebay_median,
            target_avg_price=ebay_median * 0.95,
            target_min_price=ebay_median * 0.6,
            target_max_price=ebay_median * 1.4,
            target_25th_percentile=ebay_median * 0.8,
            target_75th_percentile=ebay_median * 1.2,
            num_sold_listings=30,
        )

        cat_info = CATEGORY_TAXONOMY.get(category, {})
        analysis = pricing.analyze(
            source_price_jpy=source_price_jpy,
            market_data=market_data,
            category=category,
            item_weight_kg=weight_kg,
            item_condition=condition,
            is_rare=is_rare,
        )

        # Display results
        st.divider()
        st.subheader("Analysis Results")

        # Price recommendation
        rec_color = {
            "strong_buy": "🟢",
            "buy": "🟡",
            "hold": "🟠",
            "skip": "🔴",
        }
        st.markdown(f"### {rec_color.get(analysis.recommendation, '⚪')} Recommendation: {analysis.recommendation.upper()}")

        # Key metrics
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.metric("Listing Price", f"${analysis.listing_price:.2f}")
        with m2:
            st.metric("Net Profit", f"${analysis.net_profit:.2f}")
        with m3:
            st.metric("Net Margin", f"{analysis.net_margin_pct:.0f}%")
        with m4:
            st.metric("ROI", f"{analysis.roi_pct:.0f}%")
        with m5:
            st.metric("Profit/Day", f"${analysis.profit_per_day:.2f}")

        # Fee breakdown
        st.subheader("Fee Breakdown")
        fees = analysis.fees
        fee_data = {
            "Fee Type": [
                "Source Selling Fee", "Source Payment Fee",
                "Proxy Service Fee", "Proxy Payment Fee",
                "Shipping", "Insurance", "Customs/Duties",
                "eBay Final Value Fee", "eBay Payment Fee",
                "eBay Insertion Fee", "Promoted Listing", "LLM Cost",
                "TOTAL",
            ],
            "Amount ($)": [
                fees.source_selling_fee, fees.source_payment_fee,
                fees.proxy_service_fee, fees.proxy_payment_fee,
                fees.shipping_cost, fees.insurance_cost, fees.customs_duties,
                fees.target_final_value_fee, fees.target_payment_fee,
                fees.target_insertion_fee, fees.target_promoted_listing_fee, fees.llm_cost,
                fees.total_fees,
            ],
        }
        fee_df = pd.DataFrame(fee_data)
        st.dataframe(fee_df, use_container_width=True, hide_index=True)

        # Cost breakdown pie chart
        st.subheader("Cost Breakdown")
        cost_data = pd.DataFrame({
            "Component": ["Source Cost", "Fees", "Net Profit"],
            "Amount": [analysis.source_cost, fees.total_fees, max(0, analysis.net_profit)],
        })
        st.bar_chart(cost_data.set_index("Component"))

        # Rejection reasons
        if analysis.rejection_reasons:
            st.subheader("⚠️ Rejection Reasons")
            for reason in analysis.rejection_reasons:
                st.warning(reason)

        # Category info
        cat_pricing = CATEGORY_PRICING.get(category, {})
        if cat_pricing:
            st.subheader("Category Insights")
            c1, c2, c3 = st.columns(3)
            with c1:
                st.info(f"Avg Days to Sell: {cat_pricing.get('avg_days_to_sell', 'N/A')}")
            with c2:
                st.info(f"Price Volatility: {cat_pricing.get('price_volatility', 'N/A'):.0%}")
            with c3:
                sweet = cat_pricing.get("sweet_spot_price_usd", (0, 0))
                st.info(f"Sweet Spot: ${sweet[0]}-${sweet[1]}")


def render_capital_allocation():
    """Render capital allocation optimizer."""
    st.title("📈 Capital Allocation")

    col1, col2 = st.columns(2)

    with col1:
        total_capital = st.number_input("Total Capital ($)", min_value=100, value=2000, step=100)
        reserve_pct = st.slider("Reserve %", min_value=5, max_value=30, value=15)
        max_category_pct = st.slider("Max Single Category %", min_value=10, max_value=50, value=35)

    with col2:
        st.markdown("### Category Profiles")
        profile_data = []
        for cat, profile in CATEGORY_PROFILES.items():
            margin, days, sell_through, capital = profile
            daily_return = margin / max(days, 1) * sell_through
            profile_data.append({
                "Category": cat.replace("_", " ").title(),
                "Margin": f"{margin:.0%}",
                "Days to Sell": f"{days:.0f}",
                "Sell-Through": f"{sell_through:.0%}",
                "Capital/Item": f"${capital:.0f}",
                "Daily Return": f"{daily_return:.2%}",
            })
        st.dataframe(pd.DataFrame(profile_data), use_container_width=True, hide_index=True)

    if st.button("Optimize Allocation", type="primary"):
        optimizer = CapitalOptimizer(
            total_capital=total_capital,
            reserve_pct=reserve_pct / 100,
            max_single_category_pct=max_category_pct / 100,
        )
        plan = optimizer.optimize()

        st.divider()
        st.subheader("Optimal Allocation Plan")

        # Summary
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            st.metric("Deployable Capital", f"${plan.deployable_capital:.0f}")
        with s2:
            st.metric("Total Listings", plan.total_listings)
        with s3:
            st.metric("Expected Monthly Profit", f"${plan.total_expected_monthly_profit:.0f}")
        with s4:
            st.metric("Capital Efficiency", f"{plan.overall_capital_efficiency:.2%}/day")

        # Allocation chart
        if plan.allocations:
            alloc_data = pd.DataFrame([
                {
                    "Category": a.category.replace("_", " ").title(),
                    "Capital": a.capital_allocated,
                    "Listings": a.num_listings,
                    "Monthly Profit": a.expected_monthly_profit,
                    "Efficiency": a.efficiency,
                }
                for a in plan.allocations
            ])

            st.subheader("Allocation by Category")
            tab1, tab2 = st.tabs(["Capital", "Expected Profit"])
            with tab1:
                st.bar_chart(alloc_data.set_index("Category")["Capital"])
            with tab2:
                st.bar_chart(alloc_data.set_index("Category")["Monthly Profit"])

            st.dataframe(alloc_data, use_container_width=True, hide_index=True)

        # Recommendations
        if plan.recommendations:
            st.subheader("💡 Recommendations")
            for rec in plan.recommendations:
                st.info(rec)

        if plan.warnings:
            st.subheader("⚠️ Warnings")
            for warn in plan.warnings:
                st.warning(warn)


def render_market_data():
    """Render market data explorer."""
    st.title("📉 Market Data")

    st.subheader("Category Price Ranges")

    cat_data = []
    for cat_key, cat_info in CATEGORY_TAXONOMY.items():
        cat_data.append({
            "Category": cat_key.replace("_", " ").title(),
            "eBay Category ID": cat_info.get("ebay_category_id", "N/A"),
            "Avg Weight (kg)": cat_info.get("avg_weight_kg", 0),
            "Avg Shipping ($)": cat_info.get("avg_shipping_usd", 0),
            "Demand Score": cat_info.get("demand_score", 0),
            "Competition": cat_info.get("competition_score", 0),
            "Typical Margin": f"{cat_info.get('typical_margin_pct', 0):.0f}%",
            "Price Range": f"${cat_info.get('price_range_usd', (0, 0))[0]}-${cat_info.get('price_range_usd', (0, 0))[1]}",
        })

    st.dataframe(pd.DataFrame(cat_data), use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("Shipping Cost Estimates (Japan → US)")
    shipping_df = pd.DataFrame(SHIPPING_TIERS[:-1], columns=[
        "Max Weight (kg)", "ePacket ($)", "DHL Express ($)", "FedEx ($)", "EMS ($)"
    ])
    st.dataframe(shipping_df, use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("eBay Fee Structure by Category")
    fee_data = [
        {"Category": k.replace("_", " ").title(), "Final Value Fee": f"{v:.0%}"}
        for k, v in EBAY_FEES["final_value_fee"].items()
    ]
    fee_data.append({
        "Category": "Payment Processing",
        "Final Value Fee": f"{EBAY_FEES['payment_processing_pct']:.1%} + ${EBAY_FEES['payment_processing_fixed']:.2f}",
    })
    st.dataframe(pd.DataFrame(fee_data), use_container_width=True, hide_index=True)


def render_sync_status(logs):
    """Render sync status and logs."""
    st.title("🔄 Sync Status")

    if not logs:
        st.info("No sync logs yet")
        return

    # Summary
    latest = logs[0]
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Last Sync", latest.created_at.strftime("%H:%M:%S"))
    with c2:
        st.metric("Items Checked", latest.items_checked)
    with c3:
        st.metric("Delisted", latest.items_delisted)
    with c4:
        st.metric("Updated", latest.items_updated)
    with c5:
        st.metric("Errors", latest.errors)

    # History chart
    st.subheader("Sync History")
    log_data = pd.DataFrame([
        {
            "Time": log.created_at.strftime("%m-%d %H:%M"),
            "Checked": log.items_checked,
            "Delisted": log.items_delisted,
            "Errors": log.errors,
            "Duration (s)": log.duration_seconds,
        }
        for log in reversed(logs)
    ])
    st.line_chart(log_data.set_index("Time")[["Checked", "Delisted", "Errors"]])

    # Error details
    error_logs = [l for l in logs if l.errors > 0]
    if error_logs:
        st.subheader("Recent Errors")
        for log in error_logs[:5]:
            with st.expander(f"{log.created_at.strftime('%Y-%m-%d %H:%M')} — {log.errors} errors"):
                st.text(log.error_details or "No details")


def render_settings():
    """Render settings page."""
    st.title("⚙️ Settings")

    st.subheader("Current Configuration")

    config_data = {
        "Setting": [
            "Database URL",
            "LLM Provider",
            "LLM Model",
            "eBay Sandbox",
            "Scrape Interval (min)",
            "Min Margin %",
            "Max Source Price ($)",
            "Markup Multiplier",
            "Default Shipping ($)",
            "Proxy Service Fee ($)",
            "CF Bypass URL",
        ],
        "Value": [
            settings.database_url,
            settings.llm_provider,
            settings.anthropic_model if settings.llm_provider == "anthropic" else settings.openai_model,
            str(settings.ebay_sandbox),
            str(settings.scrape_interval_minutes),
            f"{settings.min_margin_percent:.0f}%",
            f"${settings.max_source_price_usd:.0f}",
            f"{settings.markup_multiplier:.1f}x",
            f"${settings.default_shipping_cost:.0f}",
            f"${settings.proxy_service_fee:.0f}",
            settings.cf_bypass_url or "Not configured",
        ],
    }
    st.dataframe(pd.DataFrame(config_data), use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("Platform Connection Status")

    # Check connections
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**Mercari JP**")
        st.info("API: Not tested" if not settings.proxy_url else f"Proxy: {settings.proxy_url}")

    with col2:
        st.markdown("**eBay API**")
        if settings.ebay_app_id:
            st.success(f"App ID: {settings.ebay_app_id[:8]}...")
        else:
            st.warning("Not configured")

    with col3:
        st.markdown("**Telegram**")
        if settings.telegram_bot_token:
            st.success("Bot configured")
        else:
            st.warning("Not configured")


# --- Main ---

def main():
    """Main dashboard entry point."""

    # Sidebar navigation
    st.sidebar.title("🔄 Arbitrage Engine")
    st.sidebar.divider()

    page = st.sidebar.radio(
        "Navigation",
        [
            "📊 Portfolio",
            "🔍 Discovery",
            "💰 Pricing",
            "📈 Capital",
            "📉 Market Data",
            "🔄 Sync",
            "⚙️ Settings",
        ],
    )

    st.sidebar.divider()
    st.sidebar.markdown(f"**LLM:** {settings.llm_provider}")
    st.sidebar.markdown(f"**eBay:** {'Sandbox' if settings.ebay_sandbox else 'Production'}")
    st.sidebar.markdown(f"**Interval:** {settings.scrape_interval_minutes}min")

    # Page routing
    engine = get_engine()
    factory = get_session_factory()

    if page == "📊 Portfolio":
        # Run async data fetch
        async def load_portfolio():
            async with factory() as session:
                summary = await fetch_portfolio_summary(session)
                listings = await fetch_active_listings(session)
                return summary, listings

        summary, listings = asyncio.run(load_portfolio())
        render_portfolio_overview(summary)
        st.divider()
        render_active_listings(listings)

    elif page == "🔍 Discovery":
        async def load_candidates():
            async with factory() as session:
                return await fetch_discovery_candidates(session)

        candidates = asyncio.run(load_candidates())
        render_discovery_results(candidates)

    elif page == "💰 Pricing":
        render_pricing_analysis()

    elif page == "📈 Capital":
        render_capital_allocation()

    elif page == "📉 Market Data":
        render_market_data()

    elif page == "🔄 Sync":
        async def load_sync():
            async with factory() as session:
                return await fetch_sync_logs(session)

        logs = asyncio.run(load_sync())
        render_sync_status(logs)

    elif page == "⚙️ Settings":
        render_settings()


if __name__ == "__main__":
    main()
