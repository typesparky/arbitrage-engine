#!/usr/bin/env python3
"""Statistical Arb Pair Ranker.

Uses existing divergence_engine + hotstuff scraper modules.
Fetches historical data for all Hotstuff perps vs cross-venue counterparts,
computes divergence statistics, ranks all pairs by expectancy.

Expectancy = |mean_divergence_bps| × win_rate × |sharpe_of_divergence|

Usage:
    python scripts/stat_arb_ranker.py
    python scripts/stat_arb_ranker.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from rich.console import Console
from rich.table import Table
from rich import box
from rich.columns import Columns

from src.scrapers.hotstuff import HotstuffClient
from src.processors.divergence_engine import DivergenceEngine
from src.processors.multi_venue import MultiVenueClient, CrossVenueAnalyzer
from src.processors.hotstuff_fees import get_fee_tier, analyze_arb


# ── All Hotstuff perps with cross-venue symbols ───────────────────────
ALL_PAIRS = [
    # (hotstuff_sym, binance_sym, bybit_sym, okx_sym, category)
    ("BTC-PERP",  "BTCUSDT",  "BTCUSDT",  "BTC-USDT-SWAP", "crypto"),
    ("ETH-PERP",  "ETHUSDT",  "ETHUSDT",  "ETH-USDT-SWAP", "crypto"),
    ("SOL-PERP",  "SOLUSDT",  "SOLUSDT",  "SOL-USDT-SWAP", "crypto"),
    ("XRP-PERP",  "XRPUSDT",  "XRPUSDT",  "XRP-USDT-SWAP", "crypto"),
    ("BNB-PERP",  "BNBUSDT",  "BNBUSDT",  None,             "crypto"),
    ("HYPE-PERP", "HYPEUSDT", "HYPEUSDT", None,             "crypto"),
    ("ZEC-PERP",  "ZECUSDT",  "ZECUSDT",  None,             "crypto"),
    ("AAPL-PERP", None,       "AAPLUSDT", None,             "rwa_stock"),
    ("TSLA-PERP", None,       "TSLAUSDT", None,             "rwa_stock"),
    ("NVDA-PERP", None,       "NVDAUSDT", None,             "rwa_stock"),
    ("META-PERP", None,       "METAUSDT", None,             "rwa_stock"),
    ("MSFT-PERP", None,       "MSFTUSDT", None,             "rwa_stock"),
    ("PLTR-PERP", None,       "PLTRUSDT", None,             "rwa_stock"),
    ("GOOGL-PERP",None,       "GOOGLUSDT",None,             "rwa_stock"),
    ("AMZN-PERP", None,       "AMZNUSDT", None,             "rwa_stock"),
    ("EWJ-PERP",  None,       "EWJUSDT",  None,             "rwa_stock"),
    ("EWY-PERP",  None,       "EWYUSDT",  None,             "rwa_stock"),
    ("GOLD-PERP", None,       None,       None,             "commodity"),
    ("WTIOIL-PERP",None,      None,       None,             "commodity"),
    ("BRENTOIL-PERP",None,    None,       None,             "commodity"),
    ("SILVER-PERP",None,      None,       None,             "commodity"),
    ("NATGAS-PERP",None,      None,       None,             "commodity"),
    ("EURUSD-PERP",None,      None,       None,             "forex"),
    ("USDJPY-PERP",None,      None,       None,             "forex"),
    ("USA500-PERP",None,      None,       None,             "index"),
    ("USA100-PERP",None,      None,       None,             "index"),
    ("X-PERP",    None,       None,       None,             "other"),
]


async def fetch_history(days: int = 7):
    """Fetch hourly price history for all pairs."""
    hs_client = HotstuffClient()
    mv_client = MultiVenueClient()
    engine = DivergenceEngine()

    try:
        await hs_client.initialize()
        now = int(time.time())
        from_ts = now - days * 86400

        # Get Hotstuff instrument IDs
        hs_instruments = {}
        for inst in hs_client._instruments.values():
            if inst.name.endswith("-PERP") and not inst.delisted:
                base = inst.name.replace("-PERP", "")
                hs_instruments[base] = inst

        results = {}

        for hs_sym, bn_sym, by_sym, okx_sym, cat in ALL_PAIRS:
            base = hs_sym.replace("-PERP", "")
            results[base] = {"category": cat, "pairs": {}}

            # Hotstuff history using instrument ID
            inst = hs_instruments.get(base)
            if inst:
                hs_chart = await hs_client.get_chart(
                    inst.id, "mark", "60", from_ts, now
                )
                if hs_chart:
                    hs_prices = [c.get('close', 0) for c in hs_chart]
                else:
                    hs_prices = []
            else:
                hs_prices = []

            if not hs_prices:
                continue

            # Binance
            if bn_sym:
                bn_chart = await mv_client.get_binance(bn_sym + "___")  # Hack to get single
                # Actually use direct API for history
                bn_history = await _fetch_binance_history(bn_sym, days)
                if bn_history:
                    results[base]["pairs"]["HS_BN"] = (hs_prices, bn_history)

            # Bybit
            if by_sym:
                by_history = await _fetch_bybit_history(by_sym, days)
                if by_history:
                    results[base]["pairs"]["HS_BY"] = (hs_prices, by_history)

            # OKX
            if okx_sym:
                okx_history = await _fetch_okx_history(okx_sym, days)
                if okx_history:
                    results[base]["pairs"]["HS_OKX"] = (hs_prices, okx_history)

            await asyncio.sleep(0.3)

        return results
    finally:
        await hs_client.close()
        await mv_client.close()
        await engine.close()


async def _fetch_binance_history(symbol: str, days: int) -> list[float] | None:
    """Fetch Binance perp kline history."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={days * 24}"
            )
            d = r.json()
            if isinstance(d, list) and d:
                return [float(k[4]) for k in d]
    except Exception:
        pass
    return None


