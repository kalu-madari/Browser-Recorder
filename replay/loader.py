"""
replay/loader.py — Streaming recording reader.

Reads JSONL manifests incrementally without loading the entire recording into
RAM.  Binary bodies are resolved lazily by path when actually needed.

Layout of a recording session directory (all paths relative to capture_dir):
    manifest.jsonl        — network transactions (TransactionState schema)
    navigation.jsonl      — NavigationRecord rows
    interaction.jsonl     — InteractionRecord rows
    mutations.jsonl       — MutationRecord rows (may be absent)
    downloads.jsonl       — DownloadRecord rows (may be absent)
    websocket.jsonl       — WebSocketState / WebSocketFrame rows (may be absent)
    dom.jsonl             — DOMSnapshotRecord rows
    session.json          — session metadata
    resources/            — response body files
    request_bodies/       — request body files
    dom/                  — DOM snapshot HTML files

Design constraints:
    - Stream JSONL line-by-line; never load a full manifest at once.
    - Tolerate a truncated / malformed FINAL line (documented crash behaviour).
    - Detect and raise errors for missing / corrupt binary bodies eagerly on
      first access, not at load time.
    - Preserve recorded sequence ordering (records are yielded in file order,
      which is insertion order, which equals increasing sequence for manifests).
    - Do NOT silently discard malformed data.  Raise RecordingLoadError or
      RecordingCorruption with enough context to diagnose the problem.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Iterator, Optional

from replay.errors import (
    CorruptBinaryBody,
    MissingBinaryBody,
    RecordingCorruption,
    RecordingLoadError,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _stream_jsonl(path: str, *, label: str) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file one line at a time.

    The very last line is allowed to be truncated/malformed — this mirrors the
    documented crash-recovery behaviour of the recorder (a process crash can
    leave an incomplete final JSONL record).  All other malformed lines raise
    RecordingCorruption.
    """
    if not os.path.exists(path):
        raise RecordingLoadError(
            f"Required recording file not found: {path}", label=label, path=path
        )

    prev_line: Optional[str] = None
    prev_lineno: int = 0

    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            # Flush the previously buffered line (now definitely not the last)
            if prev_line is not None:
                yield _parse_line(prev_line, path=path, lineno=prev_lineno, label=label, last=False)

            prev_line = raw.rstrip("\r\n")
            prev_lineno = lineno

    # Handle the last line with leniency
    if prev_line:
        stripped = prev_line.strip()
        if stripped:
            try:
                obj = json.loads(stripped)
                if not isinstance(obj, dict):
                    raise RecordingCorruption(
                        f"Last line is not a JSON object in {label}",
                        path=path,
                        lineno=prev_lineno,
                    )
                yield obj
            except json.JSONDecodeError:
                # Tolerate truncated last record — warn via return, caller decides
                import logging
                logging.getLogger(__name__).warning(
                    "Truncated/malformed final JSONL record in %s (line %d) — skipped",
                    path,
                    prev_lineno,
                )


