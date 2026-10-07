"""Tests for source adapter normalization (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from shopper.models import Listing, Source
from shopper.sources._fx import derive_sek_rate, parse_ecb_rates
from shopper.sources._units import add_metric
from shopper.sources.auctionet import AuctionetSource, _strip_html
from shopper.sources.base import SearchQuery
from shopper.sources.blinto import BlintoSource
from shopper.sources.blinto import _reserve_status_from_detail as blinto_reserve_status
from shopper.sources.blocket import BlocketSource, _detail_metadata, _seller_from_detail
from shopper.sources.ebay import EbaySource
from shopper.sources.klaravik import KlaravikSource, _shipping_details_from_detail
from shopper.sources.klaravik import _reserve_status_from_detail as klaravik_reserve_status
from shopper.sources.psauction import PSAuctionSource
from shopper.sources.tradera import TraderaSource, _parse_number


def test_ebay_to_listing_maps_fields():
    src = EbaySource("id", "secret")
    item = {
        "itemId": "v1|123|0",
        "title": "Mitutoyo caliper",
        "shortDescription": "Digital caliper 150mm",
        "price": {"value": "450.00", "currency": "SEK"},
        "image": {"imageUrl": "https://img/1.jpg"},
        "additionalImages": [{"imageUrl": "https://img/2.jpg"}],
        "itemWebUrl": "https://ebay/itm/123",
        "itemLocation": {"city": "Göteborg", "country": "SE"},
    }
    listing = src._to_listing(item)
    assert listing.source is Source.EBAY
    assert listing.source_id == "v1|123|0"
    assert listing.price == 450.0
    assert listing.image_urls == ["https://img/1.jpg", "https://img/2.jpg"]
    assert listing.location == "Göteborg, SE"
    assert listing.uid == "ebay:v1|123|0"


def test_ebay_price_filter_string():
    src = EbaySource("id", "secret", used_only=False)
    q = SearchQuery("lathe", min_price=100, max_price=5000)
    assert src._build_filter(q) == "price:[100..5000],priceCurrency:SEK"
    assert src._build_filter(SearchQuery("lathe")) is None


def test_ebay_used_only_adds_condition_filter():
    src = EbaySource("id", "secret")  # used_only defaults to True
    # Condition clause is present with and without price bounds.
    assert src._build_filter(SearchQuery("lathe")) == "conditions:{USED}"
    q = SearchQuery("lathe", min_price=100, max_price=5000)
    assert (
        src._build_filter(q)
        == "conditions:{USED},price:[100..5000],priceCurrency:SEK"
    )


def test_ebay_price_filter_converts_sek_bounds_to_market_currency():
    src = EbaySource("id", "secret", marketplace="EBAY_DE", used_only=False)
    q = SearchQuery("lathe", min_price=115, max_price=5750)
    # 11.5 SEK per EUR → bounds of 10 and 500 EUR.
    assert (
        src._build_filter(q, sek_per_unit=11.5, currency="EUR")
        == "price:[10..500],priceCurrency:EUR"
    )


def test_ebay_marketplace_currency_mapping():
    assert EbaySource("id", "secret", marketplace="EBAY_DE")._currency == "EUR"
    assert EbaySource("id", "secret", marketplace="EBAY_GB")._currency == "GBP"
    assert EbaySource("id", "secret", marketplace="EBAY_SE")._currency == "SEK"


def test_ebay_to_sek_converts_foreign_price():
    src = EbaySource("id", "secret", marketplace="EBAY_DE")
    item = {
        "itemId": "v1|9|0",
        "title": "German lathe",
        "price": {"value": "200.00", "currency": "EUR"},
        "itemWebUrl": "https://ebay/itm/9",
    }
    listing = src._to_sek(src._to_listing(item), sek_per_unit=11.5)
    assert listing.currency == "SEK"
    assert listing.price == 2300.0


def test_ebay_to_sek_leaves_sek_price_untouched():
    src = EbaySource("id", "secret")
    item = {
        "itemId": "v1|9|0",
        "title": "Swedish caliper",
        "price": {"value": "450.00", "currency": "SEK"},
        "itemWebUrl": "https://ebay/itm/9",
    }
    listing = src._to_sek(src._to_listing(item), sek_per_unit=1.0)
    assert listing.currency == "SEK"
    assert listing.price == 450.0


def test_ebay_parse_shipping_cost_picks_cheapest():
    item = {
        "shippingOptions": [
            {"shippingCost": {"value": "9.99", "currency": "EUR"}},
            {"shippingCost": {"value": "4.50", "currency": "EUR"}},
        ]
    }
    assert EbaySource._parse_shipping_cost(item) == 4.50


def test_ebay_parse_shipping_cost_free_is_zero_not_none():
    item = {"shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "EUR"}}]}
    assert EbaySource._parse_shipping_cost(item) == 0.0


def test_ebay_parse_shipping_cost_none_when_absent_or_calculated():
    assert EbaySource._parse_shipping_cost({}) is None
    # A calculated option with no value must not be treated as free.
    assert EbaySource._parse_shipping_cost(
        {"shippingOptions": [{"shippingCostType": "CALCULATED"}]}
    ) is None


def test_ebay_to_sek_converts_shipping_cost():
    src = EbaySource("id", "secret", marketplace="EBAY_DE")
    item = {
        "itemId": "v1|9|0",
        "title": "German lathe",
        "price": {"value": "200.00", "currency": "EUR"},
        "itemWebUrl": "https://ebay/itm/9",
        "shippingOptions": [{"shippingCost": {"value": "10.00", "currency": "EUR"}}],
    }
    listing = src._to_sek(src._to_listing(item), sek_per_unit=11.5)
    assert listing.known_shipping_cost == 115.0


def test_ebay_to_sek_keeps_free_shipping_zero():
    src = EbaySource("id", "secret", marketplace="EBAY_DE")
    item = {
        "itemId": "v1|9|0",
        "title": "German lathe",
        "price": {"value": "200.00", "currency": "EUR"},
        "itemWebUrl": "https://ebay/itm/9",
        "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "EUR"}}],
    }
    listing = src._to_sek(src._to_listing(item), sek_per_unit=11.5)
    assert listing.known_shipping_cost == 0.0


_ECB_SAMPLE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"'
    ' xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
    "<Cube><Cube time='2026-08-12'>"
    "<Cube currency='USD' rate='1.10'/>"
    "<Cube currency='GBP' rate='0.85'/>"
    "<Cube currency='SEK' rate='11.50'/>"
    "</Cube></Cube></gesmes:Envelope>"
)


def test_parse_ecb_rates_includes_eur_base():
    rates = parse_ecb_rates(_ECB_SAMPLE)
    assert rates["EUR"] == 1.0
    assert rates["SEK"] == 11.50
    assert rates["GBP"] == 0.85


def test_derive_sek_rate_for_each_currency():
    rates = parse_ecb_rates(_ECB_SAMPLE)
    assert derive_sek_rate(rates, "SEK") == 1.0
    assert derive_sek_rate(rates, "EUR") == 11.50
    # SEK per GBP = SEK/EUR ÷ GBP/EUR = 11.50 / 0.85.
    assert derive_sek_rate(rates, "GBP") == 11.50 / 0.85
    assert derive_sek_rate(rates, "JPY") is None


def test_add_metric_inches():
    assert add_metric('6" bench vise') == '6" (15.2 cm) bench vise'
    assert add_metric("6in bar") == "6in (15.2 cm) bar"
    assert add_metric("6 inch bar") == "6 inch (15.2 cm) bar"
    assert add_metric("12 inches long") == "12 inches (30.5 cm) long"


def test_add_metric_fractions_and_mixed_numbers():
    assert add_metric('1/2" drive') == '1/2" (1.3 cm) drive'
    assert add_metric('6-1/2" jaw') == '6-1/2" (16.5 cm) jaw'
    assert add_metric('6½"') == '6½" (16.5 cm)'


def test_add_metric_feet_pounds_ounces():
    assert add_metric("4 ft table") == "4 ft (121.9 cm) table"
    assert add_metric("10 lb anvil") == "10 lb (4.5 kg) anvil"
    assert add_metric("8 oz hammer") == "8 oz (226.8 g) hammer"
    # Small weights render in grams, not fractional kilos.
    assert add_metric("1 lb weight") == "1 lb (453.6 g) weight"


def test_add_metric_ignores_bare_in_word_and_metric_text():
    # "in" as the English word must not be treated as inches.
    assert add_metric("6 in stock") == "6 in stock"
    # Already-metric listings are left untouched.
    assert add_metric("150 mm caliper") == "150 mm caliper"
    # An existing parenthetical metric value isn't doubled up.
    assert add_metric('6" (15.2 cm) vise') == '6" (15.2 cm) vise'


def test_ebay_to_listing_annotates_imperial_units():
    src = EbaySource("id", "secret")
    item = {
        "itemId": "v1|7|0",
        "title": '6" bench vise',
        "shortDescription": "Heavy 10 lb cast iron",
        "price": {"value": "450.00", "currency": "SEK"},
        "itemWebUrl": "https://ebay/itm/7",
    }
    listing = src._to_listing(item)
    assert listing.title == '6" (15.2 cm) bench vise'
    assert listing.description == "Heavy 10 lb (4.5 kg) cast iron"


def test_tradera_parse_and_filter():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <SearchResponse xmlns="http://api.tradera.com">
          <SearchResult>
            <Items>
              <Id>555</Id>
              <ShortDescription>Bench vise 100mm</ShortDescription>
              <BuyItNowPrice>300</BuyItNowPrice>
              <ThumbnailLink>https://t/1.jpg</ThumbnailLink>
            </Items>
            <Items>
              <Id>556</Id>
              <ShortDescription>Expensive lathe</ShortDescription>
              <BuyItNowPrice>99999</BuyItNowPrice>
            </Items>
          </SearchResult>
        </SearchResponse>
      </soap:Body>
    </soap:Envelope>"""
    listings = src._parse(xml)
    assert [ltng.source_id for ltng in listings] == ["555", "556"]
    assert listings[0].price == 300.0
    assert listings[0].url == "https://www.tradera.com/item/555"

    filtered = src._apply_price_filter(
        listings, SearchQuery("lathe", min_price=100, max_price=5000)
    )
    assert [ltng.source_id for ltng in filtered] == ["555"]