async def _fetch_bybit_history(symbol: str, days: int) -> list[float] | None:
    """Fetch Bybit kline history."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=60&limit={days * 24}"
            )
            d = r.json()
            if d.get('result', {}).get('list'):
                return [float(k[4]) for k in d['result']['list']]
    except Exception:
        pass
    return None


async def _fetch_okx_history(inst_id: str, days: int) -> list[float] | None:
    """Fetch OKX candle history."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar=1h&limit={days * 24}"
            )
            d = r.json()
            if d.get('data'):
                return [float(k[4]) for k in d['data']]
    except Exception:
        pass
    return None


def compute_stats(prices_a: list[float], prices_b: list[float],
                  fee_bps: float = 5.0) -> dict | None:
    """Compute divergence statistics between two price series."""
    n = min(len(prices_a), len(prices_b))
    if n < 10:
        return None

    a, b = prices_a[-n:], prices_b[-n:]
    divs = [(a[i] - b[i]) / b[i] * 10000 for i in range(n) if b[i] > 0]
    if len(divs) < 10:
        return None

    mean_div = statistics.mean(divs)
    std_div = statistics.stdev(divs) if len(divs) > 1 else 0
    median_div = statistics.median(divs)

    # Win rate: % of time |divergence| > fee threshold
    profitable = sum(1 for d in divs if abs(d) > fee_bps)
    win_rate = profitable / len(divs)

    # Sharpe of divergence
    sharpe = mean_div / std_div if std_div > 0 else 0

    # Expectancy
    expectancy = abs(mean_div) * win_rate * abs(sharpe) if sharpe != 0 else 0

    # Half-life via autocorrelation
    if len(divs) > 20:
        mean_d = statistics.mean(divs)
        autocov = sum((divs[i] - mean_d) * (divs[i-1] - mean_d) for i in range(1, len(divs))) / len(divs)
        autocorr = autocov / (std_div ** 2) if std_div > 0 else 0
        half_life = -math.log(2) / math.log(autocorr) if 0 < autocorr < 1 else float('inf')
    else:
        autocorr, half_life = 0, float('inf')

    sorted_divs = sorted(divs)
    return {
        "n": len(divs),
        "mean_bps": mean_div, "median_bps": median_div, "std_bps": std_div,
        "min_bps": min(divs), "max_bps": max(divs),
        "p5_bps": sorted_divs[max(0, int(len(sorted_divs) * 0.05))],
        "p95_bps": sorted_divs[min(len(sorted_divs) - 1, int(len(sorted_divs) * 0.95))],
        "win_rate": win_rate, "sharpe": sharpe, "expectancy": expectancy,
        "autocorr": autocorr, "half_life_hours": half_life,
        "max_adverse_bps": min(divs) if mean_div > 0 else max(divs),
    }


