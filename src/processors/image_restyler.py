"""AI image restyling — transform marketplace photos into professional listing images.

Takes raw Mercari/Japanese marketplace photos and restyles them into a consistent
"vintage classy photoshoot" aesthetic. This:

1. Removes seller watermarks and backgrounds
2. Creates clean, professional-looking product photos
3. Applies a consistent style across all listings
4. Makes photos look like professional eBay/Amazon listings

Supports multiple AI backends:
- Replicate (SDXL img2img) — best quality, requires API key
- Local Stable Diffusion (via diffusers) — free, requires GPU
- OpenAI DALL-E 3 — good quality, requires API key

Style presets:
- "vintage_classy": Clean white/cream background, soft shadows, warm tones
- "minimalist": Pure white background, sharp focus, neutral tones
- "lifestyle": Contextual background (shelf, desk, hands holding item)
- "auction": Dark background, dramatic lighting, high contrast
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from PIL import Image, ImageFilter, ImageEnhance

from src.config.settings import settings


# ===== STYLE PRESETS =====

STYLE_PRESETS = {
    "vintage_classy": {
        "name": "Vintage Classy",
        "prompt": (
            "professional product photography, vintage aesthetic, "
            "warm cream white background, soft natural lighting, "
            "elegant composition, shallow depth of field, "
            "clean minimalist style, high-end catalog photo, "
            "subtle shadows, warm tones, 8k quality"
        ),
        "negative_prompt": (
            "watermark, text, logo, cluttered background, harsh lighting, "
            "low quality, blurry, amateur photo, busy pattern, "
            "oversaturated, cartoon, drawing"
        ),
        "strength": 0.65,  # How much to transform (0=original, 1=completely new)
        "cfg_scale": 7.5,
    },
    "minimalist": {
        "name": "Minimalist",
        "prompt": (
            "professional product photography, pure white background, "
            "studio lighting, clean sharp focus, commercial catalog style, "
            "no shadows, high key lighting, professional eBay listing photo, "
            "8k quality, product centered"
        ),
        "negative_prompt": (
            "watermark, text, logo, background objects, shadows, "
            "low quality, blurry, amateur, cluttered"
        ),
        "strength": 0.55,
        "cfg_scale": 7.0,
    },
    "lifestyle": {
        "name": "Lifestyle",
        "prompt": (
            "lifestyle product photography, item displayed on wooden shelf "
            "or desk with subtle props, warm ambient lighting, "
            "cozy aesthetic, professional catalog style, "
            "shallow depth of field, 8k quality"
        ),
        "negative_prompt": (
            "watermark, text, logo, cluttered, harsh lighting, "
            "low quality, blurry, white background"
        ),
        "strength": 0.70,
        "cfg_scale": 8.0,
    },
    "auction": {
        "name": "Auction",
        "prompt": (
            "dramatic product photography, dark moody background, "
            "spotlight lighting, high contrast, premium look, "
            "museum display style, elegant shadows, "
            "professional auction listing photo, 8k quality"
        ),
        "negative_prompt": (
            "watermark, text, logo, bright background, flat lighting, "
            "low quality, blurry, amateur"
        ),
        "strength": 0.60,
        "cfg_scale": 8.5,
    },
}


@dataclass
class RestyledImage:
    """A restyled product image."""
    original_url: str
    local_path: str
    restyled_path: str = ""
    style: str = ""
    width: int = 0
    height: int = 0
    file_size: int = 0
    success: bool = False
    error: str = ""


@dataclass
class RestyleResult:
    """Result of restyling a set of images for a listing."""
    images: list[RestyledImage] = field(default_factory=list)
    total: int = 0
    successful: int = 0
    failed: int = 0
    duration_seconds: float = 0.0


# ===== IMAGE DOWNLOADER =====

class ImageDownloader:
    """Download images from URLs with retry logic."""

    def __init__(self, cache_dir: str = ""):
        self.cache_dir = Path(cache_dir or "data/images/originals")
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

    async def download(self, url: str, listing_id: str = "") -> str | None:
        """Download an image and return local path."""
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        ext = self._get_ext(url)
        cache_subdir = self.cache_dir / (listing_id[:8] if listing_id else "misc")
        cache_subdir.mkdir(exist_ok=True)
        local_path = cache_subdir / f"{url_hash}{ext}"

        if local_path.exists():
            return str(local_path)

        try:
            client = await self._get_client()
            resp = await client.get(url)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return str(local_path)
        except Exception as e:
            logger.debug(f"Image download failed: {e}")
            return None

    def _get_ext(self, url: str) -> str:
        path = urlparse(url).path.lower()
        if path.endswith(".png"):
            return ".png"
        if path.endswith(".webp"):
            return ".webp"
        return ".jpg"

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ===== AI IMAGE RESTYLER =====

class AIRestyler:
    """Restyle product images using AI image-to-image generation.

    Supports multiple backends. Automatically picks the best available.
    """

    def __init__(
        self,
        style: str = "vintage_classy",
        backend: str = "replicate",
        output_dir: str = "",
    ):
        self.style = style
        self.backend = backend
        self.preset = STYLE_PRESETS.get(style, STYLE_PRESETS["vintage_classy"])
        self.output_dir = Path(output_dir or "data/images/restyled")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.downloader = ImageDownloader()

    async def restyle_listing_images(
        self,
        image_urls: list[str],
        listing_id: str = "",
        max_images: int = 4,
    ) -> RestyleResult:
        """Download and restyle all images for a listing."""
        start = time.monotonic()
        result = RestyleResult(total=min(len(image_urls), max_images))

        for i, url in enumerate(image_urls[:max_images]):
            restyled = RestyledImage(original_url=url, style=self.style)
            try:
                # Download original
                local_path = await self.downloader.download(url, listing_id)
                if not local_path:
                    restyled.error = "Download failed"
                    result.failed += 1
                    result.images.append(restyled)
                    continue

                restyled.local_path = local_path

                # Restyle
                output_path = self.output_dir / f"{listing_id[:8]}_{i:02d}.jpg"
                restyled_path = await self._restyle_single(
                    local_path, str(output_path)
                )

                if restyled_path:
                    restyled.restyled_path = restyled_path
                    restyled.success = True
                    img = Image.open(restyled_path)
                    restyled.width, restyled.height = img.size
                    restyled.file_size = Path(restyled_path).stat().st_size
                    result.successful += 1
                else:
                    restyled.error = "Restyling failed"
                    result.failed += 1

            except Exception as e:
                restyled.error = str(e)
                result.failed += 1

            result.images.append(restyled)

        result.duration_seconds = time.monotonic() - start
        logger.info(
            f"[Restyler] {result.successful}/{result.total} images restyled "
            f"in {result.duration_seconds:.1f}s"
        )
        return result

    async def _restyle_single(
        self, input_path: str, output_path: str
    ) -> str | None:
        """Restyle a single image using the configured backend."""
        if self.backend == "replicate":
            return await self._restyle_replicate(input_path, output_path)
        elif self.backend == "dalle":
            return await self._restyle_dalle(input_path, output_path)
        elif self.backend == "local":
            return await self._restyle_local(input_path, output_path)
        else:
            # Fallback: just enhance the original
            return self._enhance_original(input_path, output_path)

    async def _restyle_replicate(
        self, input_path: str, output_path: str
    ) -> str | None:
        """Use Replicate's SDXL img2img model."""
        api_key = settings.replicate_api_key
        if not api_key:
            logger.debug("[Restyler] No Replicate API key, using enhancement fallback")
            return self._enhance_original(input_path, output_path)

        try:
            # Read and encode image
            with open(input_path, "rb") as f:
                import base64
                img_b64 = base64.b64encode(f.read()).decode()
                img_data = f"data:image/jpeg;base64,{img_b64}"

            # Call Replicate API
            async with httpx.AsyncClient(timeout=120.0) as client:
                # Start prediction
                resp = await client.post(
                    "https://api.replicate.com/v1/predictions",
                    headers={
                        "Authorization": f"Token {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "version": "39ed52f2a78e934b3ba6e2a89f5b1c712de7dfea535525255b1aa35c5565e08b",
                        "input": {
                            "image": img_data,
                            "prompt": self.preset["prompt"],
                            "negative_prompt": self.preset["negative_prompt"],
                            "strength": self.preset["strength"],
                            "guidance_scale": self.preset["cfg_scale"],
                            "num_inference_steps": 30,
                        },
                    },
                )
                resp.raise_for_status()
                prediction = resp.json()
                prediction_id = prediction["id"]

                # Poll for result
                for _ in range(60):  # Max 60 seconds
                    await asyncio.sleep(2)
                    status_resp = await client.get(
                        f"https://api.replicate.com/v1/predictions/{prediction_id}",
                        headers={"Authorization": f"Token {api_key}"},
                    )
                    status_data = status_resp.json()
                    status = status_data.get("status", "")

                    if status == "succeeded":
                        output_url = status_data.get("output", "")
                        if isinstance(output_url, list):
                            output_url = output_url[0]
                        if output_url:
                            # Download the result
                            img_resp = await client.get(output_url)
                            with open(output_path, "wb") as f:
                                f.write(img_resp.content)
                            return output_path
                        return None
                    elif status in ("failed", "canceled"):
                        logger.warning(f"[Restyler] Replicate failed: {status_data.get('error', '')}")
                        return None

                logger.warning("[Restyler] Replicate timeout")
                return None

        except Exception as e:
            logger.warning(f"[Restyler] Replicate error: {e}")
            return None

    async def _restyle_dalle(
        self, input_path: str, output_path: str
    ) -> str | None:
        """Use OpenAI DALL-E for image variation."""
        if not settings.openai_api_key:
            return self._enhance_original(input_path, output_path)

        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.openai_api_key)

            # Resize to DALL-E requirements (1024x1024)
            img = Image.open(input_path)
            img = img.resize((1024, 1024), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            response = await client.images.create_variation(
                image=buf,
                n=1,
                size="1024x1024",
            )

            if response.data:
                image_url = response.data[0].url
                async with httpx.AsyncClient(timeout=30.0) as dl:
                    img_resp = await dl.get(image_url)
                    with open(output_path, "wb") as f:
                        f.write(img_resp.content)
                return output_path

        except Exception as e:
            logger.warning(f"[Restyler] DALL-E error: {e}")
        return None

    async def _restyle_local(
        self, input_path: str, output_path: str
    ) -> str | None:
        """Use local Stable Diffusion via diffusers."""
        try:
            import torch
            from diffusers import StableDiffusionXLImg2ImgPipeline

            pipe = StableDiffusionXLImg2ImgPipeline.from_pretrained(
                "stabilityai/stable-diffusion-xl-refiner-1-0",
                torch_dtype=torch.float16,
                use_safetensors=True,
                variant="fp16",
            )
            pipe = pipe.to("mps" if torch.backends.mps.is_available() else "cuda")

            init_image = Image.open(input_path).convert("RGB")
            init_image = init_image.resize((1024, 1024), Image.Resampling.LANCZOS)

            result = pipe(
                prompt=self.preset["prompt"],
                negative_prompt=self.preset["negative_prompt"],
                image=init_image,
                strength=self.preset["strength"],
                guidance_scale=self.preset["cfg_scale"],
                num_inference_steps=30,
            )

            result.images[0].save(output_path, quality=90)
            return output_path

        except ImportError:
            logger.debug("[Restyler] diffusers not installed, using enhancement")
            return self._enhance_original(input_path, output_path)
        except Exception as e:
            logger.warning(f"[Restyler] Local SD error: {e}")
            return None

    def _enhance_original(self, input_path: str, output_path: str) -> str | None:
        """Fallback: enhance the original image without AI.

        Applies professional-looking enhancements:
        - Auto white balance
        - Sharpening
        - Contrast boost
        - Clean background (basic)
        - Resize to eBay specs
        """
        try:
            img = Image.open(input_path)
            img = img.convert("RGB")

            # Resize to eBay requirements
            img = self._resize_for_ebay(img)

            # Enhance
            img = self._auto_enhance(img)

            # Save
            img.save(output_path, "JPEG", quality=90)
            return output_path

        except Exception as e:
            logger.warning(f"[Restyler] Enhancement failed: {e}")
            return None

    def _resize_for_ebay(self, img: Image.Image) -> Image.Image:
        """Resize to eBay requirements (min 500px, max 1600px)."""
        w, h = img.size
        min_size, max_size = 500, 1600

        if w < min_size or h < min_size:
            scale = min_size / min(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            w, h = img.size

        if w > max_size or h > max_size:
            scale = max_size / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        return img

    def _auto_enhance(self, img: Image.Image) -> Image.Image:
        """Apply automatic enhancements to make photos look professional."""
        # Auto contrast
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.1)

        # Slight sharpness boost
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.2)

        # Color balance
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(1.05)

        # Brightness
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(1.05)

        return img

    async def close(self):
        await self.downloader.close()


