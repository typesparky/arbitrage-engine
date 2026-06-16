#!/usr/bin/env python3
"""Crypto Factor Model Dashboard.

Implements Q-7 + Momentum + Dead/Alive factor model.
Shows factor scores, rankings, and composite signals.

Usage:
    python scripts/factor_dashboard.py
    python scripts/factor_dashboard.py --top 20
    python scripts/factor_dashboard.py --factor quality
    python scripts/factor_dashboard.py --sector DeFi
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from src.processors.factor_model import (
    DataFetcher,
    FactorModel,
    build_universe,
)


def fmt_z(z: float) -> str:
    """Format z-score with color."""
    if z > 2:
        return f"[bold bright_green]{z:+.2f}[/]"
    elif z > 1:
        return f"[green]{z:+.2f}[/]"
    elif z > 0.5:
        return f"[bright_green]{z:+.2f}[/]"
    elif z < -2:
        return f"[bold bright_red]{z:+.2f}[/]"
    elif z < -1:
        return f"[red]{z:+.2f}[/]"
    elif z < -0.5:
        return f"[bright_red]{z:+.2f}[/]"
    else:
        return f"[dim]{z:+.2f}[/]"


def fmt_composite(z: float) -> str:
    """Format composite score."""
    if z > 1:
        return f"[bold bright_green]{z:+.2f}[/]"
    elif z > 0.5:
        return f"[green]{z:+.2f}[/]"
    elif z < -1:
        return f"[bold bright_red]{z:+.2f}[/]"
    elif z < -0.5:
        return f"[red]{z:+.2f}[/]"
    else:
        return f"[dim]{z:+.2f}[/]"


def build_composite_table(scores: list, top_n: int = 30) -> Table:
    """Composite factor rankings."""
    table = Table(
        title="🏆 Composite Factor Rankings (Q-7 + MOM + D/A)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )

    table.add_column("#", width=4, justify="right")
    table.add_column("Token", width=10, style="bold white")
    table.add_column("Sector", width=12)
    table.add_column("Composite", justify="right", width=10)
    table.add_column("Size", justify="right", width=8)
    table.add_column("Reversal", justify="right", width=8)
    table.add_column("Momentum", justify="right", width=8)
    table.add_column("Value", justify="right", width=8)
    table.add_column("Vol", justify="right", width=8)
    table.add_column("Quality", justify="right", width=8)
    table.add_column("Funding", justify="right", width=8)
    table.add_column("D/A", justify="right", width=8)

    sorted_scores = sorted(scores, key=lambda s: s.composite_score, reverse=True)

    for i, s in enumerate(sorted_scores[:top_n], 1):
        table.add_row(
            str(i),
            s.symbol,
            s.sector if hasattr(s, 'sector') else "",
            fmt_composite(s.composite_score),
            fmt_z(s.size_z),
            fmt_z(s.reversal_z),
            fmt_z(s.momentum_z),
            fmt_z(s.value_z),
            fmt_z(s.volatility_z),
            fmt_z(s.quality_z),
            fmt_z(s.funding_z),
            fmt_z(s.dead_alive_z),
        )

    return table


def build_factor_leaderboard(scores: list, factor: str, top_n: int = 15) -> Table:
    """Top/bottom tokens for a single factor."""
    attr = f"{factor}_z"
    sorted_scores = sorted(scores, key=lambda s: getattr(s, attr, 0), reverse=True)

    table = Table(
        title=f"📊 {factor.upper()} Factor — Long (top) vs Short (bottom)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )

    table.add_column("Rank", width=5, justify="right")
    table.add_column("Token", width=10, style="bold white")
    table.add_column("Z-Score", justify="right", width=10)
    table.add_column("Side", width=6)

    # Top N (long)
    for i, s in enumerate(sorted_scores[:top_n], 1):
        z = getattr(s, attr, 0)
        table.add_row(str(i), s.symbol, fmt_z(z), "[green]LONG[/]")

    table.add_row("", "", "", "", end_section=True)

    # Bottom N (short)
    for i, s in enumerate(reversed(sorted_scores[-top_n:]), 1):
        z = getattr(s, attr, 0)
        table.add_row(str(i), s.symbol, fmt_z(z), "[red]SHORT[/]")

    return table


def build_sector_breakdown(scores: list, tokens: list) -> Table:
    """Average factor exposure by sector."""
    table = Table(
        title="🏭 Sector Factor Exposure (avg z-score)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )

    table.add_column("Sector", style="bold white", width=14)
    table.add_column("Tokens", justify="right", width=6)
    table.add_column("Composite", justify="right", width=10)
    table.add_column("Momentum", justify="right", width=10)
    table.add_column("Quality", justify="right", width=10)
    table.add_column("Value", justify="right", width=10)
    table.add_column("D/A", justify="right", width=10)

    # Group by sector
    token_sectors = {t.symbol: t.sector for t in tokens}
    sector_scores: dict[str, list] = {}
    for s in scores:
        sector = token_sectors.get(s.symbol, "Other")
        sector_scores.setdefault(sector, []).append(s)

    for sector in sorted(sector_scores.keys()):
        group = sector_scores[sector]
        n = len(group)
        avg = lambda attr: sum(getattr(s, attr, 0) for s in group) / n if n > 0 else 0

        table.add_row(
            sector,
            str(n),
            fmt_composite(avg("composite_score")),
            fmt_z(avg("momentum_z")),
            fmt_z(avg("quality_z")),
            fmt_z(avg("value_z")),
            fmt_z(avg("dead_alive_z")),
        )

    return table


def build_momentum_term_structure(scores: list) -> Table:
    """Show momentum at different lookback horizons."""
    table = Table(
        title="📈 Momentum Term Structure (multi-horizon)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )

    table.add_column("Token", style="bold white", width=10)
    table.add_column("7d", justify="right", width=10)
    table.add_column("14d", justify="right", width=10)
    table.add_column("21d", justify="right", width=10)
    table.add_column("42d", justify="right", width=10)
    table.add_column("63d", justify="right", width=10)
    table.add_column("Robust", justify="right", width=10)

    # Sort by robust momentum
    sorted_scores = sorted(scores, key=lambda s: s.momentum_z, reverse=True)

    for s in sorted_scores[:20]:
        table.add_row(
            s.symbol,
            fmt_z(getattr(s, "momentum_7d", 0)),
            fmt_z(getattr(s, "momentum_14d", 0)),
            fmt_z(getattr(s, "momentum_21d", 0)),
            fmt_z(getattr(s, "momentum_42d", 0)),
            fmt_z(getattr(s, "momentum_63d", 0)),
            fmt_z(s.momentum_z),
        )

    return table


async def main(args):
    console = Console()

    console.print("\n[bold white]🔬 Crypto Factor Model — Q-7 + Momentum + Dead/Alive[/]")
    console.print("[dim]Building universe from CoinGecko top coins...[/]\n")

    fetcher = DataFetcher()
    model = FactorModel()

    try:
        # Build universe
        with console.status("[bold cyan]Fetching market data...[/]"):
            tokens = await build_universe(fetcher, max_tokens=args.max_tokens)

        if not tokens:
            console.print("[red]Failed to fetch data. Check internet connection.[/]")
            return

        console.print(f"[green]✓[/] Loaded {len(tokens)} tokens")

        # Compute factors
        with console.status("[bold cyan]Computing factor exposures...[/]"):
            scores = model.compute_factors(tokens)

        console.print(f"[green]✓[/] Computed {len(scores)} factor score vectors\n")

        if args.factor:
            # Single factor view
            table = build_factor_leaderboard(scores, args.factor, top_n=args.top)
            console.print(table)
        else:
            # Full dashboard
            composite = build_composite_table(scores, top_n=args.top)
            sector = build_sector_breakdown(scores, tokens)
            momentum = build_momentum_term_structure(scores)

            # Layout
            console.print(composite)
            console.print("")
            bottom = Columns([sector, momentum], equal=False)
            console.print(bottom)

            # Factor legend
            console.print("")
            legend = Text()
            legend.append("\n  Factor Legend: ", style="bold")
            legend.append("Size=-log(mcap) ", style="dim")
            legend.append("Reversal=-21d_ret ", style="dim")
            legend.append("Momentum=multi-horizon ", style="dim")
            legend.append("Value=NVT_proxy ", style="dim")
            legend.append("Vol=EWMA_resid_vol ", style="dim")
            legend.append("Quality=on-chain_activity ", style="dim")
            legend.append("Funding=contrarian ", style="dim")
            legend.append("D/A=longevity×vitality ", style="dim")
            console.print(legend)

    finally:
        await model.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto Factor Model Dashboard")
    parser.add_argument("--top", type=int, default=30, help="Show top N tokens (default: 30)")
    parser.add_argument("--max-tokens", type=int, default=50, help="Max tokens in universe (default: 50)")
    parser.add_argument("--factor", type=str, default=None,
                        choices=["size", "reversal", "momentum", "value", "volatility", "quality", "funding", "dead_alive"],
                        help="Show single factor leaderboard")
    parser.add_argument("--sector", type=str, default=None,
                        help="Filter to sector")

    args = parser.parse_args()
    asyncio.run(main(args))
