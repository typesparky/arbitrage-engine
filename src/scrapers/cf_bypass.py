"""Cloudflare Bypass middleware client.

Routes requests through a local Cloudflare Bypass server
(https://github.com/sarperavci/CloudflareBypassForScraping).

Usage:
    1. Run the CF bypass server: docker run -p 8000:8000 ghcr.io/sarperavci/cloudflarebypassforscraping:latest
    2. Set CF_BYPASS_URL=http://localhost:8000 in .env
    3. Use CloudflareBypasser.fetch(url) instead of direct requests

The bypass server:
- Uses a real browser to solve Cloudflare challenges
- Caches clearance cookies (valid for hours)
- Provides request mirroring for any HTTP method
- Returns raw HTML after bypass
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from src.config.settings import settings


class CloudflareBypasser:
    """Client for the Cloudflare Bypass middleware server."""

    def __init__(self, base_url: str = ""):
        self.base_url = base_url or settings.cf_bypass_url

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        proxy: str = "",
        force_fresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch a URL through the Cloudflare bypass server.

        Args:
            url: Target URL to fetch
            method: HTTP method (GET, POST, etc.)
            headers: Additional headers to forward
            data: POST data
            proxy: Optional proxy URL
            force_fresh: Force fresh cookie generation (bypass cache)

        Returns:
            Dict with: success, html, status_code, cookies, user_agent, error
        """
        if not self.base_url:
            return {"success": False, "error": "CF bypass URL not configured", "html": "", "status_code": 0}

        target_hostname = urlparse(url).netloc

        request_headers = {
            "x-hostname": target_hostname,
        }
        if proxy:
            request_headers["x-proxy"] = proxy
        if force_fresh:
            request_headers["x-bypass-cache"] = "true"
        if headers:
            for k, v in headers.items():
                request_headers[k] = v

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=60.0) as client:
                if method.upper() == "GET":
                    response = await client.get(url, headers=request_headers)
                elif method.upper() == "POST":
                    response = await client.post(url, headers=request_headers, json=data)
                else:
                    response = await client.request(method, url, headers=request_headers, json=data)

                return {
                    "success": response.status_code == 200,
                    "html": response.text,
                    "status_code": response.status_code,
                    "cookies": response.headers.get("x-cf-bypasser-cookies", ""),
                    "user_agent": response.headers.get("x-cf-bypasser-user-agent", ""),
                    "final_url": response.headers.get("x-cf-bypasser-final-url", url),
                    "processing_time_ms": int(response.headers.get("x-processing-time-ms", "0")),
                    "error": "" if response.status_code == 200 else f"HTTP {response.status_code}",
                }

        except Exception as e:
            logger.error(f"[CF Bypass] Failed to fetch {url}: {e}")
            return {"success": False, "error": str(e), "html": "", "status_code": 0}

    async def get_cookies(self, url: str) -> dict[str, str]:
        """Get Cloudflare cookies for a URL without fetching content."""
        if not self.base_url:
            return {}

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
                response = await client.get(
                    "/cookies",
                    params={"url": url},
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get("cookies", {})
        except Exception as e:
            logger.error(f"[CF Bypass] Cookie fetch failed: {e}")

        return {}

    async def health_check(self) -> bool:
        """Check if the bypass server is running."""
        if not self.base_url:
            return False
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
                response = await client.get("/")
                return response.status_code < 500
        except Exception:
            return False
