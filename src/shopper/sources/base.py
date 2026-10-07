"""Source adapter interface."""

from __future__ import annotations

import abc

from ..models import Listing


class SearchQuery:
    """Normalized search parameters passed to every source."""

    def __init__(
        self,
        text: str,
        min_price: float | None = None,
        max_price: float | None = None,
        location: str = "",
        limit: int = 50,
    ) -> None:
        self.text = text
        self.min_price = min_price
        self.max_price = max_price
        self.location = location
        self.limit = limit


class Source(abc.ABC):
    """A marketplace adapter that returns normalized :class:`Listing` objects."""

    name: str

    @abc.abstractmethod
    async def search(self, query: SearchQuery) -> list[Listing]:
        """Run one search and return normalized listings (best-effort)."""

    async def enrich(self, listing: Listing) -> None:  # noqa: B027 - optional override, no-op by default
        """Fill in extra per-item details not returned by search (e.g. real
        shipping cost), mutating ``listing`` in place. No-op unless overridden.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional override, no-op by default
        """Release any held resources (HTTP clients, etc.)."""
