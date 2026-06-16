"""Scrapfly API client — managed scraping fallback.

When self-hosted scraping gets blocked, route through Scrapfly.
They handle: proxies, fingerprints, anti-bot bypass, JS rendering.

Pricing: 1000 free credits, then ~$0.002/request
Docs: https://scrapfly.io/docs/scrape-api/getting-started
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger

from src.config.settings import settings


class ScrapflyClient:
    """Client for Scrapfly Web Scraping API."""

    BASE_URL = "https://api.scrapfly.io/scrape"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or settings.scrapfly_api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def scrape(
        self,
        url: str,
        js_render: bool = True,
        proxy_country: str = "",
        asp: bool = False,  # Anti-scraping protection bypass
        format: str = "markdown",  # "markdown" | "text" | "raw"
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Scrape a URL via Scrapfly.

        Args:
            url: Target URL
            js_render: Enable JavaScript rendering
            proxy_country: Proxy country code (e.g., "us", "jp")
            asp: Enable anti-scraping protection bypass
            format: Output format (markdown, text, raw)

        Returns:
            Dict with: success, content, status_code, url, error
        """
        if not self.api_key:
            return {"success": False, "error": "Scrapfly API key not configured", "content": "", "status_code": 0}

        params: dict[str, Any] = {
            "key": self.api_key,
            "url": url,
            "render_js": str(js_render).lower(),
            "format": format,
            "asp": str(asp).lower(),
        }

        if proxy_country:
            params["country"] = proxy_country

        # Add any extra params
        for k, v in kwargs.items():
            params[k] = v

        try:
            client = await self._get_client()
            response = await client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()

            result = data.get("result", {})
            return {
                "success": result.get("status_code", 0) == 200,
                "content": result.get("content", ""),
                "status_code": result.get("status_code", 0),
                "url": result.get("url", url),
                "duration": result.get("duration", 0),
                "antibot": result.get("antibot", ""),
                "proxy": result.get("proxy", ""),
                "error": "",
            }

        except Exception as e:
            logger.error(f"[Scrapfly] Scrape failed for {url}: {e}")
            return {"success": False, "error": str(e), "content": "", "status_code": 0}

    async def scrape_multiple(
        self,
        urls: list[str],
        max_concurrent: int = 5,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Scrape multiple URLs concurrently."""
        import asyncio
        semaphore = asyncio.Semaphore(max_concurrent)

        async def bounded_scrape(url: str) -> dict[str, Any]:
            async with semaphore:
                return await self.scrape(url, **kwargs)

        tasks = [bounded_scrape(url) for url in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
