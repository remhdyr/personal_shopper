"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from shopper.db import Database
from shopper.models import Listing, Source


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def listing() -> Listing:
    return Listing(
        source=Source.EBAY,
        source_id="123",
        title="Used metal lathe",
        description="A well-kept hobby lathe.",
        price=5000,
        currency="SEK",
        url="https://example.com/item/123",
        image_urls=["https://example.com/img/123.jpg"],
    )
