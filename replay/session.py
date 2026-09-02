"""
replay/session.py — Replay session controller.

The ReplaySession orchestrates:
    1. Loading the recording via RecordingLoader.
    2. Building the RequestMatcher from the recorded transactions.
    3. Launching a Playwright browser + context in Strict Mock Mode.
    4. Installing the network interceptor BEFORE any page is created.
    5. Creating the initial page and navigating to the first recorded URL.
    6. Running through the recorded navigation sequence.
    7. Collecting mismatches and producing a final report.

Strict Mock Mode
----------------
In Strict Mock Mode the catch-all route `**/*` is installed on the browser
context before any page is opened.  This means:
    - Every request passes through NetworkInterceptor.handle_route.
    - Unexpected requests raise UnexpectedRequest and are blocked.
    - The real network is never contacted for application traffic.
    - Browser-internal URLs (chrome://, data:, etc.) are allowed through
      automatically.

This is the ONLY mode exposed in Phase 1.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from replay.coordinator import SequenceCoordinator
from replay.errors import (
    MissingPage,
    ReplayFatal,
    ReplayWarning,
    UnsupportedReplayFeature,
)
from replay.loader import RecordingLoader
from replay.matcher import RequestMatcher
from replay.network import NetworkInterceptor
from replay.pages import PageManager

logger = logging.getLogger(__name__)


class ReplaySession:
    """Controls one complete replay session.

    Parameters
    ----------
    capture_dir:  Path to the recording session directory.
    headless:     Whether to run the browser headlessly.
    strict:       If True (default), unexpected requests are fatal.
    """

    def __init__(
        self,
        capture_dir: str,
        *,
        headless: bool = True,
        strict: bool = True,
    ):
        self.capture_dir = capture_dir
        self.headless = headless
        self.strict = strict

        # Sub-components
        self.loader = RecordingLoader(capture_dir)
        self.matcher = RequestMatcher()
        self.page_manager = PageManager()
        self.coordinator = SequenceCoordinator()
        self.interceptor: Optional[NetworkInterceptor] = None

        # Mismatch collection
        self._fatal_mismatches: List[ReplayFatal] = []
        self._warnings: List[ReplayWarning] = []

        # Playwright handles (set during run())
        self._playwright = None
        self._browser = None
        self._context = None
        self._initial_page = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self) -> "ReplayResult":
        """Execute the full replay session and return a result summary."""
        try:
            self._load_recording()
            self._launch_browser()
            self._install_network_interceptor()
            self._replay_navigations()
        except ReplayFatal as exc:
            self._fatal_mismatches.append(exc)
            logger.error("Fatal replay error: %s", exc)
        finally:
            self._shutdown()

        return ReplayResult(
            capture_dir=self.capture_dir,
            fatal_mismatches=list(self._fatal_mismatches),
            warnings=list(self._warnings),
            fulfilled_count=self.interceptor.fulfilled_count if self.interceptor else 0,
            aborted_count=self.interceptor.aborted_count if self.interceptor else 0,
            pending_transactions=self.matcher.all_pending(),
        )

    # ------------------------------------------------------------------ #
    # Phase steps
    # ------------------------------------------------------------------ #

    def _load_recording(self) -> None:
        """Parse and validate the recording, populate the matcher and coordinator."""
        logger.info("Loading recording: %s", self.capture_dir)

        session = self.loader.session_info()
        logger.info(
            "Session: %s  browser=%s",
            session.get("session_id"),
            session.get("browser"),
        )

        sequences = []
        for txn in self.loader.iter_transactions():
            sequences.append(txn["sequence"])

        # Reload for matcher — RecordingLoader streams; we need a second pass
        self.matcher.load_transactions(self.loader.iter_transactions())
        self.coordinator.load_sequences(sequences)

        # Discover all page_ids
        self.page_manager.discover_page_ids(self.loader)
        logger.info(
            "Loaded %d sequences, %d page_ids",
            len(sequences),
            len(self.page_manager.all_recorded_page_ids),
        )

    def _launch_browser(self) -> None:
        """Start Playwright browser and create a context."""
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(ignore_https_errors=True)
        logger.info("Browser launched (headless=%s)", self.headless)

    def _install_network_interceptor(self) -> None:
        """Install the catch-all route on the browser context.

        MUST be called before any page is created to guarantee coverage.
        """
        self.interceptor = NetworkInterceptor(
            loader=self.loader,
            matcher=self.matcher,
            page_manager=self.page_manager,
            on_mismatch=self._on_mismatch,
            strict=self.strict,
        )

        self._context.route(
            "**/*",
            lambda route, request: self.interceptor.handle_route(route, request),
        )
        logger.info("Strict Mock Mode: network interceptor installed")

    def _replay_navigations(self) -> None:
        """Replay the navigation history in recorded order."""
        # Determine initial page and URL from the first navigation record
        nav_records = list(self.loader.iter_navigations())
        if not nav_records:
            logger.warning("No navigation records found — nothing to replay")
            return

        # Group navigations by page_id, process in sequence order
        # (navigation.jsonl is already in recorded order)
        recorded_page_ids = self.page_manager.all_recorded_page_ids
        if not recorded_page_ids:
            logger.warning("No page_ids found in recording")
            return

        primary_page_id = recorded_page_ids[0]

        # Open the initial page
        self._initial_page = self._context.new_page()
        try:
            self.page_manager.register_page(primary_page_id, self._initial_page)
        except Exception as exc:
            self._on_mismatch(ReplayFatal(f"Cannot register initial page: {exc}"))
            return

        # Navigate through each navigation record for the primary page
        for nav in nav_records:
            pid = nav.get("page_id")
            to_url = nav.get("to_url", "")

            if pid != primary_page_id:
                # Multi-page / popup replay is Phase 1 foundation only
                self._warn(UnsupportedReplayFeature(
                    f"Popup/secondary page replay not yet implemented "
                    f"(page_id={pid!r})",
                    page_id=pid,
                    url=to_url,
                ))
                continue

            if not to_url or to_url in ("about:blank", ""):
                continue

            try:
                page = self.page_manager.get_page(pid)
                logger.info("Navigating page %s → %s", pid, to_url)
                page.goto(to_url, wait_until="domcontentloaded", timeout=15_000)
            except MissingPage as exc:
                self._on_mismatch(exc)
                break
            except Exception as exc:
                logger.warning("Navigation to %s raised: %s", to_url, exc)
                # Navigation errors during replay are warnings unless they
                # prevent subsequent transactions from matching
                self._warn(UnsupportedReplayFeature(
                    f"Navigation error: {exc}",
                    url=to_url,
                    page_id=pid,
                ))

    # ------------------------------------------------------------------ #
    # Mismatch handling
    # ------------------------------------------------------------------ #

    def _on_mismatch(self, exc: ReplayFatal) -> None:
        """Collect a fatal mismatch. Called from within route handlers."""
        self._fatal_mismatches.append(exc)
        logger.error("[REPLAY FATAL] %s", exc)

    def _warn(self, exc: ReplayWarning) -> None:
        self._warnings.append(exc)
        logger.warning("[REPLAY WARN] %s", exc)

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def _shutdown(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception as exc:
            logger.debug("Context close error (ignored): %s", exc)
        try:
            if self._browser:
                self._browser.close()
        except Exception as exc:
            logger.debug("Browser close error (ignored): %s", exc)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as exc:
            logger.debug("Playwright stop error (ignored): %s", exc)
        logger.info("Replay session shut down")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class ReplayResult:
    """Summary of a completed replay session."""

    def __init__(
        self,
        capture_dir: str,
        fatal_mismatches: List[ReplayFatal],
        warnings: List[ReplayWarning],
        fulfilled_count: int,
        aborted_count: int,
        pending_transactions: List[dict],
    ):
        self.capture_dir = capture_dir
        self.fatal_mismatches = fatal_mismatches
        self.warnings = warnings
        self.fulfilled_count = fulfilled_count
        self.aborted_count = aborted_count
        self.pending_transactions = pending_transactions

    @property
    def success(self) -> bool:
        return len(self.fatal_mismatches) == 0

    def __str__(self) -> str:
        lines = [
            f"ReplayResult: {'PASS' if self.success else 'FAIL'}",
            f"  Fulfilled: {self.fulfilled_count}",
            f"  Aborted:   {self.aborted_count}",
            f"  Pending:   {len(self.pending_transactions)}",
            f"  Fatals:    {len(self.fatal_mismatches)}",
            f"  Warnings:  {len(self.warnings)}",
        ]
        for f in self.fatal_mismatches:
            lines.append(f"  [FATAL] {f}")
        for w in self.warnings:
            lines.append(f"  [WARN]  {w}")
        return "\n".join(lines)
