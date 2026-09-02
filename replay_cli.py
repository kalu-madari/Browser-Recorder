"""
replay_cli.py — Minimal CLI entry point for the replay engine.

Usage:
    python replay_cli.py <capture_dir> [--headed] [--non-strict]

Arguments:
    capture_dir   Path to a recording session directory
                  (e.g. captures/2026-09-02_12-13-32)

Options:
    --headed      Run with a visible browser window (default: headless)
    --non-strict  Allow unexpected requests through (NOT recommended;
                  use only for debugging)

Exit codes:
    0  Replay completed with no fatal mismatches.
    1  One or more fatal mismatches occurred.
    2  Command-line argument error.
"""

from __future__ import annotations

import argparse
import logging
import sys
import os

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from replay.session import ReplaySession


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay_cli",
        description="Replay a Browser Recorder session in Strict Mock Mode.",
    )
    parser.add_argument(
        "capture_dir",
        help="Path to the recording session directory",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Run with a visible browser (default: headless)",
    )
    parser.add_argument(
        "--non-strict",
        action="store_true",
        default=False,
        help="Allow unexpected requests (debug only — weakens isolation guarantee)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    if not os.path.isdir(args.capture_dir):
        print(f"ERROR: capture directory not found: {args.capture_dir}", file=sys.stderr)
        return 2

    session = ReplaySession(
        capture_dir=args.capture_dir,
        headless=not args.headed,
        strict=not args.non_strict,
    )

    result = session.run()
    print(result)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
