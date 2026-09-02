"""
replay/matcher.py — Deterministic network request matcher.

Maps an incoming Playwright route request to a specific recorded
TransactionState dict from the recording.

Matching algorithm
------------------
Primary key (all must match exactly):
    1. Normalised URL  (see _normalise_url)
    2. HTTP method     (uppercased)

Secondary discriminator (used when multiple recorded transactions share the
same primary key — i.e. "concurrent identical requests"):
    3. Request body SHA-256 (when both sides have a body)

Candidate selection
-------------------
When multiple recorded transactions pass steps 1-3, the engine uses the
recorded sequence order as a strict FIFO tiebreaker.  This is safe because
the recorder guarantees that identical requests with identical bodies were
served in sequence order.

If after applying all discriminators there is still ambiguity AND the
candidates have differing recorded responses, the engine raises
RequestMismatch rather than guessing.

Explicitly forbidden
--------------------
- URL-only matching
- URL + method FIFO (without body discrimination when bodies exist)
- Fuzzy / probabilistic matching
- Timing heuristics
- pop(0) FIFO without body checking first
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from replay.errors import (
    MissingRecordedRequest,
    RequestMismatch,
    UnexpectedRequest,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _normalise_url(url: str) -> str:
    """Return a normalised URL for comparison.

    Currently just strips a trailing fragment (#...).  Query parameters and
    paths are preserved exactly so that ?foo=1 and ?foo=2 are distinct.
    """
    if "#" in url:
        url = url.split("#", 1)[0]
    return url


def _body_sha256(body: Optional[bytes]) -> Optional[str]:
    if not body:
        return None
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Match key
# ---------------------------------------------------------------------------

MatchKey = Tuple[str, str, Optional[str]]  # (normalised_url, METHOD, page_id)


def _make_key(url: str, method: str, page_id: Optional[str] = None) -> MatchKey:
    return (_normalise_url(url), method.upper(), page_id)


# ---------------------------------------------------------------------------
# RequestMatcher
# ---------------------------------------------------------------------------

class RequestMatcher:
    """Holds the pool of pending recorded transactions and matches
    incoming replay requests against them.

    Call `load_transactions(iter_transactions())` before replay begins.
    Each recorded transaction may be matched at most once.
    """

    def __init__(self):
        # MatchKey -> list of recorded txn dicts, sorted by sequence ascending
        self._pool: Dict[MatchKey, List[dict]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def load_transactions(self, txn_iter) -> int:
        """Load all recorded transactions into the pool.

        Skips transactions that are recorded failures without a usable
        response (CDP_ONLY_FAILED / PLAYWRIGHT_ONLY_FAILED) — these are
        handled separately by the failure router.

        Returns the total number of transactions loaded.
        """
        count = 0
        failures = 0
        for txn in txn_iter:
            corr = txn.get("correlation_state", "")
            if corr in ("CDP_ONLY_FAILED", "PLAYWRIGHT_ONLY_FAILED"):
                # Failure transactions are loaded into a separate failure pool.
                # They are NOT in the normal response pool.
                failures += 1
                # We still insert them so that the failure router can find them;
                # mark with a sentinel so normal matching skips them.
                txn["_is_failure"] = True

            key = _make_key(txn["url"], txn["method"], txn.get("page_id"))
            self._pool[key].append(txn)
            count += 1

        # Sort each bucket by sequence to guarantee FIFO ordering
        for lst in self._pool.values():
            lst.sort(key=lambda t: t["sequence"])

        logger.debug(
            "RequestMatcher loaded %d transactions (%d failures)", count, failures
        )
        return count

    # ------------------------------------------------------------------ #
    # Matching
    # ------------------------------------------------------------------ #

    def match(
        self,
        url: str,
        method: str,
        body: Optional[bytes] = None,
        page_id: Optional[str] = None,
    ) -> dict:
        """Find and consume the best matching recorded transaction.

        Parameters
        ----------
        url:    The incoming request URL.
        method: The HTTP method (GET, POST, …).
        body:   The raw request body bytes (may be None / empty).
        page_id: The page_id the request originated from.

        Returns
        -------
        The matched recorded transaction dict (removed from the pool).

        Raises
        ------
        UnexpectedRequest
            No recorded transaction matches this URL + method combination at all.
        RequestMismatch
            Candidates exist but body mismatch makes the match ambiguous.
        """
        key = _make_key(url, method, page_id)
        candidates = self._pool.get(key)

        if not candidates:
            # Fallback: were there candidates for THIS url/method but a DIFFERENT page?
            # We don't want to fail with generic UnexpectedRequest if the page context was wrong.
            # But UnexpectedRequest takes page_id.
            raise UnexpectedRequest(url=url, method=method, page_id=page_id)

        # Filter out failure-only transactions for normal response matching
        normal = [c for c in candidates if not c.get("_is_failure")]
        failures_only = [c for c in candidates if c.get("_is_failure")]

        if not normal:
            # The only candidates are recorded failures — route to failure handler
            raise UnexpectedRequest(
                url=url,
                method=method,
                page_id=page_id,
                detail="All matching recorded transactions are failures; use failure router",
            )

        incoming_sha = _body_sha256(body)
        recorded_sha = _recorded_body_sha256(normal[0])

        if len(normal) == 1:
            # Single candidate — validate body if both sides have one
            candidate = normal[0]
            if incoming_sha and recorded_sha and incoming_sha != recorded_sha:
                raise RequestMismatch(
                    f"Request body mismatch for {method} {url}",
                    url=url,
                    method=method,
                    page_id=page_id,
                    expected_body_sha256=recorded_sha,
                    actual_body_sha256=incoming_sha,
                    sequence=candidate.get("sequence"),
                )
            return self._consume(key, candidate)

        # Multiple candidates — use body SHA to narrow down
        if incoming_sha:
            body_matched = [c for c in normal if _recorded_body_sha256(c) == incoming_sha]
            if body_matched:
                return self._consume(key, body_matched[0])
            # Body doesn't match any candidate
            raise RequestMismatch(
                f"Body mismatch: {method} {url} — body does not match any recorded candidate",
                url=url,
                method=method,
                page_id=page_id,
                actual_body_sha256=incoming_sha,
                candidates=[c.get("sequence") for c in normal],
            )

        # No body on incoming request; check if candidates are interchangeable
        # (same response body SHA and status)
        if _candidates_interchangeable(normal):
            # Safely use FIFO — responses are identical
            return self._consume(key, normal[0])

        # Multiple non-interchangeable candidates with no body discriminator
        raise RequestMismatch(
            f"Ambiguous match: {method} {url} has {len(normal)} candidates with "
            "differing responses and no body to discriminate",
            url=url,
            method=method,
            page_id=page_id,
            candidates=[c.get("sequence") for c in normal],
        )

    def match_failure(self, url: str, method: str, page_id: Optional[str] = None) -> Optional[dict]:
        """Find and consume the next failure transaction for this URL+method.

        Returns None if no failure transaction is available (caller should
        treat the request as an unexpected request).
        """
        key = _make_key(url, method, page_id)
        candidates = self._pool.get(key, [])
        failures = [c for c in candidates if c.get("_is_failure")]
        if not failures:
            return None
        return self._consume(key, failures[0])

    def pending_count(self) -> int:
        """Return the number of unmatched (pending) recorded transactions."""
        return sum(len(v) for v in self._pool.values())

    def all_pending(self) -> List[dict]:
        """Return all unmatched transactions (for end-of-session validation)."""
        result = []
        for lst in self._pool.values():
            result.extend(lst)
        result.sort(key=lambda t: t["sequence"])
        return result

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _consume(self, key: MatchKey, txn: dict) -> dict:
        """Remove `txn` from the pool and return it."""
        lst = self._pool[key]
        lst.remove(txn)
        if not lst:
            del self._pool[key]
        return txn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recorded_body_sha256(txn: dict) -> Optional[str]:
    """Return the SHA-256 of the recorded request body, if any."""
    # Binary bodies have their SHA-256 stored in resource (for responses)
    # but request bodies use post_data_file — we don't store SHA for those.
    # We can only compare when the inline post_data field is present.
    post_data = txn.get("post_data")
    if post_data:
        return hashlib.sha256(post_data.encode("utf-8", errors="replace")).hexdigest()
    # Binary request body: SHA is NOT recorded in the manifest — we read the
    # file during matching if needed.  Signal "unknown" as None.
    return None


def _candidates_interchangeable(candidates: List[dict]) -> bool:
    """Return True if all candidates have identical responses (safe FIFO)."""
    if len(candidates) <= 1:
        return True
    first = candidates[0]
    ref_status = first.get("status")
    ref_sha = (first.get("resource") or {}).get("sha256")
    ref_error = first.get("error")
    for c in candidates[1:]:
        if c.get("status") != ref_status:
            return False
        if (c.get("resource") or {}).get("sha256") != ref_sha:
            return False
        if c.get("error") != ref_error:
            return False
    return True
