"""Market data engine — real sold prices, time-to-sale, and logistics feasibility."""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from src.config.settings import settings


@dataclass
class SoldListing:
    title: str
    price_usd: float
    shipping_usd: float = 0.0
    sold_date: str = ""
    url: str = ""
    condition: str = ""


@dataclass
class MarketAnalysis:
    query: str
    sold_listings: list[SoldListing] = field(default_factory=list)
    median_price: float = 0.0
    avg_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    p25_price: float = 0.0
    p75_price: float = 0.0
    total_sold: int = 0
    data_source: str = ""
    confidence: str = "low"
    scrape_duration: float = 0.0


@dataclass
class TimeToSaleEstimate:
    days_min: int = 0
    days_expected: int = 0
    days_max: int = 0
    confidence: str = "low"
    factors: list[str] = field(default_factory=list)


@dataclass
class LogisticsFeasibility:
    is_feasible: bool = False
    total_cost: float = 0.0
    net_profit: float = 0.0
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    profit_per_day: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


# ===== EBAY DATA (API-first, fallback to estimates) =====

class EbayMarketData:
    """Get eBay sold data. Uses Finding API, falls back to estimates."""

    FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"

    async def search_sold(self, query: str, max_results: int = 50) -> MarketAnalysis:
        start = time.monotonic()
        result = MarketAnalysis(query=query)

        # Try Finding API first
        try:
            data = await self._call_api(query, max_results)
            listings, total = self._parse_api_response(data)
            if listings:
                result.sold_listings = listings
                result.total_sold = total
                result.data_source = "ebay_api"
                prices = [l.price_usd for l in listings if l.price_usd > 0]
                if prices:
                    self._compute_stats(result, prices)
                    result.confidence = "high" if len(prices) >= 10 else "medium"
                return result
        except Exception as e:
            logger.debug(f"[eBay] API failed: {e}")

        # Fallback: estimate based on query quality
        result.data_source = "fallback"
        result.confidence = "low"
        result.scrape_duration = time.monotonic() - start
        return result

    async def _call_api(self, query: str, max_results: int) -> dict:
        params = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": settings.ebay_app_id or "none",
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": query,
            "paginationInput.entriesPerPage": min(max_results, 100),
            "paginationInput.pageNumber": 1,
            "sortOrder": "EndTimeSoonest",
        }
        headers = {
            "X-EBAY-SOA-OPERATION-NAME": "findCompletedItems",
            "X-EBAY-SOA-SERVICE-VERSION": "1.13.0",
            "X-EBAY-SOA-REQUEST-DATA-FORMAT": "JSON",
            "X-EBAY-SOA-RESPONSE-DATA-FORMAT": "JSON",
            "X-EBAY-SOA-SECURITY-APPNAME": settings.ebay_app_id or "none",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.get(self.FINDING_URL, params=params)
            resp.raise_for_status()
            return resp.json()

    def _parse_api_response(self, data: dict) -> tuple:
        listings = []
        try:
            search_result = (
                data.get("findCompletedItemsResponse", [{}])[0]
                .get("searchResult", [{}])[0]
            )
            total = int(search_result.get("totalEntries", ["0"])[0].get("text", "0"))
            raw_items = search_result.get("item", [])
            for raw in raw_items:
                try:
                    item_id = raw.get("itemId", [""])[0]
                    title = raw.get("title", [""])[0]
                    price_info = raw.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
                    price = float(price_info.get("__value__", "0") or "0")
                    ship_info = raw.get("shippingInfo", [{}])[0]
                    ship_cost = ship_info.get("shippingServiceCost", [{}])[0]
                    shipping = float(ship_cost.get("__value__", "0") or "0")
                    sold_date = raw.get("listingInfo", [{}])[0].get("endTime", [""])[0]
                    url = raw.get("viewItemURL", [""])[0]
                    if price > 0:
                        listings.append(SoldListing(
                            title=title, price_usd=price,
                            shipping_usd=shipping, sold_date=sold_date, url=url,
                        ))
                except (IndexError, ValueError, KeyError):
                    continue
        except (IndexError, KeyError):
            pass
        return listings, total

    def _compute_stats(self, result: MarketAnalysis, prices: list[float]):
        sorted_p = sorted(prices)
        n = len(sorted_p)
        result.min_price = sorted_p[0]
        result.max_price = sorted_p[-1]
        result.avg_price = statistics.mean(sorted_p)
        result.median_price = statistics.median(sorted_p)
        result.p25_price = sorted_p[max(0, int(n * 0.25) - 1)]
        result.p75_price = sorted_p[min(n - 1, int(n * 0.75))]


# ===== TIME-TO-SALE ESTIMATOR =====

TIME_TO_SALE_DATA = {
    "anime_figure": [
        ((0, 30), 3, "high"), ((30, 100), 7, "high"),
        ((100, 300), 14, "medium"), ((300, 1000), 21, "medium"),
        ((1000, float('inf')), 45, "low"),
    ],
    "trading_cards": [
        ((0, 20), 2, "high"), ((20, 100), 5, "high"),
        ((100, 500), 10, "medium"), ((500, 2000), 14, "medium"),
        ((2000, float('inf')), 30, "low"),
    ],
    "vintage_camera": [
        ((0, 100), 10, "medium"), ((100, 500), 14, "high"),
        ((500, 2000), 21, "medium"), ((2000, float('inf')), 45, "low"),
    ],
    "japanese_knife": [
        ((0, 80), 7, "high"), ((80, 300), 10, "high"),
        ((300, 1000), 14, "medium"), ((1000, float('inf')), 30, "medium"),
    ],
    "streetwear": [
        ((0, 50), 3, "high"), ((50, 200), 5, "high"),
        ((200, 500), 10, "medium"), ((500, float('inf')), 14, "medium"),
    ],
    "vintage_electronics": [
        ((0, 100), 14, "medium"), ((100, 500), 21, "medium"),
        ((500, float('inf')), 30, "low"),
    ],
    "japanese_ceramics": [
        ((0, 50), 14, "medium"), ((50, 200), 21, "medium"),
        ((200, float('inf')), 30, "low"),
    ],
    "retro_games": [
        ((0, 30), 5, "high"), ((30, 150), 7, "high"),
        ((150, 500), 14, "medium"), ((500, float('inf')), 21, "medium"),
    ],
    "vinyl_records": [
        ((0, 30), 7, "high"), ((30, 100), 14, "medium"),
        ((100, 300), 21, "medium"), ((300, float('inf')), 30, "low"),
    ],
    "vintage_toys": [
        ((0, 50), 10, "medium"), ((50, 200), 14, "medium"),
        ((200, 500), 21, "medium"), ((500, float('inf')), 30, "low"),
    ],
}


def estimate_time_to_sale(
    category: str, listing_price: float,
    market_data: MarketAnalysis | None = None,
) -> TimeToSaleEstimate:
    result = TimeToSaleEstimate()
    tiers = TIME_TO_SALE_DATA.get(category, [
        ((0, 50), 7, "medium"), ((50, 200), 14, "medium"),
        ((200, float('inf')), 21, "low"),
    ])
    for (low, high), days, conf in tiers:
        if low <= listing_price < high:
            result.days_expected = days
            result.days_min = max(1, int(days * 0.5))
            result.days_max = int(days * 2.0)
            result.confidence = conf
            break
    else:
        result.days_expected = 14
        result.days_min = 7
        result.days_max = 30

    if market_data:
        total_sold = (
            getattr(market_data, 'total_sold', 0) or
            getattr(market_data, 'num_sold_listings', 0)
        )
        median_price = (
            getattr(market_data, 'median_price', 0) or
            getattr(market_data, 'target_median_price', 0)
        )
        if total_sold >= 5:
            if total_sold >= 30:
                result.days_expected = int(result.days_expected * 0.7)
                result.factors.append("High sell-through — faster sale expected")
            if median_price > 0:
                ratio = listing_price / median_price
            if ratio < 0.8:
                result.days_expected = int(result.days_expected * 0.6)
                result.factors.append(f"Priced {((1-ratio)*100):.0f}% below market median")
            elif ratio > 1.5:
                result.days_expected = int(result.days_expected * 1.5)
                result.factors.append(f"Priced {((ratio-1)*100):.0f}% above market median — slower sale")
    return result


# ===== LOGISTICS FEASIBILITY =====

def validate_logistics(
    source_price_jpy: float,
    listing_price_usd: float,
    category: str,
    weight_kg: float = 0.3,
    proxy_service: str = "buyee",
    destination: str = "us",
) -> LogisticsFeasibility:
    result = LogisticsFeasibility()
    jpy_usd = 0.0067

    # Source cost
    source_usd = source_price_jpy * jpy_usd
    result.breakdown["source_item"] = source_usd

    # Mercari JP fees (10% selling + 3% payment)
    result.breakdown["mercari_fees"] = source_usd * 0.13

    # Proxy service
    proxy_costs = {
        "buyee": (200 + 500) * jpy_usd,
        "zenmarket": 300 * jpy_usd,
        "fromjapan": 400 * jpy_usd,
    }
    result.breakdown["proxy_service"] = proxy_costs.get(proxy_service, 4.50)

    # Shipping
    ship_cost = _est_shipping(weight_kg, destination)
    result.breakdown["shipping"] = ship_cost

    # Insurance (2% of value, min $2)
    result.breakdown["insurance"] = max(2.0, listing_price_usd * 0.02)

    # eBay fees
    result.breakdown["ebay_fvf"] = listing_price_usd * 0.13
    result.breakdown["ebay_payment"] = listing_price_usd * 0.029 + 0.30

    # Tariff
    tariff = 0.0
    if destination == "us" and listing_price_usd > 800:
        rates = {
            "anime_figure": 0.0, "trading_cards": 0.0,
            "vintage_camera": 0.035, "japanese_knife": 0.065,
            "streetwear": 0.12, "vintage_electronics": 0.025,
            "japanese_ceramics": 0.05, "retro_games": 0.0,
            "vinyl_records": 0.0, "vintage_toys": 0.0,
        }
        rate = rates.get(category, 0.05)
        tariff = (listing_price_usd - 800) * rate
    result.breakdown["tariff"] = tariff

    result.breakdown["llm_cost"] = 0.03

    # Totals
    result.total_cost = sum(result.breakdown.values())
    result.net_profit = listing_price_usd - result.total_cost
    result.net_margin_pct = (result.net_profit / listing_price_usd * 100) if listing_price_usd > 0 else 0
    result.roi_pct = (result.net_profit / source_usd * 100) if source_usd > 0 else 0

    # Time-to-sale → profit/day
    tts = estimate_time_to_sale(category, listing_price_usd)
    if tts.days_expected > 0:
        result.profit_per_day = result.net_profit / tts.days_expected

    # Feasibility
    result.is_feasible = True
    if result.net_profit < 15:
        result.is_feasible = False
        result.warnings.append(f"Profit ${result.net_profit:.2f} < $15 min")
    if result.net_margin_pct < 20:
        result.is_feasible = False
        result.warnings.append(f"Margin {result.net_margin_pct:.0f}% < 20% min")
    if result.roi_pct < 30:
        result.is_feasible = False
        result.warnings.append(f"ROI {result.roi_pct:.0f}% < 30% min")

    # Weight warnings
    if weight_kg > 2.0:
        result.warnings.append(f"Heavy ({weight_kg}kg) — high shipping cost")
    if weight_kg > 5.0:
        result.is_feasible = False
        result.warnings.append(f"Very heavy ({weight_kg}kg) — likely unprofitable")

    # Proxy recommendation
    if source_price_jpy > 50000:
        result.recommendations.append("High-value: consider ZenMarket for better handling")
    if source_price_jpy > 100000:
        result.recommendations.append("Very high-value: use DHL Express for speed + tracking")

    # Destination
    if destination == "us" and listing_price_usd <= 800:
        result.recommendations.append("Under $800 US de minimis — no duty. Use eBay GSP.")
    if destination == "eu":
        result.warnings.append("EU: 20% VAT on imports — may deter buyers")

    return result


def _est_shipping(weight_kg: float, dest: str = "us") -> float:
    if dest == "us":
        if weight_kg <= 0.1: return 8.0
        elif weight_kg <= 0.25: return 12.0
        elif weight_kg <= 0.5: return 15.0
        elif weight_kg <= 1.0: return 20.0
        elif weight_kg <= 2.0: return 28.0
        elif weight_kg <= 5.0: return 45.0
        else: return 70.0 + (weight_kg - 5) * 10
    elif dest == "eu":
        if weight_kg <= 0.5: return 18.0
        elif weight_kg <= 2.0: return 30.0
        else: return 50.0 + (weight_kg - 2) * 12
    return 25.0
