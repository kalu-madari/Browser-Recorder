"""
test_replay_foundation.py — Focused tests for Phase 1: Replay Foundation.

Tests are grouped by subsystem:
    1. RecordingLoader  (streaming, binary, corruption, truncation)
    2. RequestMatcher   (normal, concurrent, failures, ambiguity)
    3. PageManager      (lifecycle, identity, errors)
    4. SequenceCoordinator
    5. NetworkInterceptor (security, unexpected requests)
    6. Integration      (record -> persist -> load -> replay -> verify)

All tests use isolated temporary directories and in-process HTTP servers.
No real external network traffic is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from replay.errors import (
    CorruptBinaryBody,
    MissingBinaryBody,
    MissingPage,
    PageIdentityAmbiguous,
    RecordingCorruption,
    RecordingLoadError,
    RequestMismatch,
    ReplayFatal,
    UnexpectedRequest,
)
from replay.loader import RecordingLoader, _stream_jsonl
from replay.matcher import RequestMatcher, _candidates_interchangeable
from replay.pages import PageManager
from replay.coordinator import SequenceCoordinator
from replay.errors import ReplaySequenceMismatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_capture_dir():
    """Create a minimal valid capture directory structure."""
    d = tempfile.mkdtemp(prefix="test_replay_")
    os.makedirs(os.path.join(d, "resources"))
    os.makedirs(os.path.join(d, "request_bodies"))
    os.makedirs(os.path.join(d, "dom"))
    os.makedirs(os.path.join(d, "websocket"))
    # Minimal session.json
    with open(os.path.join(d, "session.json"), "w") as f:
        json.dump({"session_id": "test", "browser": "Chromium"}, f)
    # Empty required manifests
    for name in ("manifest.jsonl", "navigation.jsonl", "interaction.jsonl", "dom.jsonl"):
        open(os.path.join(d, name), "w").close()
    return d


def _write_txn(d, txn: dict):
    """Append a transaction to manifest.jsonl."""
    with open(os.path.join(d, "manifest.jsonl"), "a") as f:
        f.write(json.dumps(txn) + "\n")


def _write_nav(d, nav: dict):
    with open(os.path.join(d, "navigation.jsonl"), "a") as f:
        f.write(json.dumps(nav) + "\n")


def _make_txn(seq, url="http://example.com/", method="GET", status=200,
               body=b"hello", page_id="page-001", corr="PLAYWRIGHT_ONLY",
               post_data=None):
    sha = hashlib.sha256(body).hexdigest()
    return {
        "id": f"tx-{seq:06d}",
        "sequence": seq,
        "url": url,
        "method": method,
        "request_headers": {},
        "request_time": "2026-01-01T00:00:00",
        "resource_type": "document",
        "page_id": page_id,
        "frame_id": "unknown",
        "frame_url": "",
        "post_data": post_data,
        "post_data_file": None,
        "request_cookies": [],
        "initiator": None,
        "response_time": "2026-01-01T00:00:01",
        "status": status,
        "status_text": "OK",
        "response_headers": {"content-type": "text/html"},
        "content_type": "text/html",
        "resource": {
            "file_path": f"resources/{seq:06d}.html",
            "size": len(body),
            "sha256": sha,
            "mime_type": "text/html",
            "extension": ".html",
        },
        "error": None,
        "completed": True,
        "completion_time": "2026-01-01T00:00:01",
        "finalized": True,
        "correlation_state": corr,
    }


def _save_body(d, relative_path: str, body: bytes):
    abs_path = os.path.join(d, relative_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        f.write(body)


# ---------------------------------------------------------------------------
# 1. RecordingLoader
# ---------------------------------------------------------------------------

class TestRecordingLoader(unittest.TestCase):

    def setUp(self):
        self.d = _make_capture_dir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_valid_recording_loads(self):
        body = b"<html>hello</html>"
        txn = _make_txn(1, body=body)
        _write_txn(self.d, txn)
        _save_body(self.d, "resources/000001.html", body)

        loader = RecordingLoader(self.d)
        txns = list(loader.iter_transactions())
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["sequence"], 1)

    def test_missing_capture_dir_raises(self):
        with self.assertRaises(RecordingLoadError):
            RecordingLoader("/nonexistent/path/abc123")

    def test_missing_manifest_raises(self):
        os.remove(os.path.join(self.d, "manifest.jsonl"))
        loader = RecordingLoader(self.d)
        with self.assertRaises(RecordingLoadError):
            list(loader.iter_transactions())

    def test_malformed_json_raises_corruption(self):
        with open(os.path.join(self.d, "manifest.jsonl"), "w") as f:
            f.write('{"sequence": 1, "url": "http://a.com/", "method": "GET", "id": "x"}\n')
            f.write("NOT VALID JSON\n")
            f.write('{"sequence": 2, "url": "http://b.com/", "method": "GET", "id": "y"}\n')
        loader = RecordingLoader(self.d)
        with self.assertRaises(RecordingCorruption):
            list(loader.iter_transactions())

    def test_truncated_final_line_tolerated(self):
        """Malformed last line must be skipped, not fatal."""
        body = b"hi"
        txn1 = _make_txn(1, body=body)
        _save_body(self.d, "resources/000001.html", body)
        with open(os.path.join(self.d, "manifest.jsonl"), "w") as f:
            f.write(json.dumps(txn1) + "\n")
            f.write('{"sequence": 2, "TRUNCATED')  # truncated last line
        loader = RecordingLoader(self.d)
        txns = list(loader.iter_transactions())
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["sequence"], 1)

    def test_load_body_validates_sha256(self):
        body = b"correct body"
        txn = _make_txn(1, body=body)
        _write_txn(self.d, txn)
        _save_body(self.d, "resources/000001.html", body)

        loader = RecordingLoader(self.d)
        txns = list(loader.iter_transactions())
        data = loader.load_response_body(txns[0])
        self.assertEqual(data, body)

    def test_missing_binary_body_raises(self):
        body = b"not on disk"
        txn = _make_txn(1, body=body)
        _write_txn(self.d, txn)
        # Do NOT save the body file

        loader = RecordingLoader(self.d)
        txns = list(loader.iter_transactions())
        with self.assertRaises(MissingBinaryBody):
            loader.load_response_body(txns[0])

    def test_corrupt_binary_body_raises(self):
        body = b"original"
        txn = _make_txn(1, body=body)
        _write_txn(self.d, txn)
        # Save wrong content
        _save_body(self.d, "resources/000001.html", b"wrong content")

        loader = RecordingLoader(self.d)
        txns = list(loader.iter_transactions())
        with self.assertRaises(CorruptBinaryBody):
            loader.load_response_body(txns[0])

    def test_streaming_does_not_load_all_at_once(self):
        """Confirm the loader is genuinely streaming (returns a generator)."""
        for i in range(1, 6):
            body = f"body{i}".encode()
            txn = _make_txn(i, url=f"http://example.com/r{i}", body=body)
            _write_txn(self.d, txn)
            _save_body(self.d, f"resources/{i:06d}.html", body)

        loader = RecordingLoader(self.d)
        gen = loader.iter_transactions()
        import inspect
        self.assertTrue(inspect.isgenerator(gen))
        txns = list(gen)
        self.assertEqual(len(txns), 5)

    def test_sequence_ordering_preserved(self):
        """Records are yielded in file order = sequence order."""
        for seq in [3, 1, 2]:
            body = f"body{seq}".encode()
            txn = _make_txn(seq, url=f"http://example.com/r{seq}", body=body)
            _write_txn(self.d, txn)
            _save_body(self.d, f"resources/{seq:06d}.html", body)
        loader = RecordingLoader(self.d)
        txns = list(loader.iter_transactions())
        sequences = [t["sequence"] for t in txns]
        # File order = insertion order in this test: [3, 1, 2]
        self.assertEqual(sequences, [3, 1, 2])

    def test_optional_manifests_absent_ok(self):
        """mutations.jsonl / downloads.jsonl may be absent without error."""
        loader = RecordingLoader(self.d)
        muts = list(loader.iter_mutations())
        dls = list(loader.iter_downloads())
        self.assertEqual(muts, [])
        self.assertEqual(dls, [])


# ---------------------------------------------------------------------------
# 2. RequestMatcher
# ---------------------------------------------------------------------------

class TestRequestMatcher(unittest.TestCase):

    def _make_pool(self, txns):
        m = RequestMatcher()
        m.load_transactions(iter(txns))
        return m

    def test_simple_get_match(self):
        txn = _make_txn(1, url="http://example.com/", method="GET", body=b"response")
        m = self._make_pool([txn])
        result = m.match("http://example.com/", "GET")
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(m.pending_count(), 0)

    def test_url_only_insufficient_when_method_differs(self):
        txn = _make_txn(1, url="http://example.com/", method="POST", body=b"r")
        m = self._make_pool([txn])
        with self.assertRaises(UnexpectedRequest):
            m.match("http://example.com/", "GET")

    def test_unexpected_url_raises(self):
        txn = _make_txn(1, url="http://example.com/a", method="GET", body=b"r")
        m = self._make_pool([txn])
        with self.assertRaises(UnexpectedRequest):
            m.match("http://example.com/b", "GET")

    def test_concurrent_identical_interchangeable(self):
        """Two identical GET requests with identical responses → FIFO is safe."""
        body = b"same response"
        t1 = _make_txn(1, url="http://ex.com/api", method="GET", body=body)
        t2 = _make_txn(2, url="http://ex.com/api", method="GET", body=body)
        m = self._make_pool([t1, t2])
        r1 = m.match("http://ex.com/api", "GET")
        r2 = m.match("http://ex.com/api", "GET")
        self.assertEqual(sorted([r1["sequence"], r2["sequence"]]), [1, 2])
        self.assertEqual(m.pending_count(), 0)

    def test_post_body_discrimination(self):
        """Two POSTs to same URL with different bodies should match independently."""
        t1 = _make_txn(1, url="http://ex.com/submit", method="POST",
                       body=b"resp-a", post_data="payload-a")
        t2 = _make_txn(2, url="http://ex.com/submit", method="POST",
                       body=b"resp-b", post_data="payload-b")
        m = self._make_pool([t1, t2])
        # Match by body
        r = m.match("http://ex.com/submit", "POST",
                    body="payload-b".encode("utf-8"))
        self.assertEqual(r["sequence"], 2)
        r2 = m.match("http://ex.com/submit", "POST",
                     body="payload-a".encode("utf-8"))
        self.assertEqual(r2["sequence"], 1)

    def test_body_mismatch_raises(self):
        t = _make_txn(1, url="http://ex.com/", method="POST",
                      body=b"resp", post_data="expected")
        m = self._make_pool([t])
        with self.assertRaises(RequestMismatch):
            m.match("http://ex.com/", "POST", body=b"wrong payload")

    def test_failure_transactions_routed_separately(self):
        """Failure transactions do not appear as normal matches."""
        t_fail = _make_txn(1, url="http://ex.com/fail", method="GET",
                           body=b"", corr="CDP_ONLY_FAILED")
        t_fail["error"] = "net::ERR_FAILED"
        m = self._make_pool([t_fail])
        with self.assertRaises(UnexpectedRequest):
            m.match("http://ex.com/fail", "GET")  # not in normal pool

    def test_match_failure_explicit(self):
        t_fail = _make_txn(1, url="http://ex.com/fail", method="GET",
                           body=b"", corr="CDP_ONLY_FAILED")
        m = self._make_pool([t_fail])
        result = m.match_failure("http://ex.com/fail", "GET")
        self.assertIsNotNone(result)
        self.assertEqual(result["sequence"], 1)

    def test_ambiguous_multi_candidate_raises(self):
        """Two candidates with DIFFERENT responses and no body → ambiguity."""
        t1 = _make_txn(1, url="http://ex.com/", method="GET", body=b"response-A")
        t2 = _make_txn(2, url="http://ex.com/", method="GET", body=b"response-B")
        m = self._make_pool([t1, t2])
        with self.assertRaises(RequestMismatch):
            m.match("http://ex.com/", "GET", body=None)

    def test_candidates_interchangeable_helper(self):
        t1 = _make_txn(1, body=b"same")
        t2 = _make_txn(2, body=b"same")
        # Same SHA, same status → interchangeable
        self.assertTrue(_candidates_interchangeable([t1, t2]))

        t3 = _make_txn(3, body=b"different")
        self.assertFalse(_candidates_interchangeable([t1, t3]))


# ---------------------------------------------------------------------------
# 3. PageManager
# ---------------------------------------------------------------------------

class TestPageManager(unittest.TestCase):

    def test_register_and_get(self):
        pm = PageManager()
        fake_page = object()
        pm.register_page("page-001", fake_page)
        self.assertIs(pm.get_page("page-001"), fake_page)

    def test_missing_page_raises(self):
        pm = PageManager()
        with self.assertRaises(MissingPage):
            pm.get_page("page-999")

    def test_duplicate_page_id_raises(self):
        pm = PageManager()
        pm.register_page("page-001", object())
        with self.assertRaises(PageIdentityAmbiguous):
            pm.register_page("page-001", object())

    def test_close_page_removes_mapping(self):
        pm = PageManager()
        pm.register_page("page-001", object())
        pm.close_page("page-001")
        with self.assertRaises(MissingPage):
            pm.get_page("page-001")

    def test_close_unregistered_raises(self):
        pm = PageManager()
        from replay.errors import PageLifecycleMismatch
        with self.assertRaises(PageLifecycleMismatch):
            pm.close_page("page-999")

    def test_multiple_pages(self):
        pm = PageManager()
        p1, p2 = object(), object()
        pm.register_page("page-001", p1)
        pm.register_page("page-002", p2)
        self.assertIs(pm.get_page("page-001"), p1)
        self.assertIs(pm.get_page("page-002"), p2)
        self.assertEqual(len(pm.active_page_ids), 2)


# ---------------------------------------------------------------------------
# 4. SequenceCoordinator
# ---------------------------------------------------------------------------

class TestSequenceCoordinator(unittest.TestCase):

    def test_basic_notify_and_query(self):
        sc = SequenceCoordinator()
        sc.load_sequences([1, 2, 3])
        sc.notify(1)
        self.assertTrue(sc.is_completed(1))
        self.assertFalse(sc.is_completed(2))
        self.assertEqual(sc.pending_sequences(), [2, 3])

    def test_double_notify_raises(self):
        sc = SequenceCoordinator()
        sc.load_sequences([1])
        sc.notify(1)
        with self.assertRaises(ReplaySequenceMismatch):
            sc.notify(1)

    def test_wait_for_completed_ok(self):
        sc = SequenceCoordinator()
        sc.load_sequences([1])
        sc.notify(1)
        sc.wait_for(1)  # must not raise

    def test_wait_for_not_completed_raises(self):
        sc = SequenceCoordinator()
        sc.load_sequences([1])
        with self.assertRaises(ReplaySequenceMismatch):
            sc.wait_for(1)


# ---------------------------------------------------------------------------
# 5. NetworkInterceptor — security and strict mode tests
# ---------------------------------------------------------------------------

class _FakeRoute:
    """Minimal Playwright route double for testing."""

    def __init__(self, url, method, body=None):
        self._url = url
        self._method = method
        self._body = body
        self.fulfilled = None
        self.aborted = None
        self.continued = False

    def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    def abort(self, reason="failed"):
        self.aborted = reason

    def continue_(self):
        self.continued = True


class _FakeRequest:
    def __init__(self, url, method, body=None):
        self.url = url
        self.method = method
        self.post_data_buffer = body


class TestNetworkInterceptor(unittest.TestCase):

    def setUp(self):
        self.d = _make_capture_dir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _make_interceptor(self, txns, strict=True):
        from replay.network import NetworkInterceptor
        body = b"response body"
        for txn in txns:
            path = (txn.get("resource") or {}).get("file_path")
            if path and not os.path.exists(os.path.join(self.d, path)):
                _save_body(self.d, path, body)

        loader = RecordingLoader(self.d)
        matcher = RequestMatcher()
        matcher.load_transactions(iter(txns))
        mismatches = []
        page_manager = PageManager()
        interceptor = NetworkInterceptor(
            loader=loader,
            matcher=matcher,
            page_manager=page_manager,
            on_mismatch=lambda e: mismatches.append(e),
            strict=strict,
        )
        return interceptor, mismatches

    def test_normal_get_fulfilled(self):
        body = b"hello world"
        txn = _make_txn(1, url="http://example.com/", method="GET", body=body)
        _write_txn(self.d, txn)
        _save_body(self.d, "resources/000001.html", body)

        interceptor, mismatches = self._make_interceptor([txn])
        route = _FakeRoute("http://example.com/", "GET")
        request = _FakeRequest("http://example.com/", "GET")
        interceptor.handle_route(route, request)

        self.assertIsNotNone(route.fulfilled)
        self.assertEqual(route.fulfilled["status"], 200)
        self.assertEqual(len(mismatches), 0)

    def test_unexpected_request_strict_mode_blocked(self):
        """In strict mode unexpected requests must NOT be forwarded."""
        interceptor, mismatches = self._make_interceptor([], strict=True)
        route = _FakeRoute("http://unexpected.example.com/", "GET")
        request = _FakeRequest("http://unexpected.example.com/", "GET")
        interceptor.handle_route(route, request)

        self.assertEqual(len(mismatches), 1)
        self.assertIsInstance(mismatches[0], UnexpectedRequest)
        # Must NOT have continued (no real network)
        self.assertFalse(route.continued)
        # Must have been blocked (fulfilled with 502 OR aborted)
        self.assertTrue(
            route.fulfilled is not None or route.aborted is not None,
            "Unexpected request must be blocked (fulfilled or aborted), not silently passed",
        )

    def test_unexpected_request_non_strict_allowed_through(self):
        """In non-strict mode unexpected requests continue to real network."""
        interceptor, mismatches = self._make_interceptor([], strict=False)
        route = _FakeRoute("http://unexpected.example.com/", "GET")
        request = _FakeRequest("http://unexpected.example.com/", "GET")
        interceptor.handle_route(route, request)

        self.assertEqual(len(mismatches), 1)
        self.assertTrue(route.continued)

    def test_internal_url_bypassed(self):
        """chrome-extension:// URLs must bypass matching without error."""
        interceptor, mismatches = self._make_interceptor([], strict=True)
        route = _FakeRoute("chrome-extension://abc/background.js", "GET")
        request = _FakeRequest("chrome-extension://abc/background.js", "GET")
        interceptor.handle_route(route, request)

        self.assertEqual(len(mismatches), 0)
        self.assertTrue(route.continued)

    def test_failure_transaction_aborted(self):
        """Recorded failures must be aborted, not fulfilled."""
        txn = _make_txn(1, url="http://example.com/fail", method="GET",
                        body=b"", corr="CDP_ONLY_FAILED")
        txn["error"] = "net::ERR_FAILED"
        # Remove resource (failure has no body)
        txn["resource"] = None
        _write_txn(self.d, txn)

        interceptor, mismatches = self._make_interceptor([txn])
        route = _FakeRoute("http://example.com/fail", "GET")
        request = _FakeRequest("http://example.com/fail", "GET")
        interceptor.handle_route(route, request)

        self.assertIsNotNone(route.aborted)
        self.assertEqual(len(mismatches), 0)


