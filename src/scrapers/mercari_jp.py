"""Mercari Japan scraper — multi-strategy approach.

Strategy priority (fastest → most reliable):
1. Internal API (direct HTTP, no browser) — fastest, works for search + item pages
2. Crawl4AI + Camoufox (stealth browser) — for when API is blocked
3. Cloudflare Bypass middleware — if Mercari adds CF protection
4. Scrapfly API — last resort, paid but reliable

The internal API is Mercari's own REST API used by their web/mobile apps.
It's the most reliable approach and doesn't need a browser at all.
We fall back to browser-based scraping only when the API is blocked.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from src.config.settings import settings
from src.scrapers.base import BaseScraper, ScrapeResult, ScrapedItem
from src.scrapers.crawl4ai_engine import Crawl4AIEngine, Crawl4AIOptions, MERCARI_ITEM_SCHEMA

# Mercari JP endpoints
MERCARI_API_SEARCH = "https://api.mercari.jp/v2/items/search"
MERCARI_API_ITEM = "https://api.mercari.jp/v2/items/{}"
MERCARI_WEB_ITEM = "https://jp.mercari.com/item/{}"
MERCARI_WEB_SEARCH = "https://jp.mercari.com/search"

# Headers that mimic the Mercari web app
MERCARI_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://jp.mercari.com",
    "Referer": "https://jp.mercari.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "X-Platform": "web",
}

# CSS selectors for browser-based fallback extraction
MERCARI_CSS_SELECTORS = {
    "base": "div[class*='item'], section[class*='item'], li[class*='item']",
    "title": "h2, h3, span[class*='title'], div[class*='name']",
    "price": "span[class*='price'], div[class*='price']",
    "image": "img[src]",
}


class MercariJPScraper(BaseScraper):
    """Multi-strategy Mercari Japan scraper."""

    PLATFORM = "mercari_jp"

    def __init__(self, proxy_url: str = "", user_agent: str = ""):
        super().__init__(
            proxy_url=proxy_url or settings.proxy_url,
            user_agent=user_agent or settings.user_agent,
        )
        self._http_client: httpx.AsyncClient | None = None
        self._crawl4ai: Crawl4AIEngine | None = None
        self._request_count = 0
        self._last_request_time = 0.0
        self._api_blocked = False  # Set to True when API starts returning errors

    # --- Strategy 1: Internal API (fastest) ---

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            headers = {**MERCARI_API_HEADERS, "User-Agent": self.user_agent}
            mounts = {}
            if self.proxy_url:
                mounts = {"all://": httpx.AsyncHTTPTransport(proxy=self.proxy_url)}
            self._http_client = httpx.AsyncClient(
                headers=headers, timeout=30.0, follow_redirects=True, mounts=mounts
            )
        return self._http_client

    async def _rate_limit(self) -> None:
        """~2 second between API requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < 2.0:
            await asyncio.sleep(2.0 - elapsed)
        self._last_request_time = time.monotonic()
        self._request_count += 1

    async def _api_request(self, url: str, params: dict | None = None) -> dict[str, Any]:
        """Make a rate-limited API request."""
        await self._rate_limit()
        client = await self._get_http_client()
        response = await client.get(url, params=params)

        if response.status_code in (403, 429, 503):
            self._api_blocked = True
            raise Exception(f"API blocked: HTTP {response.status_code}")

        response.raise_for_status()
        return response.json()

    # --- Strategy 2: Crawl4AI browser (fallback) ---

    async def _get_crawl4ai(self, use_camoufox: bool = False) -> Crawl4AIEngine:
        if self._crawl4ai is None:
            options = Crawl4AIOptions(
                headless=True,
                use_camoufox=use_camoufox,
                proxy=self.proxy_url,
                user_agent=self.user_agent,
                use_llm_extraction=True,
                llm_schema=MERCARI_ITEM_SCHEMA,
                wait_time=3.0,
            )
            self._crawl4ai = Crawl4AIEngine(options)
        return self._crawl4ai

    # --- Public interface ---

    async def search(
        self,
        query: str,
        category: str = "",
        max_items: int = 50,
        **kwargs: Any,
    ) -> ScrapeResult:
        """Search Mercari Japan. Tries API first, falls back to browser."""
        start_time = time.monotonic()

        # Try internal API first (unless we know it's blocked)
        if not self._api_blocked:
            try:
                return await self._search_api(query, category, max_items)
            except Exception as e:
                self._api_blocked = True
                self._log(f"API failed, switching to browser: {e}", "warning")

        # Fallback: Crawl4AI browser
        try:
            return await self._search_browser(query, category, max_items)
        except Exception as e:
            self._log(f"Browser search also failed: {e}", "error")

        duration = time.monotonic() - start_time
        return ScrapeResult(items=[], total_found=0, errors=[f"All strategies failed for '{query}'"], duration_seconds=duration)

    async def _search_api(
        self, query: str, category: str, max_items: int
    ) -> ScrapeResult:
        """Search via internal API."""
        start_time = time.monotonic()
        items: list[ScrapedItem] = []
        offset = 0
        limit = 50

        while len(items) < max_items:
            params = {
                "keyword": query,
                "limit": min(limit, max_items - len(items)),
                "offset": offset,
                "sort": "SORT_SCORE",
                "order": "ORDER_DESC",
                "status": "STATUS_ON_SALE",
            }
            if category:
                params["category_id"] = category

            data = await self._api_request(MERCARI_API_SEARCH, params)
            results = data.get("data", {}).get("items", [])

            if not results:
                break

            for raw in results:
                try:
                    item = self._parse_api_item(raw)
                    if item:
                        items.append(item)
                except Exception as e:
                    self._log(f"Parse error: {e}", "debug")

            offset += len(results)
            if len(results) < limit:
                break

        duration = time.monotonic() - start_time
        self._log(f"API: Found {len(items)} items in {duration:.1f}s")
        return ScrapeResult(items=items, total_found=len(items), duration_seconds=duration)

    async def _search_browser(
        self, query: str, category: str, max_items: int
    ) -> ScrapeResult:
        """Search via Crawl4AI browser (fallback)."""
        start_time = time.monotonic()

        search_url = f"{MERCARI_WEB_SEARCH}?keyword={query}"
        if category:
            search_url += f"&category_id={category}"

        engine = await self._get_crawl4ai(use_camoufox=True)
        result = await engine.scrape_page(search_url)

        items: list[ScrapedItem] = []
        if result["success"] and result["structured_data"]:
            # LLM extraction returned structured data
            data = result["structured_data"]
            if isinstance(data, list):
                for entry in data:
                    item = self._parse_llm_item(entry)
                    if item:
                        items.append(item)
            elif isinstance(data, dict) and "items" in data:
                for entry in data["items"]:
                    item = self._parse_llm_item(entry)
                    if item:
                        items.append(item)

        # If LLM extraction didn't work, try CSS selectors
        if not items and result["markdown"]:
            items = self._parse_markdown_results(result["markdown"], max_items)

        duration = time.monotonic() - start_time
        self._log(f"Browser: Found {len(items)} items in {duration:.1f}s")
        return ScrapeResult(items=items, total_found=len(items), duration_seconds=duration)

    async def get_item(self, external_id: str) -> ScrapedItem | None:
        """Fetch a single item. Tries API first, then browser."""
        if not self._api_blocked:
            try:
                data = await self._api_request(MERCARI_API_ITEM.format(external_id))
                raw = data.get("data", {})
                if raw:
                    return self._parse_api_item(raw)
            except Exception:
                self._api_blocked = True

        # Browser fallback
        try:
            engine = await self._get_crawl4ai(use_camoufox=True)
            result = await engine.scrape_page(MERCARI_WEB_ITEM.format(external_id))
            if result["success"] and result["structured_data"]:
                return self._parse_llm_item(result["structured_data"])
        except Exception as e:
            self._log(f"Browser item fetch failed: {e}", "error")

        return None

    async def check_availability(self, external_id: str) -> bool:
        """Quick check if item is still on sale. Uses API (lightweight)."""
        if not self._api_blocked:
            try:
                data = await self._api_request(MERCARI_API_ITEM.format(external_id))
                raw = data.get("data", {})
                if raw:
                    status = raw.get("status", "")
                    return status in ("STATUS_ON_SALE", "on_sale")
                return False
            except Exception:
                self._api_blocked = True

        # Browser fallback
        try:
            item = await self.get_item(external_id)
            return item is not None and item.is_available
        except Exception:
            return False

    async def health_check(self) -> bool:
        """Check if we can reach Mercari."""
        try:
            result = await self.search("テスト", max_items=1)
            return len(result.errors) == 0
        except Exception:
            return False

    # --- Parsers ---

    def _parse_api_item(self, raw: dict[str, Any]) -> ScrapedItem | None:
        """Parse internal API response into ScrapedItem."""
        item_id = raw.get("id") or raw.get("item_id")
        if not item_id:
            return None

        price = float(raw.get("price", 0) or 0)

        photos = raw.get("photos", [])
        image_urls = []
        thumbnail = ""
        for i, photo in enumerate(photos):
            url = photo.get("url", "") if isinstance(photo, dict) else str(photo)
            if url:
                image_urls.append(url)
                if i == 0:
                    thumbnail = url

        seller = raw.get("seller", {})
        condition_map = {"1": "New", "2": "Like New", "3": "Good", "4": "Fair", "5": "Poor"}

        category_parts = []
        for cat_key in ["category1", "category2", "category3"]:
            cat = raw.get(cat_key)
            if cat:
                name = cat.get("name", "") if isinstance(cat, dict) else str(cat)
                if name:
                    category_parts.append(name)

        return ScrapedItem(
            external_id=str(item_id),
            url=MERCARI_WEB_ITEM.format(item_id),
            title=raw.get("name", ""),
            description=raw.get("description", ""),
            price=price,
            currency="JPY",
            condition=condition_map.get(str(raw.get("item_condition_id", "")), ""),
            category=" > ".join(category_parts),
            seller_rating=float(seller.get("rating", 0) or 0),
            seller_reviews=int(seller.get("review_count", 0) or 0),
            image_urls=image_urls,
            thumbnail_url=thumbnail,
            is_available=raw.get("status", "") in ("STATUS_ON_SALE", "on_sale", ""),
            raw_data=raw,
        )

    def _parse_llm_item(self, data: dict[str, Any]) -> ScrapedItem | None:
        """Parse LLM-extracted data into ScrapedItem."""
        title = data.get("title", "")
        price = float(data.get("price", 0) or 0)
        item_id = data.get("item_id", "")
        url = data.get("url", "")

        if not title or not item_id:
            return None

        image_urls = data.get("image_urls", [])
        if isinstance(image_urls, str):
            image_urls = [image_urls]

        return ScrapedItem(
            external_id=str(item_id),
            url=url or MERCARI_WEB_ITEM.format(item_id),
            title=title,
            description=data.get("description", ""),
            price=price,
            currency="JPY",
            condition=data.get("condition", ""),
            category=data.get("category", ""),
            seller_rating=float(data.get("seller_rating", 0) or 0),
            image_urls=image_urls,
            thumbnail_url=image_urls[0] if image_urls else "",
            is_available=True,
            raw_data=data,
        )

    def _parse_markdown_results(self, markdown: str, max_items: int) -> list[ScrapedItem]:
        """Last resort: parse markdown output for item data."""
        # Basic markdown parsing — look for links and prices
        import re
        items = []

        # Try to find item links and prices in markdown
        # Pattern: [title](url) ... ¥price
        patterns = re.findall(
            r'\[([^\]]+)\]\((https?://jp\.mercari\.com/item/([^)]+)\)\).*?[¥￥]([\d,]+)',
            markdown,
        )

        for title, url, item_id, price_str in patterns[:max_items]:
            price = float(price_str.replace(",", ""))
            items.append(
                ScrapedItem(
                    external_id=item_id,
                    url=url,
                    title=title,
                    price=price,
                    currency="JPY",
                    image_urls=[],
                    is_available=True,
                )
            )

        return items

    async def close(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
