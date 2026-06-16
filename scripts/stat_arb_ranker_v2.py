#!/usr/bin/env python3
"""Statistical Arb Pair Ranker v2 — Sub-minute timeframe.

Fetches 1-min candle data across 5 venues:
  Hotstuff, Binance, Bybit, OKX, dYdX

Hyperliquid excluded (no historical candle API).

Computes per-pair expectancy = |mean_bps| x win_rate x |sharpe|
Ranks all pairs from best to worst for stat arb.

Usage:
    python scripts/stat_arb_ranker_v2.py
    python scripts/stat_arb_ranker_v2.py --minutes 60
    python scripts/stat_arb_ranker_v2.py --json
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


# ── All Hotstuff perps with cross-venue symbols ───────────────────────
# (hotstuff_sym, binance_sym, bybit_sym, okx_sym, dydx_sym, category)
ALL_PAIRS = [
    # Crypto (5-venue overlap)
    ("BTC-PERP",  "BTCUSDT",  "BTCUSDT",  "BTC-USDT-SWAP",  "BTC-USD",  "crypto"),
    ("ETH-PERP",  "ETHUSDT",  "ETHUSDT",  "ETH-USDT-SWAP",  "ETH-USD",  "crypto"),
    ("SOL-PERP",  "SOLUSDT",  "SOLUSDT",  "SOL-USDT-SWAP",  "SOL-USD",  "crypto"),
    ("XRP-PERP",  "XRPUSDT",  "XRPUSDT",  "XRP-USDT-SWAP",  "XRP-USD",  "crypto"),
    ("BNB-PERP",  "BNBUSDT",  "BNBUSDT",  None,              "BNB-USD",  "crypto"),
    ("HYPE-PERP", "HYPEUSDT", "HYPEUSDT", None,              "HYPE-USD", "crypto"),
    ("ZEC-PERP",  "ZECUSDT",  "ZECUSDT",  None,              "ZEC-USD",  "crypto"),
    # RWA Stocks (Hotstuff + Bybit)
    ("AAPL-PERP", None,       "AAPLUSDT", None,              None,       "rwa_stock"),
    ("TSLA-PERP", None,       "TSLAUSDT", None,              None,       "rwa_stock"),
    ("NVDA-PERP", None,       "NVDAUSDT", None,              None,       "rwa_stock"),
    ("META-PERP", None,       "METAUSDT", None,              None,       "rwa_stock"),
    ("MSFT-PERP", None,       "MSFTUSDT", None,              None,       "rwa_stock"),
    ("PLTR-PERP", None,       "PLTRUSDT", None,              None,       "rwa_stock"),
    ("GOOGL-PERP",None,       "GOOGLUSDT",None,              None,       "rwa_stock"),
    ("AMZN-PERP", None,       "AMZNUSDT", None,              None,       "rwa_stock"),
    ("EWJ-PERP",  None,       "EWJUSDT",  None,              None,       "rwa_stock"),
    ("EWY-PERP",  None,       "EWYUSDT",  None,              None,       "rwa_stock"),
    # Commodities (Hotstuff only)
    ("GOLD-PERP", None,       None,       None,              None,       "commodity"),
    ("WTIOIL-PERP",None,      None,       None,              None,       "commodity"),
    ("BRENTOIL-PERP",None,    None,       None,              None,       "commodity"),
    ("SILVER-PERP",None,      None,       None,              None,       "commodity"),
    ("NATGAS-PERP",None,      None,       None,              None,       "commodity"),
    # Forex
    ("EURUSD-PERP",None,      None,       None,              None,       "forex"),
    ("USDJPY-PERP",None,      None,       None,              None,       "forex"),
    # Indices
    ("USA500-PERP",None,      None,       None,              None,       "index"),
    ("USA100-PERP",None,      None,       None,              None,       "index"),
    ("X-PERP",    None,       None,       None,              None,       "other"),
]


async def fetch_1min_history(minutes: int = 60):
    """Fetch 1-min candle data for all pairs across 5 venues."""
    async with httpx.AsyncClient(timeout=15) as c:
        now = int(time.time())
        from_ts = now - minutes * 60  # seconds
        
        # Get Hotstuff instrument IDs
        r = await c.post('https://api.hotstuff.trade/info', json={
            'method': 'instruments', 'params': {'type': 'perps'}
        })
        hs_insts = {p['name'].replace("-PERP", ""): p['id'] for p in r.json().get('perps', []) if not p.get('delisted')}
        
        results = {}
        
        for hs_sym, bn_sym, by_sym, okx_sym, dydx_sym, cat in ALL_PAIRS:
            base = hs_sym.replace("-PERP", "")
            results[base] = {"category": cat, "pairs": {}}
            
            # Hotstuff 1-min candles
            inst_id = hs_insts.get(base)
            if inst_id:
                r2 = await c.post('https://api.hotstuff.trade/info', json={
                    'method': 'chart', 'params': {
                        'symbol': str(inst_id), 'chart_type': 'mark',
                        'resolution': '1', 'from': from_ts, 'to': now
                    }
                })
                d2 = r2.json()
                if isinstance(d2, list) and d2:
                    hs_prices = [candle.get('close', 0) for candle in d2]
                else:
                    hs_prices = []
            else:
                hs_prices = []
            
            if not hs_prices:
                continue
            
            # Binance 1m
            if bn_sym:
                r3 = await c.get(
                    f'https://fapi.binance.com/fapi/v1/klines?symbol={bn_sym}&interval=1m&limit={minutes}'
                )
                d3 = r3.json()
                if isinstance(d3, list) and d3:
                    bn_prices = [float(k[4]) for k in d3]
                    results[base]["pairs"]["HS_BN"] = (hs_prices, bn_prices)
            
            # Bybit 1m
            if by_sym:
                r4 = await c.get(
                    f'https://api.bybit.com/v5/market/kline?category=linear&symbol={by_sym}&interval=1&limit={minutes}'
                )
                d4 = r4.json()
                if d4.get('result', {}).get('list'):
                    by_prices = [float(k[4]) for k in d4['result']['list']]
                    results[base]["pairs"]["HS_BY"] = (hs_prices, by_prices)
            
            # OKX 1m
            if okx_sym:
                r5 = await c.get(
                    f'https://www.okx.com/api/v5/market/candles?instId={okx_sym}&bar=1m&limit={minutes}'
                )
                d5 = r5.json()
                if d5.get('data'):
                    okx_prices = [float(k[4]) for k in d5['data']]
                    results[base]["pairs"]["HS_OKX"] = (hs_prices, okx_prices)
            
            # dYdX 1m
            if dydx_sym:
                r6 = await c.get(
                    f'https://indexer.dydx.trade/v4/candles/perpetualMarkets/{dydx_sym}?resolution=1MIN&limit={minutes}'
                )
                d6 = r6.json()
                if d6.get('candles'):
                    dydx_prices = [float(candle['close']) for candle in d6['candles']]
                    results[base]["pairs"]["HS_DYDX"] = (hs_prices, dydx_prices)
            
            await asyncio.sleep(0.2)
        
        return results


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
        "autocorr": autocorr, "half_life_minutes": half_life,
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
        title="📊 Statistical Arb Pair Rankings — 1-Minute Data (by Expectancy)",
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
    table.add_column("Score", justify="right", width=8)
    
    for i, r in enumerate(rankings, 1):
        ec = color_exp(r["expectancy"])
        score = min(100, r["expectancy"] * 2)
        sc = color_exp(r["expectancy"])
        hl = f"{r['half_life_minutes']:.0f}m" if r['half_life_minutes'] != float('inf') else "∞"
        table.add_row(
            str(i), r["pair"], r["category"], str(r["n"]),
            fmt_bps(r["mean_bps"]), fmt_bps(r["std_bps"]),
            fmt_pct(r["win_rate"]), f"{r['sharpe']:+.2f}",
            f"[{ec}]{r['expectancy']:.1f}[/]",
            hl, f"[{sc}]{score:.0f}[/]",
        )
    return table


def build_detail_table(rankings: list[dict]) -> Table:
    table = Table(
        title="📈 Detailed Statistics (Top 15)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Pair", style="bold white", width=18)
    table.add_column("Mean", justify="right", width=10)
    table.add_column("Median", justify="right", width=10)
    table.add_column("Std", justify="right", width=8)
    table.add_column("Min", justify="right", width=10)
    table.add_column("Max", justify="right", width=10)
    table.add_column("P5", justify="right", width=8)
    table.add_column("P95", justify="right", width=8)
    table.add_column("Autocorr", justify="right", width=10)
    table.add_column("Max Adv", justify="right", width=10)
    
    for r in rankings[:15]:
        table.add_row(
            r["pair"],
            fmt_bps(r["mean_bps"]), fmt_bps(r["median_bps"]), fmt_bps(r["std_bps"]),
            fmt_bps(r["min_bps"]), fmt_bps(r["max_bps"]),
            fmt_bps(r["p5_bps"]), fmt_bps(r["p95_bps"]),
            f"{r['autocorr']:+.3f}", fmt_bps(r["max_adverse_bps"]),
        )
    return table


async def main(args):
    console = Console()
    console.print(f"\n[bold white]📊 Statistical Arb Pair Ranker v2 — 1-Minute Candles[/]")
    console.print(f"[dim]Fetching {args.minutes} minutes of 1m data across 5 venues...[/]\n")
    
    with console.status("[bold cyan]Fetching data...[/]"):
        data = await fetch_1min_history(minutes=args.minutes)
    
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
    
    console.print(f"[green]✓[/] Computed statistics for {len(rankings)} venue pairs\n")
    
    console.print(build_ranking_table(rankings))
    console.print("")
    console.print(build_detail_table(rankings))
    
    # Summary by category
    console.print("")
    by_cat = {}
    for r in rankings:
        by_cat.setdefault(r["category"], []).append(r["expectancy"])
    for cat in sorted(by_cat.keys()):
        exps = by_cat[cat]
        console.print(f"  {cat}: {len(exps)} pairs, avg_exp={statistics.mean(exps):.1f}, best={max(exps):.1f}", style="dim")
    console.print("  Expectancy = |mean_bps| x win_rate x |sharpe|", style="dim")
    console.print("  Win rate = % of time |divergence| > 5 bps (fee threshold)", style="dim")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stat Arb Ranker v2 — 1-min candles")
    parser.add_argument("--minutes", type=int, default=60, help="Minutes of 1m data (default: 60)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