def _parse_line(line: str, *, path: str, lineno: int, label: str, last: bool) -> dict:
    stripped = line.strip()
    if not stripped:
        # Blank lines are silently skipped
        return None  # caller must filter None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RecordingCorruption(
            f"Malformed JSON on line {lineno} of {label}",
            path=path,
            lineno=lineno,
            detail=str(exc),
        )
    if not isinstance(obj, dict):
        raise RecordingCorruption(
            f"Non-object JSON on line {lineno} of {label}",
            path=path,
            lineno=lineno,
        )
    return obj


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class RecordingLoader:
    """Streaming reader for a single recorded session directory.

    Usage::

        loader = RecordingLoader("/path/to/captures/2026-09-02_12-13-32")
        for txn in loader.iter_transactions():
            ...  # dict matching TransactionState schema

    Binary body resolution::

        body_bytes = loader.load_body(txn["resource"]["file_path"])
        req_bytes  = loader.load_body(txn["post_data_file"])

    Both methods verify the SHA-256 hash when one is recorded.
    """

    def __init__(self, capture_dir: str):
        if not os.path.isdir(capture_dir):
            raise RecordingLoadError(
                f"Capture directory does not exist: {capture_dir}",
                capture_dir=capture_dir,
            )
        self.capture_dir = os.path.abspath(capture_dir)
        self._session: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # Session metadata
    # ------------------------------------------------------------------ #

    def session_info(self) -> dict:
        """Return the session.json metadata dict (cached)."""
        if self._session is None:
            path = os.path.join(self.capture_dir, "session.json")
            if not os.path.exists(path):
                raise RecordingLoadError(
                    "session.json missing from recording",
                    capture_dir=self.capture_dir,
                )
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._session = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                raise RecordingCorruption(
                    "session.json is unreadable", capture_dir=self.capture_dir, detail=str(exc)
                )
        return self._session

    # ------------------------------------------------------------------ #
    # JSONL stream helpers
    # ------------------------------------------------------------------ #

    def _manifest_path(self, name: str) -> str:
        return os.path.join(self.capture_dir, name)

    def _iter_jsonl(self, filename: str) -> Iterator[dict]:
        path = self._manifest_path(filename)
        if not os.path.exists(path):
            return  # optional manifests (mutations, downloads, websocket) may be absent
        for record in _stream_jsonl(path, label=filename):
            if record is not None:
                yield record

    # ------------------------------------------------------------------ #
    # Public record iterators — all return dicts matching the model schema
    # ------------------------------------------------------------------ #

    def iter_transactions(self) -> Iterator[dict]:
        """Stream network transactions from manifest.jsonl.

        Records are yielded in file order (= recorded sequence order).
        The sequence field is validated to be a positive integer.
        """
        for record in _stream_jsonl(
            self._manifest_path("manifest.jsonl"), label="manifest.jsonl"
        ):
            if record is None:
                continue
            self._validate_sequence(record, "manifest.jsonl")
            yield record

    def iter_navigations(self) -> Iterator[dict]:
        """Stream NavigationRecord dicts from navigation.jsonl."""
        yield from self._iter_jsonl("navigation.jsonl")

    def iter_interactions(self) -> Iterator[dict]:
        """Stream InteractionRecord dicts from interaction.jsonl."""
        for record in self._iter_jsonl("interaction.jsonl"):
            if record is None:
                continue
            self._validate_sequence(record, "interaction.jsonl")
            yield record

    def iter_mutations(self) -> Iterator[dict]:
        """Stream MutationRecord dicts from mutations.jsonl (may not exist)."""
        for record in self._iter_jsonl("mutations.jsonl"):
            if record is None:
                continue
            self._validate_sequence(record, "mutations.jsonl")
            yield record

    def iter_downloads(self) -> Iterator[dict]:
        """Stream DownloadRecord dicts from downloads.jsonl (may not exist)."""
        yield from self._iter_jsonl("downloads.jsonl")

    def iter_dom_snapshots(self) -> Iterator[dict]:
        """Stream DOMSnapshotRecord dicts from dom.jsonl."""
        yield from self._iter_jsonl("dom.jsonl")

    def iter_websocket_events(self) -> Iterator[dict]:
        """Stream WebSocket connection/frame events from websocket.jsonl."""
        yield from self._iter_jsonl("websocket.jsonl")

    # ------------------------------------------------------------------ #
    # Binary body resolution (lazy, with integrity check)
    # ------------------------------------------------------------------ #

    def resolve_path(self, relative_path: str) -> str:
        """Return the absolute path for a recording-relative file reference."""
        return os.path.join(self.capture_dir, relative_path)

    def body_exists(self, relative_path: Optional[str]) -> bool:
        """Return True if the body file is present on disk."""
        if not relative_path:
            return False
        return os.path.isfile(self.resolve_path(relative_path))

    def load_body(
        self,
        relative_path: str,
        *,
        expected_sha256: Optional[str] = None,
        expected_size: Optional[int] = None,
    ) -> bytes:
        """Load a binary body file and optionally verify its SHA-256.

        Raises MissingBinaryBody if the file does not exist.
        Raises CorruptBinaryBody if the SHA-256 or size does not match.
        """
        if not relative_path:
            raise MissingBinaryBody(
                "Empty body path — no binary body was recorded",
                path=relative_path,
            )

        abs_path = self.resolve_path(relative_path)
        if not os.path.isfile(abs_path):
            raise MissingBinaryBody(
                f"Binary body file not found: {relative_path}",
                path=abs_path,
                relative_path=relative_path,
            )

        try:
            with open(abs_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise MissingBinaryBody(
                f"Cannot read binary body file: {exc}",
                path=abs_path,
            )

        if expected_sha256:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected_sha256:
                raise CorruptBinaryBody(
                    f"SHA-256 mismatch for {relative_path}",
                    path=abs_path,
                    expected=expected_sha256,
                    actual=actual,
                )

        if expected_size is not None and len(data) != expected_size:
            raise CorruptBinaryBody(
                f"Size mismatch for {relative_path}",
                path=abs_path,
                expected_size=expected_size,
                actual_size=len(data),
            )

        return data

    def load_response_body(self, txn: dict) -> Optional[bytes]:
        """Load the response body for a transaction, verifying integrity.

        Returns None if the transaction has no recorded body (resource is null).
        Raises MissingBinaryBody / CorruptBinaryBody on integrity failures.
        """
        resource = txn.get("resource")
        if not resource:
            return None
        return self.load_body(
            resource["file_path"],
            expected_sha256=resource.get("sha256"),
            expected_size=resource.get("size"),
        )

    def load_request_body(self, txn: dict) -> Optional[bytes]:
        """Load the request body for a transaction (returns None if none recorded)."""
        post_data_file = txn.get("post_data_file")
        if post_data_file:
            return self.load_body(post_data_file)
        return None

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_sequence(record: dict, label: str):
        seq = record.get("sequence")
        if seq is None:
            raise RecordingLoadError(
                f"Record missing 'sequence' field in {label}",
                label=label,
                record_id=record.get("id", record.get("interaction_id", record.get("mutation_id", "?"))),
            )
        if not isinstance(seq, int) or seq < 1:
            raise RecordingLoadError(
                f"Invalid sequence value {seq!r} in {label}",
                label=label,
                sequence=seq,
            )
