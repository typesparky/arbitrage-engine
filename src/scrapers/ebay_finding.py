"""eBay Finding API client for price comparison.

Uses the eBay Finding API (free, no auth required for basic searches)
to find sold/completed listings for price discovery.

Docs: https://developer.ebay.com/api-docs/user-guides/static/finding-user-guide-landing.html
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx
from loguru import logger

from src.config.settings import settings
from src.models.database import EbaySoldListing

# eBay Finding API endpoint
EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"

# Sandbox vs production
if settings.ebay_sandbox:
    EBAY_FINDING_URL = "https://svcs.sandbox.ebay.com/services/search/FindingService/v1"


@dataclass
class EbaySoldResult:
    """A single eBay sold/completed listing."""

    external_id: str
    title: str
    price: float
    shipping_cost: float
    condition: str
    sold_at: str | None
    url: str


@dataclass
class EbayPriceAnalysis:
    """Aggregated price analysis for a search query."""

    query: str
    median_price: float
    avg_price: float
    min_price: float
    max_price: float
    total_sold: int
    items: list[EbaySoldResult]

    @property
    def suggested_listing_price(self) -> float:
        """Suggest listing price at 75th percentile (competitive but profitable)."""
        if not self.items:
            return 0.0
        sorted_prices = sorted(item.price for item in self.items)
        idx = int(len(sorted_prices) * 0.75)
        return sorted_prices[min(idx, len(sorted_prices) - 1)]




class EbayFindingClient:
    """Client for eBay Finding API — sold listings price discovery."""

    def __init__(self):
        self.app_id = settings.ebay_app_id or "none"  # Finding API works without app_id for basic use
        self._client: httpx.AsyncClient | None = None
        self._request_count = 0
        self._last_request_time = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "X-EBAY-SOA-OPERATION-NAME": "findCompletedItems",
                    "X-EBAY-SOA-SERVICE-VERSION": "1.13.0",
                    "X-EBAY-SOA-REQUEST-DATA-FORMAT": "JSON",
                    "X-EBAY-SOA-RESPONSE-DATA-FORMAT": "JSON",
                    "X-EBAY-SOA-SECURITY-APPNAME": self.app_id,
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def _rate_limit(self) -> None:
        """eBay Finding API: ~5000 calls/day. ~1 second between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        self._last_request_time = time.monotonic()

    async def search_sold(
        self,
        query: str,
        category_id: str = "",
        max_results: int = 50,
        condition: str = "",
    ) -> EbayPriceAnalysis:
        """Search eBay completed/sold listings for price discovery.

        Args:
            query: Search keywords
            category_id: eBay category ID to filter
            max_results: Max results to return (max 100)
            condition: Condition filter (New, Used, etc.)
        """
        await self._rate_limit()

        params: dict[str, Any] = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": query,
            "paginationInput.entriesPerPage": min(max_results, 100),
            "paginationInput.pageNumber": 1,
            "sortOrder": "EndTimeSoonest",
            "itemFilter(0).name": "SoldItemsOnly",
            "itemFilter(0).value": "true",
        }

        filter_idx = 1
        if category_id:
            params[f"itemFilter({filter_idx}).name"] = "CategoryID"
            params[f"itemFilter({filter_idx}).value"] = category_id
            filter_idx += 1

        if condition:
            params[f"itemFilter({filter_idx}).name"] = "Condition"
            params[f"itemFilter({filter_idx}).value"] = condition
            filter_idx += 1

        try:
            client = await self._get_client()
            response = await client.get(EBAY_FINDING_URL, params=params)
            response.raise_for_status()
            data = response.json()

            return self._parse_response(query, data)

        except Exception as e:
            logger.error(f"[eBay Finding] Search failed for '{query}': {e}")
            return EbayPriceAnalysis(
                query=query,
                median_price=0,
                avg_price=0,
                min_price=0,
                max_price=0,
                total_sold=0,
                items=[],
            )

    def _parse_response(self, query: str, data: dict) -> EbayPriceAnalysis:
        """Parse eBay Finding API response into EbayPriceAnalysis."""
        items: list[EbaySoldResult] = []

        try:
            search_result = (
                data.get("findCompletedItemsResponse", [{}])[0]
                .get("searchResult", [{}])[0]
            )
            total = int(search_result.get("totalEntries", [{}])[0].get("text", "0"))

            raw_items = search_result.get("item", [])
            for raw in raw_items:
                try:
                    item_id = raw.get("itemId", [""])[0]
                    title = raw.get("title", [""])[0]

                    # Price
                    selling = raw.get("sellingStatus", [{}])[0]
                    price_info = selling.get("currentPrice", [{}])[0]
                    price = float(price_info.get("text", "0").replace("$", "").replace(",", ""))

                    # Shipping
                    shipping_info = raw.get("shippingInfo", [{}])[0]
                    ship_cost = shipping_info.get("shippingServiceCost", [{}])[0]
                    shipping = float(ship_cost.get("text", "0").replace("$", "").replace(",", ""))

                    # Condition
                    cond = raw.get("condition", [{}])[0]
                    condition = cond.get("conditionDisplayName", [""])[0]

                    # End time (sold date)
                    end_time = raw.get("listingInfo", [{}])[0].get("endTime", [""])[0]

                    # URL
                    url = raw.get("viewItemURL", [""])[0]

                    items.append(
                        EbaySoldResult(
                            external_id=item_id,
                            title=title,
                            price=price,
                            shipping_cost=shipping,
                            condition=condition,
                            sold_at=end_time,
                            url=url,
                        )
                    )
                except (IndexError, ValueError, KeyError) as e:
                    logger.debug(f"[eBay Finding] Skipped malformed item: {e}")
                    continue

        except (IndexError, KeyError) as e:
            logger.error(f"[eBay Finding] Failed to parse response: {e}")

        # Calculate stats
        prices = [item.price for item in items]
        if prices:
            sorted_prices = sorted(prices)
            n = len(sorted_prices)
            median = sorted_prices[n // 2] if n % 2 else (
                sorted_prices[n // 2 - 1] + sorted_prices[n // 2]
            ) / 2
            avg = sum(prices) / n
        else:
            median = avg = 0

        return EbayPriceAnalysis(
            query=query,
            median_price=median,
            avg_price=avg,
            min_price=min(prices) if prices else 0,
            max_price=max(prices) if prices else 0,
            total_sold=total,
            items=items,
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
