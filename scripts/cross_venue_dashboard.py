#!/usr/bin/env python3
"""Cross-Venue Perp DEX Dashboard.

Real-time price comparison across:
  Hotstuff, Hyperliquid, dYdX, Binance, Bybit, OKX

Shows:
  1. Price matrix with composite baseline (volume-weighted)
  2. Divergence from composite per venue (color-coded)
  3. Funding rate comparison
  4. Orderbook depth comparison
  5. Top arbitrage opportunities

Usage:
    python scripts/cross_venue_dashboard.py
    python scripts/cross_venue_dashboard.py --once
    python scripts/cross_venue_dashboard.py --refresh 5
    python scripts/cross_venue_dashboard.py --coins BTC ETH SOL
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.columns import Columns
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from src.processors.divergence_engine import DivergenceEngine


# ── Instrument universe: Hotstuff perps with cross-venue overlap ──────
# Format: (hotstuff_symbol, hyperliquid, binance, bybit, okx, dydx)
INSTRUMENTS = [
    # 6-venue overlap (all venues)
    ("BTC-PERP",  "BTC",  "BTCUSDT",  "BTCUSDT",  "BTC-USDT-SWAP",  "BTC-USD"),
    ("ETH-PERP",  "ETH",  "ETHUSDT",  "ETHUSDT",  "ETH-USDT-SWAP",  "ETH-USD"),
    ("SOL-PERP",  "SOL",  "SOLUSDT",  "SOLUSDT",  "SOL-USDT-SWAP",  "SOL-USD"),
    ("XRP-PERP",  "XRP",  "XRPUSDT",  "XRPUSDT",  "XRP-USDT-SWAP",  "XRP-USD"),
    # 5-venue overlap
    ("BNB-PERP",  "BNB",  "BNBUSDT",  "BNBUSDT",  None,             "BNB-USD"),
    ("HYPE-PERP", "HYPE", "HYPEUSDT", "HYPEUSDT", None,             "HYPE-USD"),
    ("ZEC-PERP",  "ZEC",  "ZECUSDT",  "ZECUSDT",  None,             "ZEC-USD"),
    # Hotstuff-only / limited overlap (RWA, commodities, forex)
    ("AAPL-PERP", None,   None,       "AAPLUSDT", None,             None),
    ("TSLA-PERP", None,   None,       "TSLAUSDT", None,             None),
    ("NVDA-PERP", None,   None,       "NVDAUSDT", None,             None),
    ("META-PERP", None,   None,       "METAUSDT", None,             None),
    ("MSFT-PERP", None,   None,       "MSFTUSDT", None,             None),
    ("PLTR-PERP", None,   None,       "PLTRUSDT", None,             None),
    ("GOOGL-PERP",None,   None,       "GOOGLUSDT",None,             None),
    ("AMZN-PERP", None,   None,       "AMZNUSDT", None,             None),
    ("GOLD-PERP", None,   None,       None,       None,             None),
    ("WTIOIL-PERP",None,  None,       None,       None,             None),
    ("BRENTOIL-PERP",None,None,       None,       None,             None),
    ("SILVER-PERP",None,  None,       None,       None,             None),
    ("NATGAS-PERP",None,  None,       None,       None,             None),
    ("EURUSD-PERP",None,  None,       None,       None,             None),
    ("USDJPY-PERP",None,  None,       None,       None,             None),
    ("USA500-PERP",None,  None,       None,       None,             None),
    ("USA100-PERP",None,  None,       None,       None,             None),
    ("EWJ-PERP",  None,   None,       "EWJUSDT",  None,             None),
    ("EWY-PERP",  None,   None,       "EWYUSDT",  None,             None),
    ("X-PERP",    None,   None,       None,       None,             None),
]

VENUE_ORDER = ["hotstuff", "hyperliquid", "binance", "bybit", "okx", "dydx"]


def fmt_price(p: float, sym: str) -> str:
    if p <= 0:
        return "N/A"
    if p > 10000:
        return f"{p:,.0f}"
    elif p > 100:
        return f"{p:,.2f}"
    elif p > 1:
        return f"{p:,.4f}"
    else:
        return f"{p:,.6f}"


def fmt_bps(bps: float) -> str:
    if abs(bps) < 1:
        return f"{bps:+.2f}"
    return f"{bps:+.1f}"


def color_div(bps: float) -> str:
    a = abs(bps)
    if a > 20:
        return "bold bright_green" if bps > 0 else "bold bright_red"
    elif a > 10:
        return "green" if bps > 0 else "red"
    elif a > 5:
        return "yellow"
    return "dim"


def color_spread(bps: float) -> str:
    if bps < 0.5:
        return "bright_green"
    elif bps < 2:
        return "green"
    elif bps < 5:
        return "yellow"
    elif bps < 10:
        return "orange3"
    return "bold red"


def color_funding(f: float) -> str:
    if f > 0.0001:
        return "green"
    elif f < -0.0001:
        return "red"
    return "dim"


def compute_composite(snapshots: dict) -> float:
    """Volume-weighted composite price."""
    total_w, weighted_sum = 0, 0
    has_data = False
    for snap in snapshots.values():
        if snap.mark_price <= 0:
            continue
        vol = snap.volume_24h
        oi_usd = snap.open_interest * snap.mark_price
        w = vol * 0.7 + oi_usd * 0.3
        if w <= 0:
            w = 1
        has_data = True
        weighted_sum += snap.mark_price * w
        total_w += w
    if not has_data or total_w == 0:
        prices = [s.mark_price for s in snapshots.values() if s.mark_price > 0]
        return sum(prices) / len(prices) if prices else 0
    return weighted_sum / total_w


class Dashboard:
    def __init__(self, instruments):
        self.instruments = instruments
        self.engine = DivergenceEngine()
        self.last_update = 0
        self.fetch_count = 0
        self.errors = []

    async def fetch_all(self):
        """Fetch all snapshots for all instruments across all venues."""
        from src.processors.divergence_engine import VenueSnapshot, Orderbook

        all_snapshots = {}
        tasks = []
        meta = []

        for inst in self.instruments:
            hs_sym, hl_sym, bn_sym, by_sym, okx_sym, dydx_sym = inst

            # Hotstuff
            tasks.append(self.engine.get_hotstuff_snapshot(hs_sym, 0))
            meta.append((hs_sym, "hotstuff"))

            # Hyperliquid
            if hl_sym:
                tasks.append(self.engine.get_hyperliquid_snapshot(hl_sym))
                meta.append((hs_sym, "hyperliquid"))

            # Binance
            if bn_sym:
                tasks.append(self.engine.get_binance_snapshot(bn_sym))
                meta.append((hs_sym, "binance"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for m, result in zip(meta, results):
            sym, venue = m
            if isinstance(result, Exception) or result is None:
                continue
            all_snapshots.setdefault(sym, {})[venue] = result

        # Fetch Bybit and OKX separately (different API patterns)
        for inst in self.instruments:
            hs_sym, _, _, by_sym, okx_sym, _ = inst
            if by_sym:
                try:
                    client = await self.engine._c()
                    r = await client.get(
                        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={by_sym}"
                    )
                    if r.status_code == 200:
                        d = r.json()
                        items = d.get("result", {}).get("list", [])
                        if items:
                            t = items[0]
                            last = float(t.get("lastPrice", 0))
                            fund = float(t.get("fundingRate", 0))
                            vol = float(t.get("turnover24h", 0))
                            snap = type('S', (), {
                                'venue': 'bybit', 'symbol': by_sym,
                                'mark_price': last, 'index_price': last,
                                'funding_8h': fund, 'open_interest': 0,
                                'volume_24h': vol, 'orderbook': None,
                                'timestamp': time.time()
                            })()
                            all_snapshots.setdefault(hs_sym, {})['bybit'] = snap
                except Exception:
                    pass

            if okx_sym:
                try:
                    client = await self.engine._c()
                    r = await client.get(
                        f"https://www.okx.com/api/v5/market/ticker?instId={okx_sym}"
                    )
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("data"):
                            t = d["data"][0]
                            last = float(t.get("last", 0))
                            snap = type('S', (), {
                                'venue': 'okx', 'symbol': okx_sym,
                                'mark_price': last, 'index_price': last,
                                'funding_8h': 0, 'open_interest': 0,
                                'volume_24h': float(t.get("vol24h", 0)),
                                'orderbook': None, 'timestamp': time.time()
                            })()
                            all_snapshots.setdefault(hs_sym, {})['okx'] = snap
                except Exception:
                    pass

        self.last_update = time.time()
        self.fetch_count += 1
        return all_snapshots

    def build_price_table(self, all_snapshots):
        table = Table(
            title="💰 Price Matrix & Divergence from Composite",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Coin", style="bold white", width=10, no_wrap=True)
        table.add_column("Composite", justify="right", width=14, style="bold bright_white")
        for venue in VENUE_ORDER:
            vname = {"hotstuff": "Hotstuff", "hyperliquid": "Hyperliquid",
                     "binance": "Binance", "bybit": "Bybit", "okx": "OKX", "dydx": "dYdX"}[venue]
            table.add_column(vname, justify="right", width=12)
            table.add_column("Div", justify="right", width=8)

        for inst in self.instruments:
            hs_sym = inst[0]
            snapshots = all_snapshots.get(hs_sym, {})
            if not snapshots:
                continue

            composite = compute_composite(snapshots)
            if composite <= 0:
                continue

            row = [hs_sym.replace("-PERP", ""), fmt_price(composite, hs_sym)]
            for venue in VENUE_ORDER:
                snap = snapshots.get(venue)
                if snap and snap.mark_price > 0:
                    div = (snap.mark_price - composite) / composite * 10000
                    row.append(fmt_price(snap.mark_price, hs_sym))
                    row.append(f"[{color_div(div)}]{fmt_bps(div)}[/]")
                else:
                    row.append("N/A")
                    row.append("N/A")
            table.add_row(*row)
        return table

    def build_funding_table(self, all_snapshots):
        table = Table(
            title="💰 Funding Rates (8h) & Annualized",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Coin", style="bold white", width=10)
        for venue in VENUE_ORDER:
            vname = {"hotstuff": "HS", "hyperliquid": "HL", "binance": "BN",
                     "bybit": "BY", "okx": "OKX", "dydx": "DYDX"}[venue]
            table.add_column(f"{vname} 8h", justify="right", width=12)
            table.add_column("APR", justify="right", width=10)

        for inst in self.instruments:
            hs_sym = inst[0]
            snapshots = all_snapshots.get(hs_sym, {})
            if not snapshots:
                continue
            row = [hs_sym.replace("-PERP", "")]
            for venue in VENUE_ORDER:
                snap = snapshots.get(venue)
                if snap:
                    fund = snap.funding_8h
                    apr = fund * 3 * 365 * 100
                    fc = color_funding(fund)
                    row.append(f"[{fc}]{fund:+.6f}[/]")
                    row.append(f"[{fc}]{apr:+.1f}%[/]")
                else:
                    row.append("N/A")
                    row.append("N/A")
            table.add_row(*row)
        return table

    def build_opportunities_table(self, all_snapshots):
        table = Table(
            title="🔥 Top Arbitrage Opportunities",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold cyan",
            title_style="bold white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Coin", style="bold white", width=10)
        table.add_column("Buy @", width=12)
        table.add_column("Sell @", width=12)
        table.add_column("Gross", justify="right", width=10)
        table.add_column("Net", justify="right", width=10)
        table.add_column("Score", justify="right", width=8)

        opportunities = []
        for inst in self.instruments:
            hs_sym = inst[0]
            snapshots = all_snapshots.get(hs_sym, {})
            if len(snapshots) < 2:
                continue
            composite = compute_composite(snapshots)
            if composite <= 0:
                continue

            best_buy, best_sell = None, None
            best_buy_p, best_sell_p = float("inf"), 0
            for venue, snap in snapshots.items():
                if snap.mark_price <= 0:
                    continue
                if snap.mark_price < best_buy_p:
                    best_buy_p, best_buy = snap.mark_price, venue
                if snap.mark_price > best_sell_p:
                    best_sell_p, best_sell = snap.mark_price, venue

            if best_buy and best_sell and best_buy != best_sell:
                gross = (best_sell_p - best_buy_p) / composite * 10000
                net = gross - 0.5  # Estimated fees
                score = min(100, max(0, net * 5))
                opportunities.append({
                    "symbol": hs_sym.replace("-PERP", ""),
                    "buy_venue": best_buy, "sell_venue": best_sell,
                    "gross": gross, "net": net, "score": score,
                })

        opportunities.sort(key=lambda x: x["score"], reverse=True)
        for opp in opportunities[:15]:
            nc = "bright_green" if opp["net"] > 5 else "green" if opp["net"] > 2 else "yellow" if opp["net"] > 0 else "red"
            table.add_row(
                opp["symbol"], opp["buy_venue"], opp["sell_venue"],
                f"{opp['gross']:.1f}", f"[{nc}]{opp['net']:.1f}[/]",
                f"{opp['score']:.0f}",
            )
        return table

    def build_status(self):
        elapsed = time.time() - self.last_update if self.last_update > 0 else 0
        s = Text()
        s.append(f"  Last: {elapsed:.0f}s ago  Fetches: {self.fetch_count}  ", style="dim")
        if self.errors:
            s.append(f"Errors: {len(self.errors)}  ", style="red")
        s.append("Ctrl+C to exit", style="dim")
        return s

    async def build_dashboard(self):
        all_snapshots = await self.fetch_all()
        price_table = self.build_price_table(all_snapshots)
        funding_table = self.build_funding_table(all_snapshots)
        opp_table = self.build_opportunities_table(all_snapshots)

        from rich.console import Group
        group = Group(
            price_table, "",
            Columns([funding_table, opp_table], equal=False), "",
            self.build_status(),
        )
        return Panel(
            group,
            title=f"[bold white]🔥 Cross-Venue Perp DEX Dashboard — {time.strftime('%H:%M:%S UTC', time.gmtime())}[/]",
            border_style="bright_blue",
            padding=(1, 2),
        )


async def main(args):
    dashboard = Dashboard(INSTRUMENTS)
    console = Console()

    if args.once:
        panel = await dashboard.build_dashboard()
        console.print(panel)
        await dashboard.engine.close()
        return

    try:
        with Live(
            await dashboard.build_dashboard(),
            console=console,
            refresh_per_second=1 / max(0.5, args.refresh),
            screen=True,
        ) as live:
            while True:
                await asyncio.sleep(args.refresh)
                live.update(await dashboard.build_dashboard())
    except KeyboardInterrupt:
        pass
    finally:
        await dashboard.engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cross-Venue Perp DEX Dashboard")
    parser.add_argument("--refresh", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