def fmt_bps(v: float) -> str:
    return f"{v:+.2f}" if abs(v) < 1 else f"{v:+.1f}"


def fmt_pct(v: float) -> str:
    return f"{v:.0%}"


def color_exp(v: float) -> str:
    if v > 50: return "bold bright_green"
    if v > 20: return "green"
    if v > 10: return "yellow"
    if v > 5: return "orange3"
    return "red"


def build_ranking_table(rankings: list[dict]) -> Table:
    table = Table(
        title="📊 Statistical Arb Pair Rankings (by Expectancy Score)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", width=4, justify="right")
    table.add_column("Pair", style="bold white", width=18)
    table.add_column("Cat", width=10)
    table.add_column("N", justify="right", width=5)
    table.add_column("Mean bps", justify="right", width=10)
    table.add_column("Std bps", justify="right", width=8)
    table.add_column("Win Rate", justify="right", width=10)
    table.add_column("Sharpe", justify="right", width=8)
    table.add_column("Expectancy", justify="right", width=12)
    table.add_column("Half-life", justify="right", width=10)

    for i, r in enumerate(rankings, 1):
        ec = color_exp(r["expectancy"])
        score = min(100, r["expectancy"] * 2)
        table.add_row(
            str(i), r["pair"], r["category"], str(r["n"]),
            fmt_bps(r["mean_bps"]), fmt_bps(r["std_bps"]),
            fmt_pct(r["win_rate"]), f"{r['sharpe']:+.2f}",
            f"[{ec}]{r['expectancy']:.1f}[/]",
            f"{r['half_life_hours']:.0f}h" if r['half_life_hours'] != float('inf') else "∞",
        )
    return table


async def main(args):
    console = Console()
    console.print(f"\n[bold white]📊 Statistical Arb Pair Ranker[/]\n")

    with console.status("[bold cyan]Fetching historical data...[/]"):
        data = await fetch_history(days=args.days)

    rankings = []
    for base, info in data.items():
        for pair_name, (pa, pb) in info["pairs"].items():
            stats = compute_stats(pa, pb, fee_bps=5.0)
            if stats:
                rankings.append({
                    "pair": f"{base}/{pair_name}",
                    "base": base, "venue_pair": pair_name,
                    "category": info["category"], **stats,
                })

    rankings.sort(key=lambda r: r["expectancy"], reverse=True)

    if args.json:
        print(json.dumps(rankings, indent=2, default=str))
        return

    if not rankings:
        console.print("[red]No data fetched.[/]")
        return

    console.print(f"[green]✓[/] Computed statistics for {len(rankings)} pairs\n")
    console.print(build_ranking_table(rankings))

    # By category summary
    by_cat = {}
    for r in rankings:
        by_cat.setdefault(r["category"], []).append(r["expectancy"])
    console.print("")
    for cat in sorted(by_cat.keys()):
        exps = by_cat[cat]
        console.print(f"  {cat}: {len(exps)} pairs, avg_exp={statistics.mean(exps):.1f}, best={max(exps):.1f}", style="dim")
    console.print("  Expectancy = |mean_bps| × win_rate × |sharpe|", style="dim")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
