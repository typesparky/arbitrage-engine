"""SQLAlchemy models for the arbitrage engine."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.config.settings import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


class SourcePlatform(enum.Enum):
    MERCARI_JP = "mercari_jp"
    YAHOO_JP = "yahoo_jp"
    TAOBAO = "taobao"
    ALIEXPRESS = "aliexpress"
    THRIFT = "thrift"
    FACEBOOK = "facebook"
    VINTED = "vinted"
    DEPOP = "depop"
    POSHMARK = "poshmark"
    AMAZON_WAREHOUSE = "amazon_warehouse"
    WALMART = "walmart"
    RAKUTEN_JP = "rakuten_jp"
    HARDOFF = "hardoff"
    BOOKOFF = "bookoff"


class TargetPlatform(enum.Enum):
    EBAY = "ebay"
    ETSY = "etsy"
    AMAZON = "amazon"
    MERCARI_US = "mercari_us"
    POSHMARK = "poshmark"


class ListingStatus(enum.Enum):
    DISCOVERED = "discovered"  # found by scraper, not yet evaluated
    CANDIDATE = "candidate"  # passes margin check, ready to list
    LISTED = "listed"  # actively listed on target platform
    SOLD = "sold"  # sold on target platform
    DELISTED = "delisted"  # manually or auto-delisted
    SOURCE_GONE = "source_gone"  # item no longer available on source
    FAILED = "failed"  # couldn't fulfill


class SourceListing(Base):
    """An item discovered on a source marketplace."""

    __tablename__ = "source_listings"
    __table_args__ = (
        Index("idx_source_platform_external", "source_platform", "external_id", unique=True),
        Index("idx_source_status", "status"),
        Index("idx_source_category", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(Enum(SourcePlatform), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)  # ID on source platform
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title_original: Mapped[str] = mapped_column(Text, nullable=False)
    title_translated: Mapped[str] = mapped_column(Text, default="")
    description_original: Mapped[str] = mapped_column(Text, default="")
    description_translated: Mapped[str] = mapped_column(Text, default="")
    price_original: Mapped[float] = mapped_column(Float, nullable=False)  # in source currency
    currency: Mapped[str] = mapped_column(String(10), default="JPY")
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)  # converted
    condition: Mapped[str] = mapped_column(String(100), default="")
    category: Mapped[str] = mapped_column(String(255), default="")
    seller_rating: Mapped[float] = mapped_column(Float, default=0.0)
    seller_reviews: Mapped[int] = mapped_column(Integer, default=0)
    image_urls: Mapped[str] = mapped_column(Text, default="")  # JSON array
    thumbnail_url: Mapped[str] = mapped_column(String(2048), default="")
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[ListingStatus] = mapped_column(
        Enum(ListingStatus), default=ListingStatus.DISCOVERED
    )
    raw_data: Mapped[str] = mapped_column(Text, default="")  # JSON blob of original scrape
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    target_listings: Mapped[list[TargetListing]] = relationship(
        "TargetListing", back_populates="source_listing"
    )
    price_snapshots: Mapped[list[PriceSnapshot]] = relationship(
        "PriceSnapshot", back_populates="source_listing"
    )


class TargetListing(Base):
    """A listing we've created on a target marketplace."""

    __tablename__ = "target_listings"
    __table_args__ = (
        Index("idx_target_platform_external", "target_platform", "external_id", unique=True),
        Index("idx_target_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("source_listings.id"), nullable=False
    )
    target_platform: Mapped[TargetPlatform] = mapped_column(Enum(TargetPlatform), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), default="")  # ID on target platform
    url: Mapped[str] = mapped_column(String(2048), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ListingStatus] = mapped_column(
        Enum(ListingStatus), default=ListingStatus.LISTED
    )
    listed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    sold_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    delisted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    estimated_profit: Mapped[float] = mapped_column(Float, default=0.0)
    actual_profit: Mapped[float] = mapped_column(Float, default=0.0)
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    source_listing: Mapped[SourceListing] = relationship(
        "SourceListing", back_populates="target_listings"
    )


class PriceSnapshot(Base):
    """Historical price tracking for source listings."""

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("idx_price_source_time", "source_listing_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("source_listings.id"), nullable=False
    )
    price_original: Mapped[float] = mapped_column(Float, nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    source_listing: Mapped[SourceListing] = relationship(
        "SourceListing", back_populates="price_snapshots"
    )


class EbaySoldListing(Base):
    """Cached eBay sold listing data for price comparison."""

    __tablename__ = "ebay_sold_listings"
    __table_args__ = (
        Index("idx_ebay_sold_query", "search_query", "sold_at"),
        Index("idx_ebay_sold_external", "external_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    search_query: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0.0)
    condition: Mapped[str] = mapped_column(String(100), default="")
    sold_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    url: Mapped[str] = mapped_column(String(2048), default="")
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class SyncLog(Base):
    """Log of sync runs for monitoring."""

    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "inventory" | "price"
    items_checked: Mapped[int] = mapped_column(Integer, default=0)
    items_delisted: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_details: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


# --- Engine & Session ---

engine = create_async_engine(settings.database_url, echo=settings.debug)


async def init_db() -> None:
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with AsyncSession(engine) as session:
        yield session