def test_tradera_parse_shipping_cost_picks_cheapest_option():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult>
            <ShippingOptions>
              <ShippingOptionId>11</ShippingOptionId>
              <Cost>79</Cost>
            </ShippingOptions>
            <ShippingOptions>
              <ShippingOptionId>12</ShippingOptionId>
              <Cost>0</Cost>
            </ShippingOptions>
          </GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    assert src._parse_shipping_cost(xml) == 0.0


def test_tradera_parse_shipping_cost_none_when_absent():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult></GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    assert src._parse_shipping_cost(xml) is None


def test_tradera_parse_end_date_localises_to_utc():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult>
            <EndDate>2026-08-15T18:30:00</EndDate>
          </GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    ends_at = src._parse_end_date(xml)
    # Naive Swedish local time (CEST, +02:00) becomes 16:30 UTC.
    assert ends_at == datetime(2026, 8, 15, 16, 30, tzinfo=UTC)


def test_tradera_parse_end_date_none_when_absent():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult></GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    assert src._parse_end_date(xml) is None


def test_auctionet_to_listing_sets_ends_at_from_unix_timestamp():
    src = AuctionetSource()
    item = {
        "id": 42,
        "title": "Metal lathe",
        "currency": "SEK",
        "url": "https://auctionet.com/en/42",
        "next_bid_amount": 1500,
        "published_at": 1786556618,
        "ends_at": 1787163300,
    }
    listing = src._to_listing(item)
    assert listing.ends_at == datetime.fromtimestamp(1787163300, tz=UTC)