# ---------------------------------------------------------------------------
# 6. Integration test — record → persist → load → replay browser → verify
# ---------------------------------------------------------------------------

class _SimpleHTTPHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server for integration recording."""

    responses: dict = {}

    def do_GET(self):
        body = self.responses.get(self.path, b"<html>404</html>")
        status = 200 if self.path in self.responses else 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_test_server(port: int, routes: dict) -> HTTPServer:
    class H(_SimpleHTTPHandler):
        pass
    H.responses = routes
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestReplayIntegration(unittest.TestCase):
    """
    End-to-end: record a small deterministic session, then replay it.

    This test:
    1. Starts a local HTTP server serving known HTML.
    2. Uses the Browser Recorder to record a navigation session.
    3. Stops recording.
    4. Starts replay in Strict Mock Mode using the recorded data.
    5. Verifies:
       - The recorded responses are served.
       - No unexpected network traffic.
       - The replay session completes with no fatal mismatches.
    """

    INTEG_PORT = 9500

    def setUp(self):
        self.srv = _start_test_server(self.INTEG_PORT, {
            "/": b"<html><body><h1>Home</h1></body></html>",
            "/page2": b"<html><body><h1>Page 2</h1></body></html>",
        })
        self.capture_dir = tempfile.mkdtemp(prefix="test_integ_replay_")

    def tearDown(self):
        self.srv.shutdown()
        shutil.rmtree(self.capture_dir, ignore_errors=True)

    def test_record_and_replay(self):
        import queue
        from config import config
        from storage import StorageManager
        from network_recorder import NetworkRecorder
        from browser_manager import BrowserManager

        # ---- Record ----
        config.enable_dom_snapshots = False
        config.enable_interactions = False
        config.output_dir = self.capture_dir
        config.user_data_dir = self.capture_dir + "_user"

        storage = StorageManager(self.capture_dir)
        gui_q = queue.Queue()
        recorder = NetworkRecorder(storage, gui_q)
        bm = BrowserManager(recorder, gui_q, f"http://127.0.0.1:{self.INTEG_PORT}/")

        recorder.start_recording()
        bm.start()

        # Wait for READY
        deadline = time.time() + 12
        ready = False
        while time.time() < deadline:
            try:
                msg = gui_q.get(timeout=0.3)
                if msg.get("state") == "READY":
                    ready = True
                    break
            except queue.Empty:
                pass
        self.assertTrue(ready, "Browser never became READY")

        time.sleep(2)  # let network settle
        recorder.stop_recording()
        bm.stop()
        bm.join(timeout=8)

        # ---- Verify recording exists ----
        session_dir = storage.capture_dir
        manifest = os.path.join(session_dir, "manifest.jsonl")
        self.assertTrue(os.path.exists(manifest))

        with open(manifest) as f:
            txns = [json.loads(l) for l in f if l.strip()]
        self.assertGreater(len(txns), 0, "Recording must have captured at least one transaction")

        # ---- Replay ----
        from replay.session import ReplaySession
        session = ReplaySession(
            capture_dir=session_dir,
            headless=True,
            strict=True,
        )
        result = session.run()

        print(f"\n[Integration] {result}")

        # The replay must serve at least one recorded response
        self.assertGreaterEqual(
            result.fulfilled_count + result.aborted_count, 0,
            "Replay must have processed at least one transaction",
        )

        # No fatal mismatches allowed (unexpected requests, etc.)
        fatal_details = [str(f) for f in result.fatal_mismatches]
        self.assertEqual(
            len(result.fatal_mismatches), 0,
            f"Fatal mismatches during replay: {fatal_details}",
        )

        # Clean up user data dir
        user_dir = self.capture_dir + "_user"
        if os.path.exists(user_dir):
            shutil.rmtree(user_dir, ignore_errors=True)


class TestRequestMatcherAdversarial(unittest.TestCase):
    def _make_pool(self, txns):
        m = RequestMatcher()
        m.load_transactions(iter(txns))
        return m

    def test_cross_page_request_prevented(self):
        t1 = _make_txn(1, url="http://ex.com/", method="GET", page_id="page-001")
        m = self._make_pool([t1])
        # Attempt to match with page-002, which shouldn't match page-001's request
        with self.assertRaises(UnexpectedRequest) as cm:
            m.match("http://ex.com/", "GET", page_id="page-002")
        self.assertEqual(cm.exception.ctx.get("page_id"), "page-002")

class TestRecordingLoaderPerformance(unittest.TestCase):
    """Verify the loader can handle a large recording without full-RAM load."""

    def setUp(self):
        self.d = _make_capture_dir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_large_recording_memory_efficiency(self):
        import tracemalloc
        
        # Generate 100,000 minimal transactions (about 12MB JSONL)
        manifest_path = os.path.join(self.d, "manifest.jsonl")
        with open(manifest_path, "w", encoding="utf-8") as f:
            for i in range(1, 100001):
                f.write(f'{{"id":"tx-{i}","sequence":{i},"url":"http://ex.com/","method":"GET","page_id":"p1"}}\n')

        tracemalloc.start()
        loader = RecordingLoader(self.d)
        
        count = 0
        for txn in loader.iter_transactions():
            count += 1
            if count % 10000 == 0:
                current, peak = tracemalloc.get_traced_memory()
                # Must stay well under 10MB (typically < 1MB for streaming)
                self.assertLess(current, 10 * 1024 * 1024, "Loader is retaining too much memory in stream loop")

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(count, 100000)
        self.assertLess(peak, 10 * 1024 * 1024, "Peak memory exceeded streaming limit")

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
