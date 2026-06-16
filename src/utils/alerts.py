"""Telegram alerting — lightweight, phone-friendly alerts with translation and platform advice.

Alert types:
1. HOT CANDIDATE — high-score item with full analysis + where to sell + shipping/tariff
2. CANDIDATE — viable item, review when convenient
3. DELIST — source item sold, eBay listing removed
4. SUMMARY — periodic scan results summary
5. ERROR — system errors requiring attention

All alerts are in English with Japanese original titles noted.
"""

from __future__ import annotations

import html as _html
import httpx
from loguru import logger
from src.config.settings import settings


# Japanese → English category name mapping
CAT_EN = {
    "anime_figure": "Anime Figure",
    "vintage_camera": "Vintage Camera",
    "japanese_knife": "Japanese Knife",
    "streetwear": "Streetwear",
    "trading_cards": "Trading Cards",
    "vintage_electronics": "Vintage Electronics",
    "japanese_ceramics": "Japanese Ceramics",
    "retro_games": "Retro Games",
    "vinyl_records": "Vinyl Records",
    "vintage_toys": "Vintage Toys",
}


class TelegramAlerter:
    """Lightweight Telegram alerts optimized for phone monitoring."""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self._enabled = bool(self.bot_token and self.chat_id)
        self._base_url = (
            f"https://api.telegram.org/bot{self.bot_token}"
            if self.bot_token else ""
        )

    async def send(self, message: str, silent: bool = False) -> bool:
        if not self._enabled:
            logger.debug(f"[Telegram] Would send: {message[:120]}...")
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_notification": silent,
                    },
                )
                if resp.status_code != 200:
                    logger.error(f"[Telegram] HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    data = resp.json()
                    if not data.get("ok"):
                        logger.error(f"[Telegram] API error: {data.get('description', 'unknown')}")
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"[Telegram] Send failed: {e}")
            return False

    async def send_candidate(
        self,
        title: str,
        title_original: str,
        brand: str,
        category: str,
        source_price_jpy: float,
        source_price_usd: float,
        listing_price: float,
        net_profit: float,
        net_margin_pct: float,
        roi_pct: float,
        profit_per_day: float,
        days_to_sell: float,
        composite_score: float,
        priority: str,
        source_url: str,
        platform_name: str = "",
        platform_url: str = "",
        shipping_est: str = "",
        tariff_note: str = "",
        weight_kg: float = 0.3,
        logistics_feasible: bool = True,
        ebay_median: float = 0,
        ebay_sold_count: int = 0,
        condition: str = "",
        seller_rating: float = 0,
        warnings: list[str] | None = None,
        cost_breakdown: dict[str, float] | None = None,
    ) -> bool:
        """Send a rich candidate alert with translation, platform, shipping & tariff info."""
        # Escape HTML in user-provided strings
        title = _html.escape(title)
        brand = _html.escape(brand) if brand else ""

        prio_emoji = {"high": "🟢", "medium": "🟡", "low": "🟠"}.get(priority, "⚪")
        cat_en = CAT_EN.get(category, category.replace("_", " ").title())
        cat_emoji = {
            "anime_figure": "🎎", "vintage_camera": "📷", "japanese_knife": "🔪",
            "streetwear": "👕", "trading_cards": "🃏", "vintage_electronics": "📻",
            "japanese_ceramics": "🏺", "retro_games": "🎮", "vinyl_records": "💿",
            "vintage_toys": "🧸",
        }.get(category, "📦")

        lines = [
            f"{prio_emoji} <b>{priority.upper()} CANDIDATE</b> {cat_emoji} {cat_en}",
            "",
            f"📦 <b>{self._truncate(title, 80)}</b>",
        ]

        # Show original Japanese title if different
        if title_original and title_original != title:
            lines.append(f"   🇯🇵 JP: {self._truncate(_html.escape(title_original), 70)}")

        if brand:
            lines.append(f"🏷 {brand}")

        lines += [
            "",
            "<b>── PRICING ──</b>",
            f"💰 Buy:  ¥{source_price_jpy:,.0f}  (${source_price_usd:.2f}) Mercari JP",
            f"🏷 Sell: ${listing_price:.2f}  ({cat_en})",
        ]
        if ebay_median > 0:
            lines.append(f"📊 Market: ${ebay_median:.0f}  ({ebay_sold_count} sold)")
        else:
            lines.append(f"📊 Market: Est. ${listing_price:.2f} (no eBay data)")

        lines += [
            "",
            "<b>── PROFIT ──</b>",
            f"💵 Net:  ${net_profit:.2f}  ({net_margin_pct:.0f}% margin)",
            f"📈 ROI:  {roi_pct:.0f}%",
            f"⚡ ${profit_per_day:.2f}/day  •  ~{days_to_sell:.0f} to sell",
        ]

        # Logistics feasibility + cost breakdown
        if logistics_feasible:
            lines += [
                "",
                "<b>── LOGISTICS ✅</b>",
                f"📦 Ship: {shipping_est.split(chr(10))[0] if shipping_est else 'ePacket ~$12'}",
                f"🌍 Tariff: {tariff_note.split(chr(10))[0] if tariff_note else 'No duty (under $800)'}",
            ]
        else:
            lines += [
                "",
                "<b>── LOGISTICS ❌</b>",
                "⚠️ Margins too thin after all costs",
            ]

        # Cost breakdown
        if cost_breakdown:
            lines += [
                "",
                "<b>── COST BREAKDOWN ──</b>",
            ]
            for key, val in cost_breakdown.items():
                label = {
                    "source_item": "Item", "mercari_fees": "Mercari fees",
                    "proxy_service": "Proxy service", "shipping": "Shipping",
                    "insurance": "Insurance", "ebay_fvf": "eBay FVF",
                    "ebay_payment": "eBay payment", "tariff": "Tariff",
                    "llm_cost": "Listing",
                }.get(key, key)
                lines.append(f"  {label}: ${val:.2f}")

        # Where to sell
        if platform_name:
            lines += [
                "",
                "<b>── SELL ON ──</b>",
                f"🎯 {platform_name}",
            ]
            if platform_url:
                lines.append(f"🔗 {platform_url}")

        # Shipping estimate
        if shipping_est:
            lines += [
                "",
                "<b>── SHIPPING (JP → US) ──</b>",
                shipping_est,
            ]

        # Tariff note
        if tariff_note:
            lines += [
                "",
                "<b>── TARIFF ──</b>",
                tariff_note,
            ]

        if condition or seller_rating:
            lines += [
                "",
                "<b>── DETAILS ──</b>",
            ]
            if condition:
                lines.append(f"📋 Condition: {condition}")
            if seller_rating:
                lines.append(f"⭐ Seller: {seller_rating:.1f}/5.0")

        lines.append("")
        lines.append(f"🎯 Score: {composite_score:.0f}/100")

        if warnings:
            lines.append("")
            lines.append("<b>── WARNINGS ──</b>")
            for w in warnings[:3]:
                lines.append(f"⚠️ {self._truncate(_html.escape(w), 90)}")

        lines.append("")
        lines.append(f"🔗 <a href='{source_url}'>Mercari JP Listing</a>")

        msg = "\n".join(lines)
        return await self.send(msg)

    async def send_scan_summary(
        self, query: str, total_scraped: int, total_viable: int,
        total_rejected: int, top_candidates: list, duration_seconds: float,
    ) -> bool:
        lines = [
            f"📊 <b>Scan: {query}</b>",
            f"🔍 {total_scraped} found  ✅ {total_viable} viable  ❌ {total_rejected} rejected  ⏱️ {duration_seconds:.0f}s",
        ]
        if top_candidates:
            lines.append("")
            lines.append("<b>TOP PICKS:</b>")
            for i, c in enumerate(top_candidates[:5], 1):
                prio = {"high": "🟢", "medium": "🟡", "low": "🟠"}.get(c["priority"], "⚪")
                lines.append(f"{i}. {prio} {self._truncate(c.get('title',''), 40)}")
                lines.append(f"   ${c.get('net_profit',0):.0f} profit • {c.get('roi_pct',0):.0f}% ROI • Score {c.get('composite_score',0):.0f}")
        return await self.send("\n".join(lines), silent=True)

    async def send_delist(self, title: str, source_url: str, target_url: str = "", reason: str = "Source sold") -> bool:
        lines = [
            "⚠️ <b>Auto-Delisted</b>",
            "",
            f"📦 {_html.escape(self._truncate(title, 80))}",
            f"📋 {reason}",
        ]
        if target_url:
            lines.append(f"🔗 eBay: {target_url}")
        lines.append(f"🔗 Source: {source_url}")
        return await self.send("\n".join(lines))

    async def send_error(self, error: str, context: str = "") -> bool:
        msg = f"🚨 <b>Error</b>\n\n{_html.escape(self._truncate(error, 400))}"
        if context:
            msg = f"<i>{_html.escape(context)}</i>\n\n{msg}"
        return await self.send(msg)

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."
    async def send_arb_opportunity(self, opp) -> bool:
        """Send a Hotstuff arbitrage opportunity alert."""
        dir_text = "SHORT Hotstuff / LONG other" if opp.direction == "short_hotstuff" else "LONG Hotstuff / SHORT other"
        profit_emoji = "✅" if opp.fee_analysis and opp.fee_analysis.is_profitable else "❌"

        lines = [
            f"🔥 <b>{opp.symbol}</b> ({opp.category}) Score: {opp.score:.0f}/100 {profit_emoji}",
            f"",
            f"📊 <b>Prices</b>",
            f"  Hotstuff: {opp.hotstuff_mark:.2f} (bid {opp.hotstuff_bid:.2f} / ask {opp.hotstuff_ask:.2f})",
            f"  {opp.reference_venue}: {opp.reference_price:.2f}",
            f"",
            f"📈 <b>Divergence</b>: {opp.divergence_bps:+.1f} bps ({opp.divergence_usd:+.2f})",
            f"🔄 <b>Direction</b>: {dir_text}",
            f"",
            f"💰 <b>Fees & Edge</b>",
        ]

        if opp.fee_analysis:
            lines.extend([
                f"  Net edge: {opp.fee_analysis.net_edge_bps:+.1f} bps",
                f"  Profit/$1k: ${opp.net_profit_per_1k:.2f}",
                f"  Total fees: ${opp.fee_analysis.total_fees_usd:.4f}",
            ])

        lines.extend([
            f"",
            f"📉 <b>Market</b>",
            f"  Funding 8h: {opp.funding_8h:+.6f}",
            f"  Spread: {opp.spread_bps:.4f}%",
            f"  Vol 24h: ${opp.volume_24h:,.0f}",
            f"  OI: {opp.open_interest:,.1f}",
        ])

        if opp.notes:
            lines.append(f"")
            lines.append(f"⚠️ {' | '.join(opp.notes)}")

        return await self.send("\n".join(lines))

    async def send_arb_summary(self, opportunities: list, scan_time: float = 0) -> bool:
        """Send a summary of top arbitrage opportunities."""
        if not opportunities:
            return await self.send("📊 Hotstuff Arb Scan: No opportunities found.")

        profitable = [o for o in opportunities if o.fee_analysis and o.fee_analysis.is_profitable]

        lines = [
            f"📊 <b>Hotstuff Arb Scan</b> — {len(opportunities)} found, {len(profitable)} profitable",
        ]

        if scan_time > 0:
            lines.append(f"⏱ Scan time: {scan_time:.1f}s")

        lines.append("")

        for i, opp in enumerate(opportunities[:5], 1):
            profit_emoji = "✅" if opp.fee_analysis and opp.fee_analysis.is_profitable else "❌"
            dir_text = "SHORT" if opp.direction == "short_hotstuff" else "LONG"
            lines.append(
                f"{i}. {profit_emoji} <b>{opp.symbol}</b> | {opp.divergence_bps:+.0f}bps | "
                f"{dir_text} | ${opp.net_profit_per_1k:.2f}/$1k | Score {opp.score:.0f}"
            )

        return await self.send("\n".join(lines))
