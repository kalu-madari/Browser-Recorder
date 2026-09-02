"""
replay/coordinator.py — Deterministic sequence coordinator.

Uses the recorded `sequence` field as the primary causality ordering
mechanism.  Does NOT depend on wall-clock time.

Phase 1 responsibilities
------------------------
- Track the current replay sequence position.
- Provide a simple "has sequence N been reached yet" query.
- Expose hooks for later phases to register pre-conditions
  ("do not execute action X until sequence N is fulfilled").

The coordinator does NOT implement any threading or asyncio primitives in
Phase 1 — replay proceeds sequentially within a single thread by design.
Sequence-based gating for interaction replay is scaffolded but not wired.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from replay.errors import ReplaySequenceMismatch

logger = logging.getLogger(__name__)


class SequenceCoordinator:
    """Tracks sequence state for the replay session.

    The replayed network interactions are processed in ascending sequence
    order.  Each time the network interceptor serves or aborts a recorded
    transaction it should call `notify(sequence)`.

    Later phases (interaction replay) can call `wait_for(sequence)` to
    block until that sequence has been completed.
    """

    def __init__(self):
        self._completed: set = set()
        self._max_completed: int = 0
        self._expected_sequences: List[int] = []   # sorted list from recording

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def load_sequences(self, sequences: List[int]) -> None:
        """Register all expected sequence numbers from the recording.

        Must be called before replay starts.
        """
        self._expected_sequences = sorted(sequences)
        logger.debug(
            "SequenceCoordinator: %d sequences registered (max=%d)",
            len(sequences),
            max(sequences) if sequences else 0,
        )

    # ------------------------------------------------------------------ #
    # Notification
    # ------------------------------------------------------------------ #

    def notify(self, sequence: int) -> None:
        """Mark a sequence as completed.

        Called by the network interceptor after each fulfilled/aborted txn.
        """
        if sequence in self._completed:
            raise ReplaySequenceMismatch(
                f"Sequence {sequence} completed more than once",
                sequence=sequence,
            )
        self._completed.add(sequence)
        if sequence > self._max_completed:
            self._max_completed = sequence

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def is_completed(self, sequence: int) -> bool:
        """Return True if `sequence` has been notified as completed."""
        return sequence in self._completed

    def completed_count(self) -> int:
        return len(self._completed)

    def pending_sequences(self) -> List[int]:
        """Return expected sequences not yet completed, sorted ascending."""
        return sorted(s for s in self._expected_sequences if s not in self._completed)

    # ------------------------------------------------------------------ #
    # Phase 2+ scaffolding: pre-condition hooks
    # ------------------------------------------------------------------ #

    def wait_for(self, sequence: int, timeout_s: float = 10.0) -> None:
        """Block until `sequence` is notified as completed.

        In Phase 1 replay is single-threaded and sequential, so this simply
        raises ReplaySequenceMismatch if the sequence is not already completed.

        In Phase 2 (interaction replay) this will use a threading.Event.
        """
        if not self.is_completed(sequence):
            raise ReplaySequenceMismatch(
                f"wait_for({sequence}) called but sequence not yet completed",
                sequence=sequence,
                max_completed=self._max_completed,
            )
