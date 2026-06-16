#!/usr/bin/env python3
"""Real-time DEX spread dashboard.

Shows:
  1. Price matrix: coins x venues with live prices
  2. Composite baseline (volume-weighted average across venues)
  3. Divergence from composite per venue (color-coded)
  4. Spread comparison (bid-ask % per venue)
  5. Funding rate comparison
  6. Orderbook depth comparison

Uses Rich for live-updating terminal UI.

Usage:
    python scripts/dex_spread_dashboard.py
    python scripts/dex_spread_dashboard.py --refresh 5
    python scripts/dex_spread_dashboard.py --coins BTC ETH SOL
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from src.processors.divergence_engine import DivergenceEngine, VenueSnapshot


# ── Instrument universe ──────────────────────────────────────────────
# Map: hotstuff_symbol -> {venue: venue_symbol}
COINS = [
    {"hs": "BTC-PERP",  "hyperliquid": "BTC",  "binance": "BTCUSDT"},
    {"hs": "ETH-PERP",  "hyperliquid": "ETH",  "binance": "ETHUSDT"},
    {"hs": "SOL-PERP",  "hyperliquid": "SOL",  "binance": "SOLUSDT"},
    {"hs": "HYPE-PERP", "hyperliquid": "HYPE", "binance": "HYPEUSDT"},
    {"hs": "XRP-PERP",  "hyperliquid": "XRP",  "binance": "XRPUSDT"},
    {"hs": "BNB-PERP",  "hyperliquid": "BNB",  "binance": "BNBUSDT"},
    {"hs": "DOGE-PERP", "hyperliquid": "DOGE", "binance": "DOGEUSDT"},
    {"hs": "ADA-PERP",  "hyperliquid": "ADA",  "binance": "ADAUSDT"},
    {"hs": "LINK-PERP", "hyperliquid": "LINK", "binance": "LINKUSDT"},
    {"hs": "NEAR-PERP", "hyperliquid": "NEAR", "binance": "NEARUSDT"},
    {"hs": "AVAX-PERP", "hyperliquid": "AVAX", "binance": "AVAXUSDT"},
    {"hs": "ARB-PERP",  "hyperliquid": "ARB",  "binance": "ARBUSDT"},
    {"hs": "OP-PERP",   "hyperliquid": "OP",   "binance": "OPUSDT"},
    {"hs": "LTC-PERP",  "hyperliquid": "LTC",  "binance": "LTCUSDT"},
    {"hs": "ATOM-PERP", "hyperliquid": "ATOM", "binance": "ATOMUSDT"},
    {"hs": "DOT-PERP",  "hyperliquid": "DOT",  "binance": "DOTUSDT"},
    {"hs": "UNI-PERP",  "hyperliquid": "UNI",  "binance": "UNIUSDT"},
    {"hs": "AAVE-PERP", "hyperliquid": "AAVE", "binance": "AAVEUSDT"},
    {"hs": "MKR-PERP",  "hyperliquid": "MKR",  "binance": "MKRUSDT"},
    {"hs": "ZEC-PERP",  "hyperliquid": "ZEC",  "binance": "ZECUSDT"},
    {"hs": "X-PERP",    "hyperliquid": "X",    "binance": "XUSDT"},
]

VENUE_ORDER = ["hotstuff", "hyperliquid", "binance"]


# ── Helpers ──────────────────────────────────────────────────────────

def fmt_price(p: float, symbol: str) -> str:
    """Format price with appropriate precision."""
    if p <= 0:
        return "N/A"
    if p > 10_000:
        return f"{p:,.0f}"
    elif p > 100:
        return f"{p:,.2f}"
    elif p > 1:
        return f"{p:,.4f}"
    else:
        return f"{p:,.6f}"


def fmt_bps(bps: float) -> str:
    """Format basis points with color hint."""
    if abs(bps) < 1:
        return f"{bps:+.2f}"
    return f"{bps:+.1f}"


def color_for_div(bps: float) -> str:
    """Color tag for divergence magnitude."""
    abs_bps = abs(bps)
    if abs_bps > 20:
        return "bold bright_green" if bps > 0 else "bold bright_red"
    elif abs_bps > 10:
        return "green" if bps > 0 else "red"
    elif abs_bps > 5:
        return "yellow"
    else:
        return "dim"


def color_for_spread(bps: float) -> str:
    """Color for spread — lower is better."""
    if bps < 0.5:
        return "bright_green"
    elif bps < 2:
        return "green"
    elif bps < 5:
        return "yellow"
    elif bps < 10:
        return "orange3"
    else:
        return "bold red"


def color_for_funding(fund: float) -> str:
    """Color for funding rate."""
    if fund > 0.0001:
        return "green"
    elif fund < -0.0001:
        return "red"
    else:
        return "dim"


def compute_composite(snapshots: dict[str, VenueSnapshot]) -> float:
    """Compute volume-weighted composite price.

    Weights: 70% volume, 30% open interest.
    Falls back to simple average if no volume/OI data.
    """
    total_weight = 0
    weighted_sum = 0
    has_data = False

    for venue, snap in snapshots.items():
        if snap.mark_price <= 0:
            continue
        # Weight: volume_24h * 0.7 + OI * mark_price * 0.3
        vol = snap.volume_24h
        oi_usd = snap.open_interest * snap.mark_price
        weight = vol * 0.7 + oi_usd * 0.3
        if weight <= 0:
            weight = 1  # Equal weight fallback
        has_data = True
        weighted_sum += snap.mark_price * weight
        total_weight += weight

    if not has_data or total_weight == 0:
        # Simple average fallback
        prices = [s.mark_price for s in snapshots.values() if s.mark_price > 0]
        return sum(prices) / len(prices) if prices else 0

    return weighted_sum / total_weight


# ── Dashboard renderer ──────────────────────────────────────────────

class Dashboard:
    """Rich-based live dashboard."""

    def __init__(self, coins: list[dict], venues: list[str]):
        self.coins = coins
        self.venues = venues
        self.engine = DivergenceEngine()
        self.last_update = 0
        self.fetch_count = 0
        self.errors: list[str] = []

    async def fetch_all(self) -> dict[str, dict[str, VenueSnapshot]]:
        """Fetch snapshots for all coins from all venues."""
        all_snapshots: dict[str, dict[str, VenueSnapshot]] = defaultdict(dict)

        tasks = []
        task_meta = []

        for coin in self.coins:
            hs_symbol = coin["hs"]
            # Hotstuff
            tasks.append(self.engine.get_hotstuff_snapshot(hs_symbol, 0))
            task_meta.append((hs_symbol, "hotstuff"))

            # Hyperliquid
            if "hyperliquid" in coin:
                tasks.append(self.engine.get_hyperliquid_snapshot(coin["hyperliquid"]))
                task_meta.append((hs_symbol, "hyperliquid"))

            # Binance
            if "binance" in coin:
                tasks.append(self.engine.get_binance_snapshot(coin["binance"]))
                task_meta.append((hs_symbol, "binance"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for meta, result in zip(task_meta, results):
            hs_symbol, venue = meta
            if isinstance(result, Exception):
                self.errors.append(f"{hs_symbol}/{venue}: {result}")
                continue
            if result is not None:
                all_snapshots[hs_symbol][venue] = result

        self.last_update = time.time()
        self.fetch_count += 1
        return all_snapshots

    def build_price_table(self, all_snapshots: dict[str, dict[str, VenueSnapshot]]) -> Table:
        """Table 1: Price matrix with composite baseline and divergences."""
        table = Table(
            title="💰 Price Matrix & Divergence from Composite",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )

        # Columns: Coin | Composite | Venue1 Price | Venue1 Div | Venue2 Price | Venue2 Div | ...
        table.add_column("Coin", style="bold white", width=12, no_wrap=True)
        table.add_column("Composite", justify="right", width=14, style="bold bright_white")

        for venue in self.venues:
            if venue == "hotstuff":
                name = "Hotstuff"
            elif venue == "hyperliquid":
                name = "Hyperliquid"
            elif venue == "binance":
                name = "Binance"
            else:
                name = venue
            table.add_column(f"{name}", justify="right", width=14)
            table.add_column("Div", justify="right", width=10)

        for coin in self.coins:
            hs_symbol = coin["hs"]
            snapshots = all_snapshots.get(hs_symbol, {})
            if not snapshots:
                continue

            composite = compute_composite(snapshots)
            if composite <= 0:
                continue

            row = [hs_symbol, fmt_price(composite, hs_symbol)]

            for venue in self.venues:
                snap = snapshots.get(venue)
                if snap and snap.mark_price > 0:
                    div_bps = (snap.mark_price - composite) / composite * 10_000
                    price_str = fmt_price(snap.mark_price, hs_symbol)
                    div_str = fmt_bps(div_bps)
                    div_color = color_for_div(div_bps)
                    row.append(price_str)
                    row.append(f"[{div_color}]{div_str}[/]")
                else:
                    row.append("N/A")
                    row.append("N/A")

            table.add_row(*row)

        return table

    def build_spread_table(self, all_snapshots: dict[str, dict[str, VenueSnapshot]]) -> Table:
        """Table 2: Spread comparison."""
        table = Table(
            title="📊 Bid-Ask Spread (%)",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("Coin", style="bold white", width=12, no_wrap=True)

        for venue in self.venues:
            if venue == "hotstuff":
                name = "Hotstuff"
            elif venue == "hyperliquid":
                name = "Hyperliquid"
            elif venue == "binance":
                name = "Binance"
            else:
                name = venue
            table.add_column(f"{name}", justify="right", width=14)
            table.add_column("Bid$", justify="right", width=12)
            table.add_column("Ask$", justify="right", width=12)

        for coin in self.coins:
            hs_symbol = coin["hs"]
            snapshots = all_snapshots.get(hs_symbol, {})
            if not snapshots:
                continue

            row = [hs_symbol]

            for venue in self.venues:
                snap = snapshots.get(venue)
                if snap and snap.orderbook:
                    spread = snap.orderbook.spread_bps / 100  # Convert bps to %
                    spread_color = color_for_spread(snap.orderbook.spread_bps)
                    bid_depth = snap.orderbook.bid_depth_usd
                    ask_depth = snap.orderbook.ask_depth_usd
                    row.append(f"[{spread_color}]{spread:.4f}%[/]")
                    row.append(f"{bid_depth/1000:.0f}k")
                    row.append(f"{ask_depth/1000:.0f}k")
                else:
                    row.append("N/A")
                    row.append("N/A")
                    row.append("N/A")

            table.add_row(*row)

        return table

    def build_funding_table(self, all_snapshots: dict[str, dict[str, VenueSnapshot]]) -> Table:
        """Table 3: Funding rates."""
        table = Table(
            title="💰 Funding Rates (8h) & Annualized",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("Coin", style="bold white", width=12, no_wrap=True)

        for venue in self.venues:
            if venue == "hotstuff":
                name = "Hotstuff"
            elif venue == "hyperliquid":
                name = "Hyperliquid"
            elif venue == "binance":
                name = "Binance"
            else:
                name = venue
            table.add_column(f"{name} 8h", justify="right", width=14)
            table.add_column("APR", justify="right", width=12)

        for coin in self.coins:
            hs_symbol = coin["hs"]
            snapshots = all_snapshots.get(hs_symbol, {})
            if not snapshots:
                continue

            row = [hs_symbol]

            for venue in self.venues:
                snap = snapshots.get(venue)
                if snap:
                    fund = snap.funding_8h
                    apr = snap.funding_annualized_pct
                    fund_color = color_for_funding(fund)
                    row.append(f"[{fund_color}]{fund:+.6f}[/]")
                    row.append(f"[{fund_color}]{apr:+.1f}%[/]")
                else:
                    row.append("N/A")
                    row.append("N/A")

            table.add_row(*row)

        return table

    def build_depth_table(self, all_snapshots: dict[str, dict[str, VenueSnapshot]]) -> Table:
        """Table 4: Orderbook depth comparison."""
        table = Table(
            title="📚 Orderbook Depth (USD)",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("Coin", style="bold white", width=12, no_wrap=True)

        for venue in self.venues:
            if venue == "hotstuff":
                name = "Hotstuff"
            elif venue == "hyperliquid":
                name = "Hyperliquid"
            elif venue == "binance":
                name = "Binance"
            else:
                name = venue
            table.add_column(f"{name}", justify="right", width=16)

        for coin in self.coins:
            hs_symbol = coin["hs"]
            snapshots = all_snapshots.get(hs_symbol, {})
            if not snapshots:
                continue

            row = [hs_symbol]

            for venue in self.venues:
                snap = snapshots.get(venue)
                if snap and snap.orderbook:
                    bid_d = snap.orderbook.bid_depth_usd
                    ask_d = snap.orderbook.ask_depth_usd
                    total = bid_d + ask_d
                    if total > 1_000_000:
                        row.append(f"${total/1_000_000:.1f}M")
                    elif total > 1_000:
                        row.append(f"${total/1_000:.0f}k")
                    else:
                        row.append(f"${total:.0f}")
                else:
                    row.append("N/A")

            table.add_row(*row)

        return table

    def build_opportunities_table(self, all_snapshots: dict[str, dict[str, VenueSnapshot]]) -> Table:
        """Table 5: Top arbitrage opportunities."""
        table = Table(
            title="🔥 Top Arbitrage Opportunities",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("Coin", style="bold white", width=12)
        table.add_column("Buy @", width=12)
        table.add_column("Sell @", width=12)
        table.add_column("Gross", justify="right", width=10)
        table.add_column("Net", justify="right", width=10)
        table.add_column("Score", justify="right", width=8)

        # Compute all pairwise divergences
        opportunities = []
        for coin in self.coins:
            hs_symbol = coin["hs"]
            snapshots = all_snapshots.get(hs_symbol, {})
            if len(snapshots) < 2:
                continue

            composite = compute_composite(snapshots)
            if composite <= 0:
                continue

            # Find best buy/sell pair
            best_buy_venue = None
            best_sell_venue = None
            best_buy_price = float("inf")
            best_sell_price = 0

            for venue, snap in snapshots.items():
                if snap.mark_price <= 0:
                    continue
                if snap.mark_price < best_buy_price:
                    best_buy_price = snap.mark_price
                    best_buy_venue = venue
                if snap.mark_price > best_sell_price:
                    best_sell_price = snap.mark_price
                    best_sell_venue = venue

            if best_buy_venue and best_sell_venue and best_buy_venue != best_sell_venue:
                gross = (best_sell_price - best_buy_price) / composite * 10_000
                # Estimate fees
                fees = 0.5  # Conservative 0.5 bps total
                net = gross - fees
                score = min(100, max(0, net * 5))

                opportunities.append({
                    "symbol": hs_symbol,
                    "buy_venue": best_buy_venue,
                    "sell_venue": best_sell_venue,
                    "gross": gross,
                    "net": net,
                    "score": score,
                })

        opportunities.sort(key=lambda x: x["score"], reverse=True)

        for opp in opportunities[:10]:
            net_color = "bright_green" if opp["net"] > 5 else "green" if opp["net"] > 2 else "yellow" if opp["net"] > 0 else "red"
            table.add_row(
                opp["symbol"],
                opp["buy_venue"],
                opp["sell_venue"],
                f"{opp['gross']:.1f}",
                f"[{net_color}]{opp['net']:.1f}[/]",
                f"{opp['score']:.0f}",
            )

        return table

    def build_status_bar(self) -> Text:
        """Status bar at bottom."""
        elapsed = time.time() - self.last_update if self.last_update > 0 else 0
        status = Text()
        status.append(f"  Last update: {elapsed:.0f}s ago  ", style="dim")
        status.append(f"Fetches: {self.fetch_count}  ", style="dim")
        if self.errors:
            status.append(f"Errors: {len(self.errors)}  ", style="red")
        status.append("Press Ctrl+C to exit", style="dim")
        return status

    def build_summary_table(self, all_snapshots: dict[str, dict[str, VenueSnapshot]]) -> Table:
        """Aggregate market summary: total volume, avg spread, hottest funding."""
        table = Table(
            title="📈 Market Summary",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("Metric", style="bold white", width=20)
        for venue in self.venues:
            name = venue.capitalize() if venue != "hotstuff" else "Hotstuff"
            table.add_column(name, justify="right", width=16)

        # Total 24h volume
        vol_row = ["24h Volume"]
        spread_row = ["Avg Spread (bps)"]
        funding_row = ["Avg Funding (8h)"]
        depth_row = ["Total Depth"]
        coins_row = ["Active Coins"]

        for venue in self.venues:
            total_vol = 0
            spreads = []
            fundings = []
            total_depth = 0
            active = 0

            for coin in self.coins:
                hs_symbol = coin["hs"]
                snap = all_snapshots.get(hs_symbol, {}).get(venue)
                if snap:
                    active += 1
                    total_vol += snap.volume_24h
                    if snap.orderbook:
                        spreads.append(snap.orderbook.spread_bps)
                        total_depth += snap.orderbook.bid_depth_usd + snap.orderbook.ask_depth_usd
                    fundings.append(snap.funding_8h)

            if total_vol > 1e6:
                vol_row.append(f"${total_vol/1e6:.0f}M")
            else:
                vol_row.append(f"${total_vol/1e3:.0f}k")

            avg_spread = sum(spreads) / len(spreads) if spreads else 0
            spread_color = color_for_spread(avg_spread)
            spread_row.append(f"[{spread_color}]{avg_spread:.2f}[/]")

            avg_fund = sum(fundings) / len(fundings) if fundings else 0
            fund_color = color_for_funding(avg_fund)
            funding_row.append(f"[{fund_color}]{avg_fund:+.6f}[/]")

            if total_depth > 1e6:
                depth_row.append(f"${total_depth/1e6:.1f}M")
            else:
                depth_row.append(f"${total_depth/1e3:.0f}k")

            coins_row.append(str(active))

        table.add_row(*vol_row)
        table.add_row(*spread_row)
        table.add_row(*funding_row)
        table.add_row(*depth_row)
        table.add_row(*coins_row)

        return table

    async def build_dashboard(self) -> Panel:
        """Build the full dashboard layout."""
        all_snapshots = await self.fetch_all()

        from rich.columns import Columns
        from rich.console import Group
        price_table = self.build_price_table(all_snapshots)
        spread_table = self.build_spread_table(all_snapshots)
        funding_table = self.build_funding_table(all_snapshots)
        depth_table = self.build_depth_table(all_snapshots)
        opp_table = self.build_opportunities_table(all_snapshots)

        # Layout: summary + price matrix on top, then spreads + funding, then depth + opportunities
        summary_table = self.build_summary_table(all_snapshots)
        price_table = self.build_price_table(all_snapshots)
        spread_table = self.build_spread_table(all_snapshots)
        funding_table = self.build_funding_table(all_snapshots)
        depth_table = self.build_depth_table(all_snapshots)
        opp_table = self.build_opportunities_table(all_snapshots)

        top_section = Group(summary_table, "", price_table)
        middle = Columns([spread_table, funding_table], equal=False)
        bottom = Columns([depth_table, opp_table], equal=False)

        group = Group(
            top_section,
            "",
            middle,
            "",
            bottom,
            "",
            self.build_status_bar(),
        )

        return Panel(
            group,
            title=f"[bold white]🔥 DEX Spread Dashboard — {time.strftime('%H:%M:%S UTC', time.gmtime())}[/]",
            border_style="bright_blue",
            padding=(1, 2),
        )


async def main(args):
    dashboard = Dashboard(COINS, VENUE_ORDER)

    refresh = args.refresh

    if args.once:
        # Single snapshot, print to stdout
        panel = await dashboard.build_dashboard()
        console = Console()
        console.print(panel)
        await dashboard.engine.close()
        return

    # Live updating
    console = Console()

    try:
        with Live(
            await dashboard.build_dashboard(),
            console=console,
            refresh_per_second=1 / max(0.5, refresh),
            screen=True,
        ) as live:
            while True:
                await asyncio.sleep(refresh)
                live.update(await dashboard.build_dashboard())
    except KeyboardInterrupt:
        pass
    finally:
        await dashboard.engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DEX Spread Dashboard")
    parser.add_argument("--refresh", type=float, default=5.0, help="Refresh interval in seconds (default: 5)")
    parser.add_argument("--once", action="store_true", help="Single snapshot, no live update")
    parser.add_argument("--coins", nargs="+", default=None, help="Filter to specific coins (e.g., BTC ETH SOL)")

    args = parser.parse_args()

    if args.coins:
        COINS[:] = [c for c in COINS if c["hs"].replace("-PERP", "") in args.coins]

    asyncio.run(main(args))
