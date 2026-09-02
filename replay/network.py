"""
replay/network.py — Playwright network interceptor for replay.

In STRICT MOCK MODE (the only mode in Phase 1), every incoming request from
the replay browser must be:

    1. Matched against a recorded transaction by the RequestMatcher.
    2. Fulfilled with the recorded response (status, headers, body).
    3. OR aborted with the recorded failure reason.

Any request that cannot be matched raises UnexpectedRequest (fatal).
No request is ever forwarded to the real network.

Security contract
-----------------
Strict Mock Mode is enforced at the route level.  The catch-all route
`**/*` is installed BEFORE any page is created.  This means EVERY request
— including requests made by browser internals (extension pages, etc.) —
passes through this handler.  Browser-internal URLs (chrome-extension://,
data:, about:, etc.) are detected and allowed through as benign.

Supported recorded correlation states
--------------------------------------
    PLAYWRIGHT_ONLY         — normal Playwright-recorded response
    CDP_CORRELATED          — Playwright + CDP correlated response
    CDP_ONLY_FAILED         — recorded network failure (aborted by network)
    PLAYWRIGHT_ONLY_FAILED  — recorded network failure (aborted by Playwright)

Unsupported (Phase 1)
---------------------
    WebSocket replay  — recorded but not intercepted in Phase 1
    Redirect chaining — served as single recorded response (status preserved)
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from replay.errors import (
    MissingResponseBody,
    ReplayFatal,
    UnexpectedRequest,
    UnsupportedReplayFeature,
)
from replay.loader import RecordingLoader
from replay.matcher import RequestMatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Benign / browser-internal URL prefixes that bypass strict matching
# ---------------------------------------------------------------------------

_BYPASS_PREFIXES = (
    "chrome-extension://",
    "chrome://",
    "devtools://",
    "data:",
    "about:",
    "blob:",
)


def _is_internal(url: str) -> bool:
    return any(url.startswith(p) for p in _BYPASS_PREFIXES)


# ---------------------------------------------------------------------------
# Abort reasons — mapping from recorded failure state to Playwright abort code
# ---------------------------------------------------------------------------

# Playwright route.abort() accepts a limited set of error codes.
# CDP network errors map imperfectly to these.  We use "failed" as the
# default because it is the safest / most generic.

_ABORT_CODE = "failed"  # default Playwright abort reason for all recorded failures


# ---------------------------------------------------------------------------
# NetworkInterceptor
# ---------------------------------------------------------------------------

class NetworkInterceptor:
    """Installs a Playwright route handler that replays recorded responses.

    Parameters
    ----------
    loader:  The RecordingLoader for the current session.
    matcher: Pre-loaded RequestMatcher for the current session.
    page_manager: The PageManager for resolving live pages to recorded page_ids.
    on_mismatch: Callable invoked with a ReplayFatal when an unrecoverable
                 mismatch occurs.  Typically adds to a mismatch list and/or
                 raises immediately.
    strict:  If True (default), any unmatched request raises UnexpectedRequest.
             If False, unexpected requests are logged and allowed through.
             WARNING: non-strict mode should only be used for debugging.
    """

    def __init__(
        self,
        loader: RecordingLoader,
        matcher: RequestMatcher,
        page_manager: "PageManager",
        on_mismatch: Callable[[ReplayFatal], None],
        strict: bool = True,
    ):
        self.loader = loader
        self.matcher = matcher
        self.page_manager = page_manager
        self.on_mismatch = on_mismatch
        self.strict = strict
        self._fulfilled: List[int] = []   # matched sequence numbers (audit trail)
        self._aborted: List[int] = []     # aborted sequence numbers

    # ------------------------------------------------------------------ #
    # Playwright route handler
    # ------------------------------------------------------------------ #

    def handle_route(self, route, request):
        """Called by Playwright for every intercepted request."""
        url = request.url
        method = request.method.upper()

        # --- Browser-internal URLs: always allow through ---
        if _is_internal(url):
            try:
                route.continue_()
            except Exception:
                pass
            return

        # --- Determine page context ---
        page_id = None
        try:
            page = request.frame.page
            page_id = self.page_manager.get_page_id(page)
        except Exception:
            pass

        # --- Read request body (may be empty) ---
        try:
            post_data = request.post_data_buffer  # raw bytes, may be None
        except Exception:
            post_data = None

        # --- Attempt to match a failure first (failures have no response body) ---
        corr_fail = self._try_match_failure(url, method, page_id)
        if corr_fail is not None:
            self._abort_route(route, corr_fail, url, method)
            return

        # --- Normal match ---
        try:
            txn = self.matcher.match(url, method, body=post_data, page_id=page_id)
        except UnexpectedRequest as exc:
            self.on_mismatch(exc)
            if self.strict:
                # Block the request hard — return a 502 to avoid browser hang
                try:
                    route.fulfill(
                        status=502,
                        headers={"content-type": "text/plain"},
                        body=f"[REPLAY] Unexpected request: {method} {url}".encode(),
                    )
                except Exception:
                    try:
                        route.abort("failed")
                    except Exception:
                        pass
            else:
                try:
                    route.continue_()
                except Exception:
                    pass
            return
        except ReplayFatal as exc:
            self.on_mismatch(exc)
            try:
                route.abort("failed")
            except Exception:
                pass
            return

        # --- Serve recorded response ---
        self._fulfill_route(route, txn, url, method)

    # ------------------------------------------------------------------ #
    # Internal: fulfill
    # ------------------------------------------------------------------ #

    def _fulfill_route(self, route, txn: dict, url: str, method: str):
        """Fulfill a route with the recorded response."""
        status = txn.get("status") or 200
        headers = dict(txn.get("response_headers") or {})
        seq = txn.get("sequence", -1)

        # Load response body
        body = b""
        resource = txn.get("resource")
        if resource:
            try:
                body = self.loader.load_response_body(txn)
            except Exception as exc:
                err = MissingResponseBody(
                    f"Cannot load response body for {method} {url} (seq={seq})",
                    url=url,
                    method=method,
                    sequence=seq,
                    detail=str(exc),
                )
                self.on_mismatch(err)
                try:
                    route.abort("failed")
                except Exception:
                    pass
                return

        # Remove hop-by-hop headers that Playwright manages itself
        for hop in ("transfer-encoding", "connection", "keep-alive", "upgrade"):
            headers.pop(hop, None)
            headers.pop(hop.title(), None)

        # Ensure content-type is set if recorded
        content_type = txn.get("content_type") or ""
        if content_type and "content-type" not in {k.lower() for k in headers}:
            headers["Content-Type"] = content_type

        try:
            route.fulfill(
                status=status,
                headers=headers,
                body=body if body else b"",
            )
            self._fulfilled.append(seq)
            logger.debug("Fulfilled seq=%d  %s %s  status=%d", seq, method, url, status)
        except Exception as exc:
            logger.warning("route.fulfill failed for seq=%d %s: %s", seq, url, exc)

    # ------------------------------------------------------------------ #
    # Internal: failure matching + abort
    # ------------------------------------------------------------------ #

    def _try_match_failure(self, url: str, method: str, page_id: Optional[str] = None) -> Optional[dict]:
        """Return a failure txn if the next pending transaction for this
        URL+method is a recorded failure, else None."""
        return self.matcher.match_failure(url, method, page_id)

    def _abort_route(self, route, txn: dict, url: str, method: str):
        """Abort a route to reproduce a recorded failure."""
        seq = txn.get("sequence", -1)
        corr = txn.get("correlation_state", "")
        abort_reason = _ABORT_CODE

        # Note: Playwright's abort() reasons are limited.  We use "failed" for
        # all recorded network failures.  This is an acknowledged limitation.
        if corr not in ("CDP_ONLY_FAILED", "PLAYWRIGHT_ONLY_FAILED"):
            logger.warning(
                "Aborting seq=%d with unexpected correlation_state=%r", seq, corr
            )

        try:
            route.abort(abort_reason)
            self._aborted.append(seq)
            logger.debug(
                "Aborted seq=%d  %s %s  corr=%s", seq, method, url, corr
            )
        except Exception as exc:
            logger.warning("route.abort failed for seq=%d %s: %s", seq, url, exc)

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    @property
    def fulfilled_count(self) -> int:
        return len(self._fulfilled)

    @property
    def aborted_count(self) -> int:
        return len(self._aborted)
