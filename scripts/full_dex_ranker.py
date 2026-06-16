#!/usr/bin/env python3
"""Full DEX Statistical Arb Ranker — 1-Minute Candles, 6 Venues.

Venues: Hotstuff, Binance, Bybit, OKX, dYdX, Aster
Excluded: Hyperliquid (no hist candles), Lighter (WS-only), GRVT (no public API)

Usage:
    python scripts/full_dex_ranker.py
    python scripts/full_dex_ranker.py --minutes 60
    python scripts/full_dex_ranker.py --json
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


# ── (base, hs_id, binance, bybit, okx, dydx, aster, category) ──
ALL_PAIRS = [
    ("BTC",  1,  "BTCUSDT",  "BTCUSDT",  "BTC-USDT-SWAP",  "BTC-USD",  "BTCUSDT",  "crypto"),
    ("ETH",  2,  "ETHUSDT",  "ETHUSDT",  "ETH-USDT-SWAP",  "ETH-USD",  "ETHUSDT",  "crypto"),
    ("SOL",  3,  "SOLUSDT",  "SOLUSDT",  "SOL-USDT-SWAP",  "SOL-USD",  "SOLUSDT",  "crypto"),
    ("GOLD", 4,  None,       None,       None,             None,       None,       "commodity"),
    ("SILVER",5,None,        None,       None,             None,       None,       "commodity"),
    ("X",    6,  None,       None,       None,             None,       None,       "other"),
    ("HYPE", 7,  "HYPEUSDT", "HYPEUSDT", None,             "HYPE-USD", "HYPEUSDT", "crypto"),
    ("XRP",  8,  "XRPUSDT",  "XRPUSDT",  "XRP-USDT-SWAP",  "XRP-USD",  "XRPUSDT",  "crypto"),
    ("ZEC",  9,  "ZECUSDT",  "ZECUSDT",  None,             "ZEC-USD",  "ZECUSDT",  "crypto"),
    ("BNB",  10, "BNBUSDT",  "BNBUSDT",  None,             "BNB-USD",  "BNBUSDT",  "crypto"),
    ("WTIOIL",11,None,       None,       None,             None,       None,       "commodity"),
    ("BRENTOIL",12,None,     None,       None,             None,       None,       "commodity"),
    ("NATGAS",13,None,       None,       None,             None,       None,       "commodity"),
    ("EURUSD",14,None,       None,       None,             None,       None,       "forex"),
    ("USDJPY",15,None,       None,       None,             None,       None,       "forex"),
    ("USA500",16,None,       None,       None,             None,       None,       "index"),
    ("USA100",17,None,       None,       None,             None,       None,       "index"),
    ("AAPL", 18, None,       "AAPLUSDT", None,             None,       "AAPLUSDT", "rwa_stock"),
    ("NVDA", 19, None,       "NVDAUSDT", None,             None,       "NVDAUSDT", "rwa_stock"),
    ("TSLA", 20, None,       "TSLAUSDT", None,             None,       "TSLAUSDT", "rwa_stock"),
    ("MSFT", 21, None,       "MSFTUSDT", None,             None,       "MSFTUSDT", "rwa_stock"),
    ("META", 22, None,       "METAUSDT", None,             None,       "METAUSDT", "rwa_stock"),
    ("AMZN", 23, None,       "AMZNUSDT", None,             None,       "AMZNUSDT", "rwa_stock"),
    ("GOOGL",24, None,       "GOOGLUSDT",None,             None,       "GOOGLUSDT","rwa_stock"),
    ("EWY",  27, None,       "EWYUSDT",  None,             None,       "EWYUSDT",  "rwa_stock"),
    ("EWJ",  28, None,       "EWJUSDT",  None,             None,       "EWJUSDT",  "rwa_stock"),
    ("PLTR", 39, None,       "PLTRUSDT", None,             None,       "PLTRUSDT", "rwa_stock"),
]


async def fetch_all(minutes=60):
    async with httpx.AsyncClient(timeout=15) as c:
        now = int(time.time())
        from_ts = now - minutes * 60
        results = {}
        
        for base, hs_id, bn, by_, okx, dydx, aster, cat in ALL_PAIRS:
            results[base] = {"category": cat, "pairs": {}}
            
            r = await c.post('https://api.hotstuff.trade/info', json={
                'method': 'chart', 'params': {
                    'symbol': str(hs_id), 'chart_type': 'mark',
                    'resolution': '1', 'from': from_ts, 'to': now
                }
            })
            d = r.json()
            if not (isinstance(d, list) and d):
                continue
            hs = [x.get('close', 0) for x in d]
            
            if bn:
                r2 = await c.get(f'https://fapi.binance.com/fapi/v1/klines?symbol={bn}&interval=1m&limit={minutes}')
                d2 = r2.json()
                if isinstance(d2, list) and d2:
                    results[base]['BN'] = (hs, [float(k[4]) for k in d2])
                    results[base]['pairs']['BN'] = (hs, [float(k[4]) for k in d2])
            
            if by_:
                r3 = await c.get(f'https://api.bybit.com/v5/market/kline?category=linear&symbol={by_}&interval=1&limit={minutes}')
                d3 = r3.json()
                if d3.get('result', {}).get('list'):
                    results[base]['BY'] = (hs, [float(k[4]) for k in d3['result']['list']])
                    results[base]['pairs']['BY'] = (hs, [float(k[4]) for k in d3['result']['list']])
            
            if okx:
                r4 = await c.get(f'https://www.okx.com/api/v5/market/candles?instId={okx}&bar=1m&limit={minutes}')
                d4 = r4.json()
                if d4.get('data'):
                    results[base]['OKX'] = (hs, [float(k[4]) for k in d4['data']])
                    results[base]['pairs']['OKX'] = (hs, [float(k[4]) for k in d4['data']])
            
            if dydx:
                r5 = await c.get(f'https://indexer.dydx.trade/v4/candles/perpetualMarkets/{dydx}?resolution=1MIN&limit={minutes}')
                d5 = r5.json()
                if d5.get('candles'):
                    results[base]['DYDX'] = (hs, [float(c['close']) for c in d5['candles']])
                    results[base]['pairs']['DYDX'] = (hs, [float(c['close']) for c in d5['candles']])
            
            if aster:
                r6 = await c.get(f'https://fapi.asterdex.com/fapi/v1/klines?symbol={aster}&interval=1m&limit={minutes}')
                d6 = r6.json()
                if isinstance(d6, list) and d6:
                    results[base]['ASTER'] = (hs, [float(k[4]) for k in d6])
                    results[base]['pairs']['ASTER'] = (hs, [float(k[4]) for k in d6])
            
            await asyncio.sleep(0.2)
        
        return results


def compute_stats(pa, pb, fee_bps=5.0):
    n = min(len(pa), len(pb))
    if n < 10: return None
    a, b = pa[-n:], pb[-n:]
    divs = [(a[i]-b[i])/b[i]*10000 for i in range(n) if b[i] > 0]
    if len(divs) < 10: return None
    
    mean_div = statistics.mean(divs)
    std_div = statistics.stdev(divs) if len(divs) > 1 else 0
    win_rate = sum(1 for d in divs if abs(d) > fee_bps) / len(divs)
    sharpe = mean_div / std_div if std_div > 0 else 0
    expectancy = abs(mean_div) * win_rate * abs(sharpe) if sharpe != 0 else 0
    
    if len(divs) > 20:
        mean_d = statistics.mean(divs)
        autocov = sum((divs[i]-mean_d)*(divs[i-1]-mean_d) for i in range(1,len(divs)))/len(divs)
        autocorr = autocov/(std_div**2) if std_div > 0 else 0
        hl = -math.log(2)/math.log(autocorr) if 0 < autocorr < 1 else float('inf')
    else:
        autocorr, hl = 0, float('inf')
    
    sd = sorted(divs)
    return {
        "n": len(divs), "mean_bps": mean_div, "std_bps": std_div,
        "min_bps": min(divs), "max_bps": max(divs),
        "p5": sd[max(0,int(len(sd)*0.05))], "p95": sd[min(len(sd)-1,int(len(sd)*0.95))],
        "win_rate": win_rate, "sharpe": sharpe, "expectancy": expectancy,
        "autocorr": autocorr, "half_life_min": hl,
    }


def fmt_bps(v): return f"{v:+.2f}" if abs(v) < 1 else f"{v:+.1f}"
def fmt_pct(v): return f"{v:.0%}"
def color_exp(v):
    if v > 50: return "bold bright_green"
    if v > 20: return "green"
    if v > 10: return "yellow"
    if v > 5: return "orange3"
    return "red"


def build_table(rankings):
    t = Table(title="📊 Full DEX Stat Arb Rankings — 1-Min Candles, 6 Venues",
              box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
              title_style="bold white", expand=True, padding=(0,1))
    t.add_column("#", width=4, justify="right")
    t.add_column("Pair", style="bold white", width=22)
    t.add_column("Cat", width=10)
    t.add_column("N", justify="right", width=5)
    t.add_column("Mean bps", justify="right", width=10)
    t.add_column("Std", justify="right", width=8)
    t.add_column("Win%", justify="right", width=8)
    t.add_column("Sharpe", justify="right", width=8)
    t.add_column("Expectancy", justify="right", width=12)
    t.add_column("Half-life", justify="right", width=10)
    
    for i, r in enumerate(rankings, 1):
        ec = color_exp(r["expectancy"])
        hl = f"{r['half_life_min']:.0f}m" if r['half_life_min'] != float('inf') else "∞"
        t.add_row(str(i), r["pair"], r["category"], str(r["n"]), fmt_bps(r["mean_bps"]),
                  fmt_bps(r["std_bps"]), fmt_pct(r["win_rate"]),
                  f"{r['sharpe']:+.2f}", f"[{ec}]{r['expectancy']:.1f}[/]", hl)
    return t


async def main(args):
    console = Console()
    console.print(f"\n[bold white]📊 Full DEX Stat Arb Ranker — 1-Minute Candles, 6 Venues[/]")
    
    with console.status("[bold cyan]Fetching 1m data from 6 DEXs...[/]"):
        data = await fetch_all(minutes=args.minutes)
    rankings = []
    for base, info in data.items():
        for vn, (pa, pb) in info["pairs"].items():
            s = compute_stats(pa, pb)
            if s:
                rankings.append({"pair": f"{base}/HS_{vn}", "base": base, "venue": vn, "category": info["category"], **s})
    
    rankings.sort(key=lambda r: r["expectancy"], reverse=True)
    
    if args.json:
        print(json.dumps(rankings, indent=2, default=str))
        return
    
    if not rankings:
        console.print("[red]No data.[/]")
        return
    
    console.print(f"[green]✓[/] {len(rankings)} pairs analyzed\n")
    console.print(build_table(rankings))
    
    console.print("")
    by_venue = {}
    for r in rankings:
        by_venue.setdefault(r["venue"], []).append(r["expectancy"])
    for vn in sorted(by_venue.keys()):
        exps = by_venue[vn]
        console.print(f"  {vn}: {len(exps)} pairs, avg={statistics.mean(exps):.1f}, best={max(exps):.1f}", style="dim")
    console.print(f"\n  Excluded: Hyperliquid (no hist API), Lighter (WS-only), GRVT (no public API)", style="dim")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
