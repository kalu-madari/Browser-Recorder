"""
replay/pages.py — Page / tab lifecycle manager for replay.

Maps recorded page_id strings to live Playwright Page objects.

Design rules
------------
- page_id is the canonical identity; never use Python id() or object address.
- Multiple pages are supported (tabs, popups).
- A page mapping is set once (on page creation) and cleared on closure.
- If a page_id cannot be deterministically mapped, MissingPage is raised.
- The first recorded page_id (lowest sequence document request) is treated
  as the initial page opened by the replay browser.

Phase 1 scope
-------------
Only top-level Page objects are managed here.  iframe / frame hierarchy
is tracked in the network interceptor via recorded frame_id, but is NOT
actively controlled in Phase 1.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from replay.errors import MissingPage, PageIdentityAmbiguous, PageLifecycleMismatch

logger = logging.getLogger(__name__)


class PageManager:
    """Maintains the mapping between recorded page_id values and live Pages."""

    def __init__(self):
        # page_id -> live Playwright Page
        self._pages: Dict[str, object] = {}
        # Ordered list of all page_ids encountered in the recording
        self._recorded_page_ids: List[str] = []

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_page(self, page_id: str, page) -> None:
        """Associate a recorded page_id with a live Playwright Page.

        Raises PageIdentityAmbiguous if page_id is already registered.
        """
        if page_id in self._pages:
            raise PageIdentityAmbiguous(
                f"page_id {page_id!r} is already registered — duplicate page identity",
                page_id=page_id,
            )
        self._pages[page_id] = page
        if page_id not in self._recorded_page_ids:
            self._recorded_page_ids.append(page_id)
        logger.debug("Registered page: %s", page_id)

    def close_page(self, page_id: str) -> None:
        """Remove a page mapping when the recorded page was closed.

        Raises PageLifecycleMismatch if the page_id was never registered.
        """
        if page_id not in self._pages:
            raise PageLifecycleMismatch(
                f"Cannot close page {page_id!r} — not registered",
                page_id=page_id,
            )
        del self._pages[page_id]
        logger.debug("Closed page: %s", page_id)

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def get_page(self, page_id: str):
        """Return the live Playwright Page for page_id.

        Raises MissingPage if the page_id is not in the active mapping.
        """
        page = self._pages.get(page_id)
        if page is None:
            raise MissingPage(
                f"No live page for recorded page_id {page_id!r}",
                page_id=page_id,
            )
        return page

    def get_page_optional(self, page_id: str) -> Optional[object]:
        """Return the live page or None without raising."""
        return self._pages.get(page_id)

    def get_page_id(self, page) -> Optional[str]:
        """Reverse lookup: return the page_id for a live Playwright Page."""
        for pid, p in self._pages.items():
            if p == page:
                return pid
        return None

    @property
    def active_page_ids(self) -> List[str]:
        return list(self._pages.keys())

    @property
    def all_recorded_page_ids(self) -> List[str]:
        return list(self._recorded_page_ids)

    def discover_page_ids(self, loader) -> List[str]:
        """Scan the recording's manifest for all unique page_ids in sequence order.

        Returns a deduplicated list preserving first-appearance order.
        """
        seen = []
        for txn in loader.iter_transactions():
            pid = txn.get("page_id")
            if pid and pid not in seen:
                seen.append(pid)
                logger.debug("Discovered page_id: %s", pid)
        # Also scan navigations which may reference popup page_ids
        for nav in loader.iter_navigations():
            pid = nav.get("page_id")
            if pid and pid not in seen:
                seen.append(pid)
        self._recorded_page_ids = seen
        return seen
