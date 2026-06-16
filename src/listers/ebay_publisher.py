"""eBay Sell API publisher — creates and manages eBay listings.

Uses the eBay Sell REST API (Inventory API + Offer API):
1. CreateInventoryItem — register the item in eBay's inventory
2. CreateOffer — create a listing offer with price, description, etc.
3. PublishOffer — make the listing live

Docs: https://developer.ebay.com/api-docs/sell/inventory/resources/methods
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger

from src.config.settings import settings
from src.processors.enricher import EnrichedListing

# eBay API endpoints
EBAY_API_BASE = "https://api.sandbox.ebay.com" if settings.ebay_sandbox else "https://api.ebay.com"
EBAY_AUTH_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token" if settings.ebay_sandbox else "https://api.ebay.com/identity/v1/oauth2/token"

# API paths
INVENTORY_ITEM_PATH = "/sell/inventory/v1/inventory_item/{sku}"
OFFER_PATH = "/sell/inventory/v1/offer"
OFFER_BY_ID_PATH = "/sell/inventory/v1/offer/{offer_id}"
PUBLISH_OFFER_PATH = "/sell/inventory/v1/offer/{offer_id}/publish"
END_OFFER_PATH = "/sell/inventory/v1/offer/{offer_id}/end"


@dataclass
class PublishedListing:
    """Result of publishing an eBay listing."""

    success: bool
    offer_id: str = ""
    item_id: str = ""
    listing_url: str = ""
    sku: str = ""
    errors: list[str] = None  # type: ignore
    raw_response: dict = None  # type: ignore

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.raw_response is None:
            self.raw_response = {}




class EbayPublisher:
    """Publishes listings to eBay via the Sell REST API."""

    def __init__(
        self,
        app_id: str = "",
        cert_id: str = "",
        dev_id: str = "",
        ru_name: str = "",
        auth_token: str = "",
        sandbox: bool = True,
    ):
        self.app_id = app_id or settings.ebay_app_id
        self.cert_id = cert_id or settings.ebay_cert_id
        self.dev_id = dev_id or settings.ebay_dev_id
        self.ru_name = ru_name or settings.ebay_ru_name
        self.auth_token = auth_token or settings.ebay_auth_token
        self.sandbox = sandbox if sandbox is not None else settings.ebay_sandbox

        self._client: httpx.AsyncClient | None = None
        self._oauth_token: str = ""
        self._token_expires: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            token = await self._ensure_token()
            self._client = httpx.AsyncClient(
                base_url=EBAY_API_BASE,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                    "Accept": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def _ensure_token(self) -> str:
        """Get or refresh OAuth token."""
        if self._oauth_token and time.time() < self._token_expires:
            return self._oauth_token

        # Client credentials flow
        credentials = base64.b64encode(
            f"{self.app_id}:{self.cert_id}".encode()
        ).decode()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                EBAY_AUTH_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory",
                },
            )
            response.raise_for_status()
            data = response.json()

        self._oauth_token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 7200) - 300  # 5 min buffer
        return self._oauth_token

    async def create_listing(
        self,
        sku: str,
        enriched: EnrichedListing,
        price: float,
        shipping_cost: float = 0.0,
        quantity: int = 1,
        image_urls: list[str] | None = None,
        condition: str = "USED_GOOD",
        category_id: str = "",
    ) -> PublishedListing:
        """Create a full eBay listing from enriched content.

        Steps:
        1. Create inventory item
        2. Create offer
        3. Publish offer
        """
        result = PublishedListing(success=False, sku=sku)

        try:
            # Step 1: Create inventory item
            await self._create_inventory_item(sku, enriched, image_urls, condition)

            # Step 2: Create offer
            offer_id = await self._create_offer(
                sku, enriched, price, shipping_cost, quantity, category_id, condition
            )
            result.offer_id = offer_id

            # Step 3: Publish
            listing_url = await self._publish_offer(offer_id)
            result.listing_url = listing_url
            result.success = True

            logger.info(f"[eBay Publisher] Listed {sku}: {listing_url}")

        except Exception as e:
            result.errors.append(str(e))
            logger.error(f"[eBay Publisher] Failed to list {sku}: {e}")

        return result

    async def _create_inventory_item(
        self,
        sku: str,
        enriched: EnrichedListing,
        image_urls: list[str] | None,
        condition: str,
    ) -> None:
        """Create or update an inventory item."""
        client = await self._get_client()

        payload: dict[str, Any] = {
            "availability": {
                "shipToLocationAvailability": {"quantity": 1}
            },
            "condition": condition,
            "product": {
                "title": enriched.title,
                "description": enriched.description,
                "aspects": enriched.item_specifics,
            },
        }

        if image_urls:
            payload["product"]["imageUrls"] = image_urls[:12]  # eBay max 12 images

        url = INVENTORY_ITEM_PATH.format(sku=sku)
        response = await client.put(url, json=payload)

        if response.status_code not in (200, 201, 204):
            # Try to create if update fails
            response = await client.put(url, json=payload)
            response.raise_for_status()

    async def _create_offer(
        self,
        sku: str,
        enriched: EnrichedListing,
        price: float,
        shipping_cost: float,
        quantity: int,
        category_id: str,
        condition: str,
    ) -> str:
        """Create an offer (listing). Returns offer_id."""
        client = await self._get_client()

        payload: dict[str, Any] = {
            "sku": sku,
            "marketplaceId": "EBAY_US",
            "format": "FIXED_PRICE",  # Buy It Now
            "availableQuantity": quantity,
            "categoryId": category_id or self._guess_category(enriched.suggested_category),
            "listingDescription": enriched.description,
            "listingPolicies": {
                "fulfillmentPolicyId": "",  # Set from your eBay account
                "paymentPolicyId": "",
                "returnPolicyId": "",
            },
            "pricingSummary": {
                "price": {
                    "currency": "USD",
                    "value": f"{price:.2f}",
                }
            },
            "merchantLocationKey": "default",
        }

        response = await client.post(OFFER_PATH, json=payload)
        response.raise_for_status()
        data = response.json()

        return data.get("offerId", "")

    async def _publish_offer(self, offer_id: str) -> str:
        """Publish an offer to make it live. Returns listing URL."""
        client = await self._get_client()

        url = PUBLISH_OFFER_PATH.format(offer_id=offer_id)
        response = await client.post(url)
        response.raise_for_status()
        data = response.json()

        listing_id = data.get("listingId", "")
        if listing_id:
            env = "sandbox." if self.sandbox else ""
            return f"https://www.{env}ebay.com/itm/{listing_id}"
        return ""

    async def end_listing(self, offer_id: str) -> bool:
        """End a listing (delist)."""
        try:
            client = await self._get_client()
            url = END_OFFER_PATH.format(offer_id=offer_id)
            response = await client.post(url)
            response.raise_for_status()
            logger.info(f"[eBay Publisher] Ended listing {offer_id}")
            return True
        except Exception as e:
            logger.error(f"[eBay Publisher] Failed to end listing {offer_id}: {e}")
            return False

    async def update_price(self, offer_id: str, new_price: float) -> bool:
        """Update the price of an existing offer."""
        try:
            client = await self._get_client()
            url = OFFER_BY_ID_PATH.format(offer_id=offer_id)

            # Get current offer
            response = await client.get(url)
            response.raise_for_status()
            offer_data = response.json()

            # Update price
            offer_data["pricingSummary"]["price"]["value"] = f"{new_price:.2f}"

            # Put updated offer
            response = await client.put(url, json=offer_data)
            response.raise_for_status()

            # Re-publish
            await self._publish_offer(offer_id)
            return True
        except Exception as e:
            logger.error(f"[eBay Publisher] Failed to update price for {offer_id}: {e}")
            return False

    def _guess_category(self, suggested: str) -> str:
        """Map suggested category to eBay category ID. Override with real mapping."""
        # TODO: implement proper category mapping
        # For now, return empty string to use eBay's suggestion
        return ""

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