def test_auctionet_to_listing_ends_at_none_when_absent():
    src = AuctionetSource()
    item = {"id": 7, "title": "No end time", "currency": "SEK", "url": "https://x/7"}
    assert src._to_listing(item).ends_at is None


def test_listing_is_ended():
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    base = {"source": Source.AUCTIONET, "source_id": "1", "title": "x", "url": "https://x/1"}
    # No end time -> treated as open (e.g. fixed-price offers).
    assert Listing(**base).is_ended(now) is False
    # End time in the past -> ended.
    assert Listing(**base, ends_at=now - timedelta(minutes=1)).is_ended(now) is True
    # End time in the future -> still open.
    assert Listing(**base, ends_at=now + timedelta(hours=1)).is_ended(now) is False


def test_tradera_parse_images_prefers_normal_format():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult>
            <DetailedImageLinks>
              <Url>http://img.tradera.net/images/1/a.jpg</Url>
              <Format>normal</Format>
            </DetailedImageLinks>
            <DetailedImageLinks>
              <Url>https://img.tradera.net/thumbs/1/a.jpg</Url>
              <Format>thumbnail</Format>
            </DetailedImageLinks>
            <DetailedImageLinks>
              <Url>http://img.tradera.net/images/1/b.jpg</Url>
              <Format>normal</Format>
            </DetailedImageLinks>
          </GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    assert src._parse_images(xml) == [
        "https://img.tradera.net/images/1/a.jpg",
        "https://img.tradera.net/images/1/b.jpg",
    ]


