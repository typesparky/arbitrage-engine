"""Central configuration via env vars + .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///data/arbitrage.db"

    # --- LLM ---
    llm_provider: str = "anthropic"  # "anthropic" | "openai"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # --- eBay ---
    ebay_app_id: str = ""
    ebay_cert_id: str = ""
    ebay_dev_id: str = ""
    ebay_ru_name: str = ""
    ebay_auth_token: str = ""
    ebay_sandbox: bool = True

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- AI Image Generation ---
    replicate_api_key: str = ""  # For SDXL img2img restyling
    image_restyle_style: str = "vintage_classy"  # vintage_classy | minimalist | lifestyle | auction
    image_restyle_backend: str = "enhance"  # replicate | dalle | local | enhance

    # --- Scraping ---
    scrape_interval_minutes: int = 10
    max_concurrent_scrapes: int = 5
    proxy_url: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    # --- Cloudflare Bypass ---
    cf_bypass_url: str = "http://localhost:8000"

    # --- Scrapfly (fallback) ---
    scrapfly_api_key: str = ""

    # --- Arbitrage ---
    min_margin_percent: float = 20.0
    max_source_price_usd: float = 500.0
    markup_multiplier: float = 2.0
    default_shipping_cost: float = 25.0
    proxy_service_fee: float = 15.0

    # --- App ---
    debug: bool = False
    log_level: str = "INFO"


settings = Settings()
