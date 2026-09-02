"""
replay/ — Browser Recorder Replay Engine

This package provides deterministic replay of browser sessions captured by the
recording subsystem.

Phase 1 covers: recording loader, session management, page lifecycle,
network mocking, sequence coordination, and the mismatch/error system.

DOM interaction replay, Shadow DOM replay, download replay, and full
WebSocket replay are NOT in scope for Phase 1.
"""
