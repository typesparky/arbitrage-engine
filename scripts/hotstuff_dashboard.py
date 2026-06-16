#!/usr/bin/env python3.12
"""Streamlit dashboard for Hotstuff perp DEX arbitrage opportunities.

Run:
    streamlit run scripts/hotstuff_dashboard.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.processors.funding_analyzer import FundingAnalyzer
from src.processors.hotstuff_arb import HotstuffArbScanner
from src.scrapers.hotstuff import HotstuffClient


@st.cache_resource
def get_scanner(volume_14d, maker_share, min_div, min_score, categories):
    """Create scanner (cached)."""
    scanner = HotstuffArbScanner(
        volume_14d=volume_14d,
        maker_share_pct=maker_share,
        min_divergence_bps=min_div,
        min_score=min_score,
        categories=categories,
    )
    return scanner


async def _run_scan(scanner):
    await scanner.initialize()
    return await scanner.scan()


async def _run_funding():
    client = HotstuffClient()
    await client.initialize()
    tickers = await client.get_tickers("all")
    analyzer = FundingAnalyzer(client)
    analyses = await analyzer.analyze_all(tickers)
    await client.close()
    return analyses


st.set_page_config(
    page_title="Hotstuff Arb Dashboard",
    page_icon="🔥",
    layout="wide",
)

st.title("🔥 Hotstuff Perp DEX Arbitrage Scanner")

# ── Sidebar ──────────────────────────────────────────────────────────
st.sidebar.header("Scanner Settings")

volume_14d = st.sidebar.number_input(
    "14d Volume (USD)", min_value=0, value=0, step=100_000,
    help="Your 14-day rolling volume for fee tier determination"
)
maker_share = st.sidebar.slider(
    "Maker Share %", min_value=0.0, max_value=20.0, value=0.0, step=0.5,
    help="Your % of maker volume for rebate tier"
)
min_div = st.sidebar.number_input(
    "Min Divergence (bps)", min_value=1.0, value=5.0, step=1.0
)
min_score = st.sidebar.slider(
    "Min Score", min_value=0, max_value=100, value=20
)
categories = st.sidebar.multiselect(
    "Categories",
    options=["rwa_stock", "commodity", "crypto", "forex", "index"],
    default=["rwa_stock", "commodity", "crypto"],
)

auto_refresh = st.sidebar.checkbox("Auto-refresh (60s)", value=False)

# ── Main content ──────────────────────────────────────────────────────

col1, col2, col3 = st.columns(3)

if col1.button("🔄 Scan Now", type="primary"):
    with st.spinner("Scanning Hotstuff perps..."):
        scanner = get_scanner(volume_14d, maker_share / 100, min_div, min_score, categories or None)
        opportunities = asyncio.run(_run_scan(scanner))

        with st.spinner("Analyzing funding rates..."):
            funding = asyncio.run(_run_funding())

    st.session_state["arb_opportunities"] = opportunities
    st.session_state["funding_analyses"] = funding
    st.session_state["last_scan"] = time.time()

if "arb_opportunities" not in st.session_state:
    st.info("Click 'Scan Now' to start scanning for arbitrage opportunities.")
    st.stop()

opportunities = st.session_state["arb_opportunities"]
funding = st.session_state.get("funding_analyses", [])
last_scan = st.session_state.get("last_scan", 0)

# Auto-refresh
if auto_refresh:
    elapsed = time.time() - last_scan
    if elapsed > 60:
        st.rerun()
    else:
        st.caption(f"Next refresh in {60 - elapsed:.0f}s")

# ── Summary metrics ───────────────────────────────────────────────────
profitable = [o for o in opportunities if o.fee_analysis and o.fee_analysis.is_profitable]
extreme_funding = [f for f in funding if f.is_extreme]

col1, col2, col3, col4 = st.columns(4)
col1.metric("Opportunities", len(opportunities))
col2.metric("Profitable", len(profitable))
col3.metric("Extreme Funding", len(extreme_funding))
col4.metric("Last Scan", f"{time.time() - last_scan:.0f}s ago")

# ── Arbitrage opportunities table ──────────────────────────────────────
st.subheader("📊 Cross-Venue Arbitrage Opportunities")

if opportunities:
    rows = []
    for opp in opportunities:
        fa = opp.fee_analysis
        rows.append({
            "Symbol": opp.symbol,
            "Category": opp.category,
            "Score": round(opp.score, 0),
            "Hotstuff Mark": f"{opp.hotstuff_mark:.2f}",
            "Reference": f"{opp.reference_price:.2f}",
            "Ref Venue": opp.reference_venue,
            "Divergence (bps)": f"{opp.divergence_bps:+.1f}",
            "Direction": "SHORT hs" if opp.direction == "short_hotstuff" else "LONG hs",
            "Net Edge (bps)": f"{fa.net_edge_bps:+.1f}" if fa else "N/A",
            "Profit/$1k": f"${opp.net_profit_per_1k:.2f}" if fa else "N/A",
            "Profitable": "✅" if fa and fa.is_profitable else "❌",
            "Funding 8h": f"{opp.funding_8h:+.6f}",
            "Spread %": f"{opp.spread_bps:.4f}",
            "Vol 24h": f"${opp.volume_24h:,.0f}",
        })

    df = pd.DataFrame(rows)

    def color_profitable(val):
        return "background-color: #1a472a" if val == "✅" else "background-color: #4a1a1a"

    styled = df.style.map(color_profitable, subset=["Profitable"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── Top opportunities detail ────────────────────────────────────────
    st.subheader("🏆 Top Opportunities")
    for opp in profitable[:3]:
        with st.expander(f"{opp.symbol} — Score {opp.score:.0f} — ${opp.net_profit_per_1k:.2f}/$1k"):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Hotstuff Price**: {opp.hotstuff_mark:.2f}")
                st.markdown(f"**Bid/Ask**: {opp.hotstuff_bid:.2f} / {opp.hotstuff_ask:.2f}")
                st.markdown(f"**Index Price**: {opp.hotstuff_index:.2f}")
                st.markdown(f"**Reference ({opp.reference_venue})**: {opp.reference_price:.2f}")
            with c2:
                st.markdown(f"**Divergence**: {opp.divergence_bps:+.1f} bps")
                st.markdown(f"**Direction**: {opp.direction}")
                if opp.fee_analysis:
                    st.markdown(f"**Net Edge**: {opp.fee_analysis.net_edge_bps:+.1f} bps")
                    st.markdown(f"**Total Fees**: ${opp.fee_analysis.total_fees_usd:.4f}")
                st.markdown(f"**Funding 8h**: {opp.funding_8h:+.6f}")
            if opp.notes:
                st.warning(" | ".join(opp.notes))
else:
    st.info("No arbitrage opportunities found matching your criteria.")

# ── Funding rates ──────────────────────────────────────────────────────
st.subheader("💰 Funding Rate Analysis")

if funding:
    fund_rows = []
    for f in funding[:20]:
        fund_rows.append({
            "Symbol": f.symbol,
            "Funding 8h": f"{f.funding_8h:+.6f}",
            "Hourly": f"{f.funding_hourly:+.8f}",
            "APR %": f"{f.funding_annualized_pct:+.1f}",
            "Premium %": f"{f.premium_pct:+.4f}",
            "Capture/$10k/h": f"${f.hourly_capture_per_10k:.2f}",
            "Capture/$10k/d": f"${f.daily_capture_per_10k:.2f}",
            "Extreme": "🔥" if f.is_extreme else "",
            "Score": round(f.score, 0),
        })

    fund_df = pd.DataFrame(fund_rows)
    st.dataframe(fund_df, use_container_width=True, hide_index=True)
else:
    st.info("No funding data available.")

# ── Fee tier info ──────────────────────────────────────────────────────
st.subheader("📋 Fee Tier Information")
tier_info = {
    "Tier": ["Standard", "VIP 1", "VIP 2", "VIP 3", "VIP 4", "VIP 5", "VIP 6"],
    "14d Volume": ["$0-1M", ">$1M", ">$5M", ">$20M", ">$100M", ">$250M", ">$500M"],
    "Taker Fee": ["0.025%", "0.024%", "0.023%", "0.022%", "0.021%", "0.020%", "0.020%"],
    "Maker Fee": ["-0.002%", "-0.0025%", "-0.003%", "-0.0035%", "-0.005%", "-0.007%", "-0.014%"],
}
st.dataframe(pd.DataFrame(tier_info), use_container_width=True, hide_index=True)

st.caption("Maker rebate tiers (additional): >2.5% share → -0.010% | >5% → -0.011% | >7.5% → -0.012% | >10% → -0.013%")
st.caption("Final maker fee = min(rebate_tier, maker_fee_tier_rate)")
