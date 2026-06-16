#!/usr/bin/env python3
"""Crypto Factor Model v2 Dashboard.

Q-7 + Dead/Alive + Enhanced Momentum.
Shows factor scores, rankings, sector breakdown, momentum term structure.

Usage:
    python scripts/factor_dashboard_v2.py
    python scripts/factor_dashboard_v2.py --top 20
    python scripts/factor_dashboard_v2.py --factor dead_alive
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.columns import Columns
from rich.table import Table
from rich import box

from src.processors.factor_model_v2 import DataFetcher, FactorModel, build_universe


def fmt_z(z: float) -> str:
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
    table.add_column("Sector", width=10)
    table.add_column("Composite", justify="right", width=10)
    table.add_column("Size", justify="right", width=7)
    table.add_column("Rev", justify="right", width=7)
    table.add_column("Mom", justify="right", width=7)
    table.add_column("Val", justify="right", width=7)
    table.add_column("Vol", justify="right", width=7)
    table.add_column("Qual", justify="right", width=7)
    table.add_column("Fund", justify="right", width=7)
    table.add_column("D/A", justify="right", width=7)

    sorted_scores = sorted(scores, key=lambda s: s.composite_score, reverse=True)
    for i, s in enumerate(sorted_scores[:top_n], 1):
        table.add_row(
            str(i), s.symbol, s.sector,
            fmt_composite(s.composite_score),
            fmt_z(s.size_z), fmt_z(s.reversal_z), fmt_z(s.momentum_z),
            fmt_z(s.value_z), fmt_z(s.volatility_z), fmt_z(s.quality_z),
            fmt_z(s.funding_z), fmt_z(s.dead_alive_z),
        )
    return table


def build_da_table(scores: list, top_n: int = 15) -> Table:
    """Dead/Alive factor leaderboard."""
    table = Table(
        title="💀 Dead/Alive Factor — Long (Alive) vs Short (Dead)",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Rank", width=5, justify="right")
    table.add_column("Token", width=10, style="bold white")
    table.add_column("Sector", width=10)
    table.add_column("D/A Score", justify="right", width=12)
    table.add_column("Side", width=8)

    sorted_scores = sorted(scores, key=lambda s: s.dead_alive_z, reverse=True)
    for i, s in enumerate(sorted_scores[:top_n], 1):
        table.add_row(str(i), s.symbol, s.sector, fmt_z(s.dead_alive_z), "[green]ALIVE[/]")
    table.add_row("", "", "", "", "", end_section=True)
    for i, s in enumerate(reversed(sorted_scores[-top_n:]), 1):
        table.add_row(str(i), s.symbol, s.sector, fmt_z(s.dead_alive_z), "[red]DEAD[/]")
    return table


def build_momentum_term_structure(scores: list) -> Table:
    table = Table(
        title="📈 Momentum Term Structure",
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

    sorted_scores = sorted(scores, key=lambda s: s.momentum_z, reverse=True)
    for s in sorted_scores[:20]:
        table.add_row(
            s.symbol,
            fmt_z(s.momentum_7d), fmt_z(s.momentum_14d), fmt_z(s.momentum_21d),
            fmt_z(s.momentum_42d), fmt_z(s.momentum_63d), fmt_z(s.momentum_z),
        )
    return table


def build_sector_breakdown(scores: list) -> Table:
    table = Table(
        title="🏭 Sector Factor Exposure",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Sector", style="bold white", width=14)
    table.add_column("N", justify="right", width=4)
    table.add_column("Composite", justify="right", width=10)
    table.add_column("Momentum", justify="right", width=10)
    table.add_column("Quality", justify="right", width=10)
    table.add_column("D/A", justify="right", width=10)

    sector_scores = {}
    for s in scores:
        sector_scores.setdefault(s.sector, []).append(s)

    for sector in sorted(sector_scores.keys()):
        group = sector_scores[sector]
        n = len(group)
        avg = lambda attr: sum(getattr(s, attr, 0) for s in group) / n if n > 0 else 0
        table.add_row(
            sector, str(n),
            fmt_composite(avg("composite_score")),
            fmt_z(avg("momentum_z")),
            fmt_z(avg("quality_z")),
            fmt_z(avg("dead_alive_z")),
        )
    return table


async def main(args):
    console = Console()
    console.print("\n[bold white]🔬 Crypto Factor Model v2 — Q-7 + Dead/Alive + Enhanced Momentum[/]\n")

    fetcher = DataFetcher()
    model = FactorModel()

    try:
        with console.status("[bold cyan]Building universe (rate-limited, ~30s)...[/]"):
            tokens = await build_universe(fetcher, max_tokens=args.max_tokens)

        if not tokens:
            console.print("[red]Failed to build universe.[/]")
            return

        tokens_with_data = [t for t in tokens if len(t.prices) >= 30]
        console.print(f"[green]✓[/] {len(tokens_with_data)} tokens with sufficient history")

        with console.status("[bold cyan]Computing factor exposures...[/]"):
            scores = model.compute_factors(tokens_with_data)

        console.print(f"[green]✓[/] Computed {len(scores)} factor score vectors\n")

        if args.factor:
            if args.factor == "dead_alive":
                table = build_da_table(scores, top_n=args.top)
            else:
                # Generic factor view
                attr = f"{args.factor}_z"
                sorted_scores = sorted(scores, key=lambda s: getattr(s, attr, 0), reverse=True)
                table = Table(title=f"📊 {args.factor.upper()} Factor", box=box.SIMPLE_HEAVY,
                              show_header=True, header_style="bold cyan", expand=True)
                table.add_column("Rank", width=5, justify="right")
                table.add_column("Token", width=12, style="bold white")
                table.add_column("Sector", width=10)
                table.add_column("Z-Score", justify="right", width=12)
                for i, s in enumerate(sorted_scores[:args.top], 1):
                    z = getattr(s, attr, 0)
                    side = "[green]LONG[/]" if z > 0 else "[red]SHORT[/]"
                    table.add_row(str(i), s.symbol, s.sector, fmt_z(z), side)
            console.print(table)
        else:
            console.print(build_composite_table(scores, top_n=args.top))
            console.print("")
            bottom = Columns([build_sector_breakdown(scores), build_momentum_term_structure(scores)])
            console.print(bottom)
            console.print("")
            console.print(build_da_table(scores, top_n=10))

    finally:
        await fetcher.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Factor Model v2 Dashboard")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=25)
    parser.add_argument("--factor", type=str, default=None,
                        choices=["size", "reversal", "momentum", "value", "volatility",
                                 "quality", "funding", "dead_alive"])
    args = parser.parse_args()
    asyncio.run(main(args))
