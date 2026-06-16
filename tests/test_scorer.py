"""Tests for the candidate scoring engine."""

from __future__ import annotations

import pytest

from src.processors.scorer import CandidateScorer, JPY_TO_USD
from src.scrapers.base import ScrapedItem
from src.scrapers.ebay_finding import EbayPriceAnalysis


@pytest.fixture
def scorer():
    return CandidateScorer(
        min_margin_percent=30,
        max_source_price_usd=500,
        default_shipping=25,
        proxy_fee=15,
    )


@pytest.fixture
def good_item():
    """A high-margin anime figure from Mercari JP.
    Source: 5000 JPY = ~$33.50 USD
    eBay median: $150
    Costs: $33.50 + $25 shipping + $15 proxy = $73.50
    Fees: $150 * 15.9% + $0.30 = $24.15
    Profit: $150 - $73.50 - $24.15 = $52.35
    Margin: $52.35 / $150 = 34.9%
    """
    return ScrapedItem(
        external_id="m12345",
        url="https://jp.mercari.com/item/m12345",
        title="Nendoroid Anime Figure Rare",
        description="Rare anime figure, good condition",
        price=5000,
        currency="JPY",
        condition="Good",
        category="Hobbies > Figures > Anime",
        seller_rating=4.8,
        seller_reviews=200,
        image_urls=["https://example.com/1.jpg"],
        is_available=True,
    )


@pytest.fixture
def good_ebay_analysis():
    """Strong eBay market data with good sold volume."""
    return EbayPriceAnalysis(
        query="Nendoroid Anime Figure Rare",
        median_price=150.0,
        avg_price=145.0,
        min_price=100.0,
        max_price=220.0,
        total_sold=85,
        items=[],
    )


@pytest.fixture
def cheap_item():
    """A low-margin item that should be rejected."""
    return ScrapedItem(
        external_id="m67890",
        url="https://jp.mercari.com/item/m67890",
        title="Common Item",
        price=20000,
        currency="JPY",
        condition="Good",
        seller_rating=3.0,
        seller_reviews=10,
        image_urls=[],
        is_available=True,
    )


@pytest.fixture
def weak_ebay_analysis():
    """Weak eBay market data — low price, few sales."""
    return EbayPriceAnalysis(
        query="Common Item",
        median_price=50.0,
        avg_price=55.0,
        min_price=30.0,
        max_price=70.0,
        total_sold=2,
        items=[],
    )


class TestCandidateScorer:
    def test_good_candidate_is_viable(self, scorer, good_item, good_ebay_analysis):
        result = scorer.score(good_item, good_ebay_analysis)
        assert result.is_viable
        assert result.estimated_margin_percent > 30
        assert result.overall_score > 40

    def test_cheap_item_rejected(self, scorer, cheap_item, weak_ebay_analysis):
        result = scorer.score(cheap_item, weak_ebay_analysis)
        assert not result.is_viable
        assert len(result.rejection_reasons) > 0

    def test_jpy_conversion(self, scorer, good_item, good_ebay_analysis):
        result = scorer.score(good_item, good_ebay_analysis)
        expected_usd = 5000 * JPY_TO_USD
        assert abs(result.source_price_usd - expected_usd) < 1.0

    def test_profit_calculation(self, scorer, good_item, good_ebay_analysis):
        result = scorer.score(good_item, good_ebay_analysis)
        assert result.estimated_profit > 40

    def test_no_ebay_data_rejected(self, scorer, good_item):
        empty_analysis = EbayPriceAnalysis(
            query="test", median_price=0, avg_price=0,
            min_price=0, max_price=0, total_sold=0, items=[],
        )
        result = scorer.score(good_item, empty_analysis)
        assert not result.is_viable
        assert any("No eBay price data" in r for r in result.rejection_reasons)

    def test_ranking(self, scorer, good_item, good_ebay_analysis, cheap_item, weak_ebay_analysis):
        good_score = scorer.score(good_item, good_ebay_analysis)
        bad_score = scorer.score(cheap_item, weak_ebay_analysis)
        ranked = scorer.rank_candidates([bad_score, good_score])
        assert ranked[0].source_item.external_id == "m12345"
        assert ranked[1].source_item.external_id == "m67890"

    def test_high_seller_rating_bonus(self, scorer, good_ebay_analysis):
        high_rated = ScrapedItem(
            external_id="test1", url="", title="Test",
            price=5000, currency="JPY", seller_rating=4.9, seller_reviews=500,
            image_urls=[], is_available=True,
        )
        low_rated = ScrapedItem(
            external_id="test2", url="", title="Test",
            price=5000, currency="JPY", seller_rating=2.0, seller_reviews=5,
            image_urls=[], is_available=True,
        )
        high_score = scorer.score(high_rated, good_ebay_analysis)
        low_score = scorer.score(low_rated, good_ebay_analysis)
        assert high_score.risk_score > low_score.risk_score

    def test_min_profit_threshold(self, scorer):
        """Items with profit < $10 should be rejected."""
        item = ScrapedItem(
            external_id="test", url="", title="Cheap Item",
            price=100, currency="JPY", seller_rating=4.0,
            image_urls=[], is_available=True,
        )
        analysis = EbayPriceAnalysis(
            query="test", median_price=5.0, avg_price=5.0,
            min_price=3.0, max_price=8.0, total_sold=50, items=[],
        )
        result = scorer.score(item, analysis)
        assert not result.is_viable
