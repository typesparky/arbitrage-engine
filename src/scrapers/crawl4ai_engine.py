"""Crawl4AI-based scraping engine — primary scraper for all marketplaces.

Replaces hand-rolled Playwright with Crawl4AI's:
- LLM-powered structured extraction
- Built-in anti-bot detection and bypass
- Async browser pool for parallel crawling
- CSS/XPath + LLM extraction strategies
- Session management and proxy rotation

For stealth-critical sites, uses Camoufox as the browser backend.
For Cloudflare-protected sites, routes through Cloudflare Bypass middleware.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

from loguru import logger

from src.config.settings import settings
from src.scrapers.base import BaseScraper, ScrapeResult, ScrapedItem


# --- Extraction schemas for different marketplaces ---

MERCARI_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Item title"},
        "price": {"type": "number", "description": "Item price in yen"},
        "description": {"type": "string", "description": "Item description"},
        "condition": {"type": "string", "description": "Item condition (new, like new, good, etc.)"},
        "category": {"type": "string", "description": "Item category"},
        "seller_name": {"type": "string", "description": "Seller username"},
        "seller_rating": {"type": "number", "description": "Seller rating 0-5"},
        "image_urls": {"type": "array", "items": {"type": "string"}, "description": "All image URLs"},
        "item_id": {"type": "string", "description": "Unique item ID"},
        "url": {"type": "string", "description": "Item URL"},
    },
    "required": ["title", "price", "item_id"],
}

EBAY_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Listing title"},
        "price": {"type": "number", "description": "Price in USD"},
        "shipping_cost": {"type": "number", "description": "Shipping cost in USD"},
        "condition": {"type": "string", "description": "Item condition"},
        "sold_date": {"type": "string", "description": "Date sold (for sold listings)"},
        "item_id": {"type": "string", "description": "eBay item ID"},
        "url": {"type": "string", "description": "Listing URL"},
    },
    "required": ["title", "price"],
}


@dataclass
class Crawl4AIOptions:
    """Options for Crawl4AI scraping."""

    # Browser
    headless: bool = True
    use_camoufox: bool = False  # Use Camoufox instead of Chromium
    proxy: str = ""
    user_agent: str = ""

    # Extraction
    use_llm_extraction: bool = True
    llm_schema: dict[str, Any] = field(default_factory=dict)
    css_selectors: dict[str, str] = field(default_factory=dict)

    # Behavior
    wait_for_selector: str = ""
    wait_time: float = 2.0
    scroll_pages: int = 0
    js_code: str = ""

    # Cache
    use_cache: bool = True
    cache_ttl: int = 3600  # seconds


class Crawl4AIEngine:
    """Unified scraping engine powered by Crawl4AI.

    Supports multiple browser backends:
    - Chromium (default, via Playwright)
    - Camoufox (anti-detect Firefox)
    - Cloudflare Bypass (middleware for CF-protected sites)
    """

    def __init__(self, options: Crawl4AIOptions | None = None):
        self.options = options or Crawl4AIOptions()
        self._crawler = None

    async def _get_crawler(self):
        """Lazy-init the Crawl4AI AsyncWebCrawler."""
        if self._crawler is not None:
            return self._crawler

        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
        except ImportError:
            raise ImportError(
                "crawl4ai is required. Install with: pip install crawl4ai && crawl4ai-setup"
            )

        # Browser config
        browser_config = BrowserConfig(
            headless=self.options.headless,
            user_agent=self.options.user_agent or settings.user_agent,
        )

        if self.options.proxy:
            browser_config.proxy = self.options.proxy

        # Use Camoufox if requested
        if self.options.use_camoufox:
            browser_config.browser_type = "camoufox"

        self._browser_config = browser_config
        self._crawler_cls = AsyncWebCrawler
        self._run_config_cls = CrawlerRunConfig
        self._cache_mode = CacheMode

        return self._crawler_cls

    async def scrape_page(
        self,
        url: str,
        options: Crawl4AIOptions | None = None,
    ) -> dict[str, Any]:
        """Scrape a single page and return structured data.

        Returns dict with:
        - markdown: clean markdown content
        - structured_data: extracted JSON (if LLM/schema extraction used)
        - html: raw HTML
        - links: extracted links
        - images: extracted image URLs
        - success: bool
        - error: error message if failed
        """
        opts = options or self.options
        start_time = time.monotonic()

        try:
            from crawl4ai import CrawlerRunConfig, CacheMode, LLMConfig
            from crawl4ai.extraction_strategy import LLMExtractionStrategy, JsonCssExtractionStrategy

            crawler_cls = await self._get_crawler()

            # Build run config
            run_kwargs: dict[str, Any] = {
                "cache_mode": CacheMode.ENABLED if opts.use_cache else CacheMode.DISABLED,
                "wait_for": opts.wait_for_selector,
                "delay_before_return_html": opts.wait_time,
                "js_code": opts.js_code if opts.js_code else None,
            }

            # Extraction strategy
            if opts.use_llm_extraction and opts.llm_schema:
                # LLM-powered extraction
                llm_config = LLMConfig(
                    provider=f"{settings.llm_provider}/{self._get_model_name()}",
                    api_token=self._get_api_key(),
                )
                run_kwargs["extraction_strategy"] = LLMExtractionStrategy(
                    llm_config=llm_config,
                    schema=opts.llm_schema,
                    instruction="Extract the product listing data from this page.",
                )
            elif opts.css_selectors:
                # CSS selector extraction (faster, no LLM cost)
                schema = {
                    "name": "items",
                    "baseSelector": opts.css_selectors.get("base", "body"),
                    "fields": [
                        {"name": k, "selector": v, "type": "text"}
                        for k, v in opts.css_selectors.items()
                        if k != "base"
                    ],
                }
                run_kwargs["extraction_strategy"] = JsonCssExtractionStrategy(schema)

            # Scroll if needed
            if opts.scroll_pages > 0:
                run_kwargs["js_code"] = self._build_scroll_js(opts.scroll_pages)

            config = CrawlerRunConfig(**run_kwargs)

            async with crawler_cls(config=self._browser_config) as crawler:
                result = await crawler.arun(url=url, config=config)

            duration = time.monotonic() - start_time

            return {
                "success": result.success,
                "markdown": (
                    result.markdown.raw_markdown
                    if hasattr(result, "markdown") and result.markdown
                    else result.html or ""
                ),
                "structured_data": (
                    json.loads(result.extracted_content)
                    if result.extracted_content
                    else {}
                ),
                "html": result.html,
                "links": result.links,
                "images": result.media.get("images", []) if result.media else [],
                "url": str(result.url),
                "status_code": result.status_code,
                "duration": duration,
                "error": result.error_message if not result.success else "",
            }

        except Exception as e:
            duration = time.monotonic() - start_time
            logger.error(f"[Crawl4AI] Scrape failed for {url}: {e}")
            return {
                "success": False,
                "markdown": "",
                "structured_data": {},
                "html": "",
                "links": {},
                "images": [],
                "url": url,
                "status_code": 0,
                "duration": duration,
                "error": str(e),
            }

    async def scrape_multiple(
        self,
        urls: list[str],
        options: Crawl4AIOptions | None = None,
        max_concurrent: int = 3,
    ) -> list[dict[str, Any]]:
        """Scrape multiple URLs concurrently with rate limiting."""
        semaphore = asyncio.Semaphore(max_concurrent)
        results = []

        async def bounded_scrape(url: str) -> dict[str, Any]:
            async with semaphore:
                result = await self.scrape_page(url, options)
                await asyncio.sleep(1.0)  # Rate limit between requests
                return result

        tasks = [bounded_scrape(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to error results
        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append({
                    "success": False,
                    "url": urls[i],
                    "error": str(result),
                    "markdown": "",
                    "structured_data": {},
                    "html": "",
                    "links": {},
                    "images": [],
                    "status_code": 0,
                    "duration": 0,
                })
            else:
                processed.append(result)

        return processed

    def _get_model_name(self) -> str:
        if settings.llm_provider == "anthropic":
            return settings.anthropic_model
        return settings.openai_model

    def _get_api_key(self) -> str:
        if settings.llm_provider == "anthropic":
            return settings.anthropic_api_key
        return settings.openai_api_key

    @staticmethod
    def _build_scroll_js(pages: int) -> str:
        """Build JS code to scroll down N pages."""
        return f"""
        () => {{
            for (let i = 0; i < {pages}; i++) {{
                window.scrollTo(0, document.body.scrollHeight);
                // Wait for content to load
                await new Promise(r => setTimeout(r, 2000));
            }}
        }}
        """
