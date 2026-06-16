"""Image processing pipeline — download, process, and upload listing images.

Steps:
1. Download images from source URLs
2. Remove watermarks (basic — crop/overlay)
3. Resize to eBay requirements (min 500px, max 1600px)
4. Upload to eBay Picture Service
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from PIL import Image, ImageFilter

from src.config.settings import settings

# eBay image requirements
EBAY_MIN_SIZE = 500  # minimum width or height in pixels
EBAY_MAX_SIZE = 1600  # maximum width or height in pixels
EBAY_MAX_IMAGES = 12
EBAY_MAX_FILE_SIZE = 7 * 1024 * 1024  # 7MB


@dataclass
class ProcessedImage:
    """A processed image ready for eBay."""

    original_url: str
    local_path: str
    width: int
    height: int
    file_size: int
    format: str
    ebay_url: str = ""  # URL after uploading to eBay




class ImageProcessor:
    """Downloads, processes, and prepares images for eBay listings."""

    def __init__(self, cache_dir: str = ""):
        self.cache_dir = Path(cache_dir or "data/images")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": settings.user_agent},
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def process_listing_images(
        self,
        image_urls: list[str],
        listing_id: str,
        max_images: int = EBAY_MAX_IMAGES,
    ) -> list[ProcessedImage]:
        """Download and process all images for a listing.

        Args:
            image_urls: URLs of source images
            listing_id: Unique ID for cache organization
            max_images: Maximum images to process

        Returns:
            List of ProcessedImage objects
        """
        urls = image_urls[:max_images]
        tasks = [self._process_image(url, listing_id, i) for i, url in enumerate(urls)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = []
        for result in results:
            if isinstance(result, ProcessedImage):
                processed.append(result)
            elif isinstance(result, Exception):
                logger.warning(f"[ImageProcessor] Failed: {result}")

        return processed

    async def _process_image(
        self,
        url: str,
        listing_id: str,
        index: int,
    ) -> ProcessedImage:
        """Download and process a single image."""
        # Create cache path
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        cache_subdir = self.cache_dir / listing_id[:8]
        cache_subdir.mkdir(exist_ok=True)
        local_path = cache_subdir / f"{index:02d}_{url_hash}.jpg"

        # Download if not cached
        if not local_path.exists():
            await self._download_image(url, str(local_path))

        # Process (resize, optimize)
        img = Image.open(local_path)
        original_size = img.size

        # Resize if needed
        img = self._resize_for_ebay(img)

        # Remove basic watermarks (crop bottom 10% if it looks like a watermark area)
        # This is a heuristic — real watermark removal needs ML
        img = self._remove_watermark_heuristic(img)

        # Save optimized
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85, optimize=True)
        buffer.seek(0)

        # Check file size
        if buffer.getbuffer().nbytes > EBAY_MAX_FILE_SIZE:
            # Re-compress at lower quality
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=70, optimize=True)
            buffer.seek(0)

        with open(local_path, "wb") as f:
            f.write(buffer.getvalue())

        return ProcessedImage(
            original_url=url,
            local_path=str(local_path),
            width=img.width,
            height=img.height,
            file_size=buffer.getbuffer().nbytes,
            format="JPEG",
        )

    async def _download_image(self, url: str, local_path: str) -> None:
        """Download an image to local path."""
        client = await self._get_client()
        response = await client.get(url)
        response.raise_for_status()

        with open(local_path, "wb") as f:
            f.write(response.content)

    def _resize_for_ebay(self, img: Image.Image) -> Image.Image:
        """Resize image to meet eBay requirements."""
        w, h = img.size

        # If already within bounds, return as-is
        if EBAY_MIN_SIZE <= w <= EBAY_MAX_SIZE and EBAY_MIN_SIZE <= h <= EBAY_MAX_SIZE:
            return img

        # Scale up if too small
        if w < EBAY_MIN_SIZE or h < EBAY_MIN_SIZE:
            scale = EBAY_MIN_SIZE / min(w, h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            w, h = new_w, new_h

        # Scale down if too large
        if w > EBAY_MAX_SIZE or h > EBAY_MAX_SIZE:
            scale = EBAY_MAX_SIZE / max(w, h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        return img

    def _remove_watermark_heuristic(self, img: Image.Image) -> Image.Image:
        """Basic watermark removal — crops bottom strip if it appears to be a watermark.

        This is a heuristic approach. For production, consider:
        - ML-based watermark detection
        - Inpainting (e.g., using OpenCV or a diffusion model)
        - Manual review for high-value items
        """
        w, h = img.size
        bottom_strip = img.crop((0, int(h * 0.9), w, h))

        # Check if bottom strip is mostly uniform (likely a watermark bar)
        # Simple heuristic: low variance in the bottom strip
        import numpy as np
        strip_array = np.array(bottom_strip.convert("L"))
        variance = np.var(strip_array)

        if variance < 500:  # low variance = likely watermark bar
            # Crop it off
            img = img.crop((0, 0, w, int(h * 0.9)))

        return img

    async def upload_to_ebay(
        self,
        processed: ProcessedImage,
        ebay_token: str,
    ) -> str:
        """Upload an image to eBay Picture Service. Returns eBay image URL."""
        url = "https://api.sandbox.ebay.com/sell/inventory/v1/upload_site_hosted_image" \
            if settings.ebay_sandbox else \
            "https://api.ebay.com/sell/inventory/v1/upload_site_hosted_image"

        try:
            client = await self._get_client()
            with open(processed.local_path, "rb") as f:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {ebay_token}",
                        "Content-Type": "image/jpeg",
                    },
                    content=f.read(),
                )
                response.raise_for_status()
                data = response.json()
                return data.get("imageUrl", "")
        except Exception as e:
            logger.error(f"[ImageProcessor] eBay upload failed: {e}")
            return ""

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
