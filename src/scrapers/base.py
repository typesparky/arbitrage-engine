"""Base scraper abstraction — all source scrapers implement this interface."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger


@dataclass
class ScrapedItem:
    """Normalized item from any source platform."""

    external_id: str
    url: str
    title: str
    description: str = ""
    price: float = 0.0
    currency: str = "USD"
    condition: str = ""
    category: str = ""
    seller_rating: float = 0.0
    seller_reviews: int = 0
    image_urls: list[str] = field(default_factory=list)
    thumbnail_url: str = ""
    is_available: bool = True
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "price": self.price,
            "currency": self.currency,
            "condition": self.condition,
            "category": self.category,
            "seller_rating": self.seller_rating,
            "seller_reviews": self.seller_reviews,
            "image_urls": self.image_urls,
            "thumbnail_url": self.thumbnail_url,
            "is_available": self.is_available,
            "raw_data": self.raw_data,
        }


@dataclass
class ScrapeResult:
    """Result of a scrape run."""

    items: list[ScrapedItem]
    total_found: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BaseScraper(ABC):
    """Abstract base for all source platform scrapers."""

    PLATFORM: str = ""  # override in subclass

    def __init__(self, proxy_url: str = "", user_agent: str = ""):
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self._session: Any = None

    @abstractmethod
    async def search(
        self, query: str, category: str = "", max_items: int = 50, **kwargs: Any
    ) -> ScrapeResult:
        """Search for items matching query. Returns normalized ScrapedItems."""
        ...

    @abstractmethod
    async def get_item(self, external_id: str) -> ScrapedItem | None:
        """Fetch a single item by its external ID."""
        ...

    @abstractmethod
    async def check_availability(self, external_id: str) -> bool:
        """Quick check if item is still available."""
        ...

    async def health_check(self) -> bool:
        """Check if the scraper can reach the platform. Override for custom logic."""
        return True

    def _log(self, message: str, level: str = "info") -> None:
        getattr(logger, level)(f"[{self.PLATFORM}] {message}")