def test_tradera_parse_images_falls_back_to_imagelinks():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult>
            <ImageLinks>
              <string>http://img.tradera.net/images/1/a.jpg</string>
              <string>http://img.tradera.net/images/1/b.jpg</string>
            </ImageLinks>
          </GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    assert src._parse_images(xml) == [
        "https://img.tradera.net/images/1/a.jpg",
        "https://img.tradera.net/images/1/b.jpg",
    ]


def test_tradera_parse_images_empty_when_absent():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetItemResponse xmlns="http://api.tradera.com">
          <GetItemResult></GetItemResult>
        </GetItemResponse>
      </soap:Body>
    </soap:Envelope>"""
    assert src._parse_images(xml) == []


def test_tradera_parse_number_tolerates_decimals_and_separators():
    assert _parse_number("79") == 79.0
    assert _parse_number("79.00") == 79.0
    assert _parse_number("79,00") == 79.0
    assert _parse_number("1 250") == 1250.0
    assert _parse_number("1\xa0250,50") == 1250.5
    assert _parse_number("") is None
    assert _parse_number(None) is None
    assert _parse_number("n/a") is None


def test_tradera_parses_decimal_price():
    src = TraderaSource("app", "key")
    xml = """<?xml version="1.0"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <SearchResponse xmlns="http://api.tradera.com">
          <SearchResult>
            <Items>
              <Id>777</Id>
              <ShortDescription>Micrometer</ShortDescription>
              <BuyItNowPrice>249,50</BuyItNowPrice>
            </Items>
          </SearchResult>
        </SearchResponse>
      </soap:Body>
    </soap:Envelope>"""
    listings = src._parse(xml)
    assert listings[0].price == 249.5


def test_auctionet_strip_html_unescapes_and_collapses():
    assert _strip_html("<p>h 182 cm.</p>") == "h 182 cm."
    assert _strip_html("a &amp; b") == "a & b"
    assert _strip_html("<p>one</p>\n<p>two</p>") == "one two"
    assert _strip_html("") == ""


def test_auctionet_to_listing_maps_fields():
    src = AuctionetSource()
    item = {
        "id": 5261897,
        "title": "SVARV, metall.",
        "description": "<p>Fungerar fint.</p>",
        "condition": "<p>Bruksslitage.</p>",
        "currency": "SEK",
        "estimate": 800,
        "starting_bid_amount": 300,
        "next_bid_amount": 300,
        "published_at": 1785709125,
        "location": "Halmstad",
        "house": "Halmstads Auktionskammare",
        "url": "https://auctionet.com/en/5261897-lathe",
        "images": [{"thumb": "t.jpg", "w640": "w.jpg", "hd": "hd.jpg"}],
    }
    listing = src._to_listing(item)
    assert listing.source is Source.AUCTIONET
    assert listing.uid == "auctionet:5261897"
    assert listing.price == 300.0
    assert listing.currency == "SEK"
    assert listing.image_urls == ["w.jpg"]  # prefers the w640 render
    assert listing.location == "Halmstad"
    assert listing.posted_at is not None
    assert listing.age_minutes() is not None
    assert "Fungerar fint." in listing.description
    assert "Condition: Bruksslitage." in listing.description
    assert "Auctioneer estimate: 800 SEK." in listing.description


def test_auctionet_keep_filters_currency_and_price():
    src = AuctionetSource(currency="SEK")
    query = SearchQuery("svarv", min_price=100, max_price=5000)
    sek = src._to_listing({"id": 1, "currency": "SEK", "next_bid_amount": 300})
    eur = src._to_listing({"id": 2, "currency": "EUR", "next_bid_amount": 300})
    pricey = src._to_listing({"id": 3, "currency": "SEK", "next_bid_amount": 99999})
    assert src._keep(sek, query)
    assert not src._keep(eur, query)  # wrong currency
    assert not src._keep(pricey, query)  # above max price


def test_klaravik_parse_maps_card_fields_and_filters():
    src = KlaravikSource()
    html = """
    <a href="/auktion/produkt/3260001-metallsvarv-storebro/" title="Metallsvarv Storebro">
      <img src="https://media.se.klaravik.com/public/productimages/3260001.jpg"
           alt="Metallsvarv Storebro">
      <span class="product_card__mark-fav addFav_3260001" data-prod-id="3260001"
            data-auction-start="2026-08-24T09:00:00+02:00"
            data-auction-close="2026-08-31T12:26:00+02:00"></span>
      <div class="product_card__title">Metallsvarv Storebro</div>
      <div class="product_card__info-text">Malmö</div>
      <span class="product_card__current-bid">4 500 SEK</span>
    </a>
    <a href="/auktion/produkt/3260002-traktor/" title="Traktor">
      <span class="product_card__mark-fav addFav_3260002" data-prod-id="3260002"></span>
      <span class="product_card__current-bid">50 000 SEK</span>
    </a>
    """

    listings = src._parse(html, SearchQuery("svarv", max_price=10_000))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source is Source.KLARAVIK
    assert listing.source_id == "3260001"
    assert listing.title == "Metallsvarv Storebro"
    assert listing.price == 4500.0
    assert listing.location == "Malmö"
    assert listing.image_urls == ["https://media.se.klaravik.com/public/productimages/3260001.jpg"]
    assert listing.url == "https://www.klaravik.se/auktion/produkt/3260001-metallsvarv-storebro/"
    assert listing.posted_at == datetime(2026, 8, 24, 7, 0, tzinfo=UTC)
    assert listing.ends_at == datetime(2026, 8, 31, 10, 26, tzinfo=UTC)


def test_klaravik_reserve_status_from_detail():
    assert klaravik_reserve_status(
        '"product":{"resPriceReached":false,"zeroReserve":false}'
    ) is False
    assert klaravik_reserve_status(
        '"product":{"resPriceReached":true,"zeroReserve":false}'
    ) is True
    assert klaravik_reserve_status(
        '"product":{"resPriceReached":false,"zeroReserve":true}'
    ) is True
    assert klaravik_reserve_status("no product data") is None


def test_klaravik_shipping_details_from_detail():
    detail = """
    <div class="object-freight__content">
      <p><b>Boka frakt?</b> Frakt kan undersökas på förfrågan inom Sverige.</p>
    </div>
    """

    assert _shipping_details_from_detail(detail) == (
        "Boka frakt? Frakt kan undersökas på förfrågan inom Sverige."
    )


def test_blinto_parse_maps_card_fields_and_filters():
    src = BlintoSource()
    html = """
    <a href="/auction/Svarv-Abene-272193-200519/">
      <div class="product-img"><img src="https://cdn.blinto.se/object/272193/lathe.jpg/600x450f?006"
           alt="Svarv Abene"></div>
      <div class="product-content">
        <span class="brand-type">Svarv</span>
        <span class="h3-second-line">Abene</span>
        <div translate="no" class="card-location">Sölvesborg</div>
        <span translate="no" class="objectprice">262 000 SEK</span>
      </div>
    </a>
    <a href="/auction/Skogsmaskin-272183-200518/">
      <img src="/images/icon_youtube.svg" alt="gt">
      <span class="objectprice">999 000 SEK</span>
    </a>
    """

    listings = src._parse(html, SearchQuery("svarv", max_price=300_000))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source is Source.BLINTO
    assert listing.source_id == "Svarv-Abene-272193-200519"
    assert listing.title == "Svarv Abene"
    assert listing.price == 262000.0
    assert listing.location == "Sölvesborg"
    assert listing.description.startswith("Seller-provided shipping: unavailable.")
    assert listing.image_urls == ["https://cdn.blinto.se/object/272193/lathe.jpg/600x450f?006"]
    assert listing.url == "https://www.blinto.se/auction/Svarv-Abene-272193-200519/"


def test_blinto_reserve_status_from_detail():
    assert blinto_reserve_status(
        '<li id="li-resprice-reached_desktop">Reservationspris ej uppnått</li>'
    ) is False
    assert blinto_reserve_status(
        '<li id="li-resprice-reached">Reservationspris uppnått</li>'
    ) is True
    assert blinto_reserve_status("no reserve status") is None


def test_psauction_parse_maps_card_fields_and_ignores_waf_page():
    src = PSAuctionSource()
    waf = """<html><script>window.gokuProps = {"key":"challenge"};</script></html>"""
    assert src._parse(waf, SearchQuery("svarv")) == []

    xml = """
    <data>
      <result>
        <id>1485511</id>
        <sku>1485511</sku>
        <name>Metallsvarv Clausing</name>
        <shortDesc>Metallsvarv Clausing i fungerande skick</shortDesc>
        <category>Industrial machinery</category>
        <auctionTitle>Workshop clearance</auctionTitle>
        <shipping_type>No shipping</shipping_type>
        <price>12500.00</price>
        <currency>SEK</currency>
        <url>https://psauction.com/item/view/1485511/metallsvarv-clausing</url>
        <imageUrl>https://d2q01ftr6ua4w.cloudfront.net/assets/images/12345.jpg</imageUrl>
        <location>Stockholm</location>
        <endingTime>2026-08-27 12:38:00</endingTime>
      </result>
    </data>
    """

    listings = src._parse(xml, SearchQuery("svarv", min_price=1_000, max_price=20_000))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source is Source.PSAUCTION
    assert listing.source_id == "1485511"
    assert listing.title == "Metallsvarv Clausing"
    assert listing.price == 12500.0
    assert listing.location == "Stockholm"
    assert listing.description.startswith("PS Auction shipping: No shipping")
    assert listing.known_shipping_cost is None
    assert listing.url == "https://psauction.com/item/view/1485511/metallsvarv-clausing"
    assert listing.image_urls == ["https://d2q01ftr6ua4w.cloudfront.net/assets/images/12345.jpg"]
    assert listing.ends_at == datetime(2026, 8, 27, 10, 38, tzinfo=UTC)
    assert listing.is_ended(datetime(2026, 10, 5, tzinfo=UTC))


def test_psauction_shipping_included_maps_to_zero_cost():
    src = PSAuctionSource()
    xml = """
    <data><result>
      <id>included-shipping</id>
      <name>Tool cabinet</name>
      <shipping_type>Shipping included (within Sweden)</shipping_type>
      <price>1000</price>
      <currency>SEK</currency>
      <url>https://psauction.com/item/view/included-shipping</url>
      <location>Skåne, Malmö</location>
    </result></data>
    """

    listings = src._parse(xml, SearchQuery("tool"))

    assert len(listings) == 1
    assert listings[0].known_shipping_cost == 0.0


def test_blocket_public_page_parse_maps_json_ld_products_and_filters():
    src = BlocketSource()
    html = """
    <script type="application/ld+json" id="seoStructuredData">
    {"@context":"https://schema.org","@type":"CollectionPage","mainEntity":{
      "@type":"ItemList","itemListElement":[
        {"@type":"ListItem","position":1,"item":{"@type":"Product",
          "description":"Svarv och Bordcirkelsåg",
          "offers":{"@type":"Offer","price":"500","priceCurrency":"SEK"},
          "name":"Svarv och Bordcirkelsåg",
          "image":"https://images.blocketcdn.se/dynamic/default/item/26054742/img",
          "url":"https://www.blocket.se/recommerce/forsale/item/26054742"}},
        {"@type":"ListItem","position":2,"item":{"@type":"Product",
          "description":"Fräs i fint skick",
          "offers":{"@type":"Offer","price":"2000","priceCurrency":"SEK"},
          "name":"Fräs",
          "url":"https://www.blocket.se/recommerce/forsale/item/26054743"}}
      ]}}
    </script>
    """

    listings = src._parse_public_page(html, SearchQuery("svarv", min_price=100, max_price=1000))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source is Source.BLOCKET
    assert listing.source_id == "26054742"
    assert listing.title == "Svarv och Bordcirkelsåg"
    assert listing.description == "Svarv och Bordcirkelsåg"
    assert listing.price == 500.0
    assert listing.currency == "SEK"
    assert listing.url == "https://www.blocket.se/recommerce/forsale/item/26054742"
    assert listing.image_urls == ["https://images.blocketcdn.se/dynamic/default/item/26054742/img"]


def test_blocket_seller_from_detail_extracts_shop_profile_name():
    state = {
        "loaderData": {
            "item-recommerce": {
                "shopProfileData": {"name": "Blinto"},
                "itemData": {
                    "location": {"postalName": "Malmö", "postalCode": "21218"}
                },
                  "transactableData": {"eligibleForShipping": False},
            }
        }
    }
    detail = (
        "<script>window.__staticRouterHydrationData = JSON.parse("
        f"{json.dumps(json.dumps(state))});</script>"
    )

    assert _seller_from_detail(detail) == "Blinto"
    assert _detail_metadata(detail) == (
      "Blinto",
      True,
      "Malmö, 21218",
      "Blocket shipping is not currently offered; pickup is required.",
      None,
    )


def test_blocket_detail_identifies_private_seller():
    state = {
        "loaderData": {
            "item-recommerce": {
                "shopProfileData": None,
                "itemData": {
                    "location": {"postalName": "Degeberga", "postalCode": "29731"}
                },
                  "transactableData": {
                    "eligibleForShipping": True,
                    "sellerPaysShipping": False,
                  },
                  "transactableUiData": {
                    "sections": {
                      "sidebar": {
                        "optedIn": {
                          "shippingPrice": {
                            "text": "Frakt från 39 kr + köpskydd 34 kr"
                          }
                        }
                      }
                    }
                  },
            }
        }
    }
    detail = (
        "<script>window.__staticRouterHydrationData = JSON.parse("
        f"{json.dumps(json.dumps(state))});</script>"
    )

    assert _detail_metadata(detail) == (
      "",
      False,
      "Degeberga, 29731",
      "Blocket shipping offered: Frakt från 39 kr + köpskydd 34 kr.",
      39.0,
    )

    direct_json = json.dumps(state["loaderData"]["item-recommerce"])
    assert _detail_metadata(direct_json) == _detail_metadata(detail)
