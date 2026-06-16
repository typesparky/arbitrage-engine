"""LLM-powered listing enrichment — translation, description writing, category mapping.

Supports Anthropic Claude and OpenAI GPT models.
Takes a scraped item from any source and produces eBay-ready listing content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.config.settings import settings


# --- System prompt for the enrichment task ---

ENRICHMENT_SYSTEM_PROMPT = """You are an expert eBay listing writer specializing in Japanese goods.

Your job: Translate and enrich product listings from Japanese marketplaces into compelling eBay listings.

Rules:
1. Translate Japanese titles accurately but optimize for eBay search (keywords first, 80 char max)
2. Write detailed, honest descriptions that build buyer confidence
3. Include relevant item specifics (brand, model, material, country of origin)
4. Add appropriate caveats: shipping time from Japan, condition notes, no returns policy
5. Use proper eBay category suggestions
6. NEVER make false claims about authenticity, condition, or origin
7. If the item appears to be a replica/counterfeit, note it as "style of" or "inspired by"
8. Format descriptions with clear sections: Overview, Condition, Shipping, Returns

Output valid JSON only, no markdown fences."""


@dataclass
class EnrichedListing:
    """eBay-ready listing content produced by the LLM."""

    title: str  # max 80 chars for eBay
    description: str  # full HTML/text description
    item_specifics: dict[str, str]  # eBay item specifics key-value pairs
    suggested_category: str  # eBay category path
    suggested_category_id: str  # eBay category ID
    condition_description: str
    shipping_notes: str
    return_policy: str
    tags: list[str] = field(default_factory=list)  # search keywords
    raw_response: str = ""  # full LLM response for debugging


class LLMEnricher:
    """Enriches scraped items into eBay-ready listings using LLM."""

    def __init__(
        self,
        provider: str = "",
        api_key: str = "",
        model: str = "",
    ):
        self.provider = provider or settings.llm_provider
        if self.provider == "anthropic":
            self.api_key = api_key or settings.anthropic_api_key
            self.model = model or settings.anthropic_model
        else:
            self.api_key = api_key or settings.openai_api_key
            self.model = model or settings.openai_model

    async def enrich(
        self,
        title: str,
        description: str = "",
        price_jpy: float = 0.0,
        category: str = "",
        condition: str = "",
        item_specifics: dict[str, str] | None = None,
    ) -> EnrichedListing:
        """Enrich a scraped item into an eBay-ready listing.

        Args:
            title: Original item title (may be in Japanese)
            description: Original item description
            price_jpy: Price in Japanese Yen
            category: Original category path
            condition: Item condition
            item_specifics: Any known item specifics
        """
        user_prompt = self._build_user_prompt(
            title, description, price_jpy, category, condition, item_specifics
        )

        if self.provider == "anthropic":
            response = await self._call_anthropic(user_prompt)
        else:
            response = await self._call_openai(user_prompt)

        return self._parse_response(response)

    def _build_user_prompt(
        self,
        title: str,
        description: str,
        price_jpy: float,
        category: str,
        condition: str,
        item_specifics: dict[str, str] | None,
    ) -> str:
        return f"""Create an eBay listing for this Japanese marketplace item:

Title (original): {title}
Description (original): {description or "(none)"}
Price: ¥{price_jpy:,.0f}
Category: {category or "(unknown)"}
Condition: {condition or "(unknown)"}
Item specifics: {json.dumps(item_specifics or {}, ensure_ascii=False)}

Respond with JSON in this exact format:
{{
  "title": "eBay-optimized title (max 80 chars, keywords first)",
  "description": "Full description with sections: Overview, Condition, Shipping, Returns. Use <br> for line breaks.",
  "item_specifics": {{"Brand": "...", "Model": "...", "Country/Region of Manufacture": "Japan", ...}},
  "suggested_category": "eBay category path",
  "suggested_category_id": "eBay category ID number",
  "condition_description": "Detailed condition description for eBay",
  "shipping_notes": "Shipping details including estimated delivery time from Japan",
  "return_policy": "Return policy text",
  "tags": ["keyword1", "keyword2", "keyword3"]
}}"""

    async def _call_anthropic(self, user_prompt: str) -> str:
        """Call Anthropic Claude API."""
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2048,
                    "system": ENRICHMENT_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["content"][0]["text"]

    async def _call_openai(self, user_prompt: str) -> str:
        """Call OpenAI GPT API."""
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2048,
                    "messages": [
                        {"role": "system", "content": ENRICHMENT_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    def _parse_response(self, response: str) -> EnrichedListing:
        """Parse LLM JSON response into EnrichedListing."""
        # Extract JSON from response (handle markdown fences if present)
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])  # remove fence lines

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"[Enricher] Failed to parse LLM response: {text[:500]}")
            # Fallback: create minimal listing
            return EnrichedListing(
                title="Item from Japan",
                description=text,
                item_specifics={},
                suggested_category="",
                suggested_category_id="",
                condition_description="",
                shipping_notes="Ships from Japan. Please allow 2-4 weeks for delivery.",
                return_policy="No returns on international orders.",
                raw_response=response,
            )

        return EnrichedListing(
            title=data.get("title", "")[:80],
            description=data.get("description", text),
            item_specifics=data.get("item_specifics", {}),
            suggested_category=data.get("suggested_category", ""),
            suggested_category_id=data.get("suggested_category_id", ""),
            condition_description=data.get("condition_description", ""),
            shipping_notes=data.get(
                "shipping_notes",
                "Ships from Japan via proxy service. Please allow 2-4 weeks for delivery.",
            ),
            return_policy=data.get(
                "return_policy",
                "No returns on international orders. Please review all photos and description carefully before purchasing.",
            ),
            tags=data.get("tags", []),
            raw_response=response,
        )