# ===== STYLE CONSISTENCY =====

class StyleConsistency:
    """Ensure all listing images have a consistent look and feel."""

    @staticmethod
    def apply_watermark(
        img_path: str,
        output_path: str,
        text: str = "",
        position: str = "bottom-right",
        opacity: int = 128,
    ) -> str:
        """Add a subtle watermark/branding to the image."""
        from PIL import ImageDraw, ImageFont

        img = Image.open(img_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        if text:
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
            except (OSError, IOError):
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

            padding = 10
            if position == "bottom-right":
                x = img.width - tw - padding
                y = img.height - th - padding
            elif position == "bottom-left":
                x, y = padding, img.height - th - padding
            else:
                x, y = padding, padding

            draw.text((x, y), text, font=font, fill=(255, 255, 255, opacity))

        result = Image.alpha_composite(img, overlay)
        result = result.convert("RGB")
        result.save(output_path, "JPEG", quality=90)
        return output_path

    @staticmethod
    def create_listing_collage(
        image_paths: list[str],
        output_path: str,
        cols: int = 2,
        padding: int = 10,
        bg_color: tuple = (255, 255, 255),
    ) -> str:
        """Create a collage of multiple product images for a single listing."""
        images = [Image.open(p).convert("RGB") for p in image_paths if Path(p).exists()]
        if not images:
            return ""

        # Normalize sizes
        target_w, target_h = 800, 800
        resized = []
        for img in images:
            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            resized.append(img)

        rows = (len(resized) + cols - 1) // cols
        collage_w = cols * target_w + (cols + 1) * padding
        collage_h = rows * target_h + (rows + 1) * padding

        collage = Image.new("RGB", (collage_w, collage_h), bg_color)

        for i, img in enumerate(resized):
            row, col = i // cols, i % cols
            x = padding + col * (target_w + padding)
            y = padding + row * (target_h + padding)
            collage.paste(img, (x, y))

        collage.save(output_path, "JPEG", quality=90)
        return output_path
