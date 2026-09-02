"""
replay/errors.py — Structured replay mismatch and error types.

Every replay failure is represented as a typed exception carrying enough
context for diagnosis.  Fatal mismatches are subclasses of ReplayFatal;
non-fatal issues are subclasses of ReplayWarning.

The engine MUST NOT swallow a ReplayFatal silently.
"""

from __future__ import annotations
from typing import Optional


# ---------------------------------------------------------------------------
# Severity marker base classes
# ---------------------------------------------------------------------------

class ReplayFatal(Exception):
    """Fatal replay failure. The replay session cannot continue reliably."""

    severity: str = "FATAL"

    def __init__(self, message: str, **ctx):
        super().__init__(message)
        self.message = message
        self.ctx = ctx  # structured context for logging / tests

    def __str__(self) -> str:
        parts = [self.message]
        for k, v in self.ctx.items():
            if v is not None:
                parts.append(f"  {k}={v!r}")
        return "\n".join(parts)


class ReplayWarning(Exception):
    """Non-fatal replay divergence. Logged but execution may continue."""

    severity: str = "WARNING"

    def __init__(self, message: str, **ctx):
        super().__init__(message)
        self.message = message
        self.ctx = ctx

    def __str__(self) -> str:
        parts = [self.message]
        for k, v in self.ctx.items():
            if v is not None:
                parts.append(f"  {k}={v!r}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Recording load errors (always fatal)
# ---------------------------------------------------------------------------

class RecordingLoadError(ReplayFatal):
    """Recording cannot be loaded or parsed."""


class RecordingCorruption(ReplayFatal):
    """A recording file is structurally corrupt (bad JSON, SHA256 mismatch, etc.)."""


class MissingBinaryBody(ReplayFatal):
    """A required binary body file (resource or request body) is absent."""


class CorruptBinaryBody(ReplayFatal):
    """A binary body file exists but its SHA256 does not match the manifest."""


class InvalidSequence(ReplayFatal):
    """The sequence field in a record is missing, negative, or non-monotonic."""


# ---------------------------------------------------------------------------
# Page / session errors
# ---------------------------------------------------------------------------

class MissingPage(ReplayFatal):
    """A recorded page_id has no live Playwright Page mapping."""


class PageLifecycleMismatch(ReplayFatal):
    """A page was expected to be open/closed but found in the wrong state."""


class PageIdentityAmbiguous(ReplayFatal):
    """A page_id cannot be deterministically mapped to a live page."""


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------

class UnexpectedRequest(ReplayFatal):
    """The replay browser made a request for which no recorded transaction exists.

    In Strict Mock Mode this is always fatal — it means the application has
    diverged from the recording.
    """

    def __init__(self, url: str, method: str, page_id: Optional[str] = None, **ctx):
        super().__init__(
            f"Unexpected {method} {url} — no matching recorded transaction",
            url=url,
            method=method,
            page_id=page_id,
            **ctx,
        )


class MissingRecordedRequest(ReplayFatal):
    """A recorded transaction was never matched by a real request during replay."""


class RequestMismatch(ReplayFatal):
    """An incoming request matched a recorded transaction by identity but a
    required field (e.g. request body) does not match."""


class ResponseMismatch(ReplayFatal):
    """The response that would be served differs in a required field."""


class MissingResponseBody(ReplayFatal):
    """A recorded response had a body reference but the body file is absent."""


# ---------------------------------------------------------------------------
# Sequence coordination errors
# ---------------------------------------------------------------------------

class ReplaySequenceMismatch(ReplayFatal):
    """Events occurred in a sequence that is impossible given the recording."""


# ---------------------------------------------------------------------------
# Feature support errors
# ---------------------------------------------------------------------------

class UnsupportedReplayFeature(ReplayWarning):
    """A recorded feature (e.g. closed shadow DOM) cannot be faithfully replayed.

    Classified as WARNING because the recording itself is valid; it is the
    replay engine that lacks support.  Individual cases may be escalated to
    FATAL by callers if the feature is required for correctness.
    """
