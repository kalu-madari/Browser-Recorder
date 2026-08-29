import queue
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import time
from collections import defaultdict
from models import TransactionState, ResourceRecord, GUIEvent, WebSocketFrame, WebSocketState
from resource_classifier import determine_extension
from storage import StorageManager
from config import config

logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Intermediate pending records — kept separate until fingerprint proves match
# ---------------------------------------------------------------------------

@dataclass
class PendingCDPRecord:
    """
    Holds data that arrived via CDP Network.requestWillBeSent.
    Intentionally kept isolated from any Playwright data until the timing
    fingerprint in handle_response/handle_request_finished proves which
    Playwright Request object corresponds to this CDP requestId.
    """
    request_id: str
    sequence: int
    url: str
    method: str
    request_headers: dict
    initiator: Optional[dict]
    resource_type: str
    page_id: str
    frame_id: str
    request_time: str
    fp: Optional[str] = None
    loader_id: Optional[str] = None
@dataclass
class PendingPWRecord:
    """
    Holds data that arrived via Playwright handle_request.
    Intentionally kept isolated from any CDP transaction until the timing
    fingerprint proves which CDP requestId this Playwright Request belongs to.

    No CDP requestId is stored here. No FIFO lookup is performed.
    """
    pw_id: int           # id(playwright_request)
    page_id: str
    pw_frame: Any
    url: str
    method: str
    request_headers: dict
    resource_type: str
    frame_url: str
    request_cookies: list
    post_data: Optional[str]
    post_data_file: Optional[str]
    request_time: str
    request_obj: Any     # the Playwright Request object, for body reads in fallback
    
    # Stashed response data (in case handle_response fires before CDP fingerprint)
    response_status: Optional[int] = None
    response_status_text: Optional[str] = None
    response_headers: Optional[dict] = None
    content_type: Optional[str] = None
def _format_milestone(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return f
    except (ValueError, TypeError):
        return None
def compute_cdp_timing_fingerprint(timing: Optional[dict]) -> Optional[str]:
    """
    Compute timing fingerprint from CDP Network.responseReceived timing dict.
    Returns None if timing is absent, non-dict, or lacks positive timing milestones.
    """
    if not timing or not isinstance(timing, dict):
        return None
    milestones = [
        _format_milestone(timing.get("dnsStart")),
        _format_milestone(timing.get("dnsEnd")),
        _format_milestone(timing.get("connectStart")),
        _format_milestone(timing.get("connectEnd")),
        _format_milestone(timing.get("sendStart")),
        _format_milestone(timing.get("receiveHeadersEnd")),
    ]
    # Check if at least one meaningful positive timing milestone exists
    if not any(m is not None and m >= 0 for m in milestones):
        return None
    return "_".join(f"{m:.3f}" if m is not None else "None" for m in milestones)
def compute_pw_timing_fingerprint(timing: Optional[dict]) -> Optional[str]:
    """
    Compute timing fingerprint from Playwright request.timing dict.
    Returns None if timing is absent, non-dict, or lacks positive timing milestones.
    """
    if not timing or not isinstance(timing, dict):
        return None
    milestones = [
        _format_milestone(timing.get("domainLookupStart")),
        _format_milestone(timing.get("domainLookupEnd")),
        _format_milestone(timing.get("connectStart")),
        _format_milestone(timing.get("connectEnd")),
        _format_milestone(timing.get("requestStart")),
        _format_milestone(timing.get("responseStart")),
    ]
    if not any(m is not None and m >= 0 for m in milestones):
        return None
    return "_".join(f"{m:.3f}" if m is not None else "None" for m in milestones)
class FrameRegistry:
    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self._mapping = {} # guid -> cdp_frame_id

    def _get_key(self, pw_frame) -> str:
        impl = getattr(pw_frame, '_impl_obj', pw_frame)
        guid = getattr(impl, '_guid', None)
        if guid: return guid
        return str(id(pw_frame))

    def map_frame(self, pw_frame, cdp_frame_id: str):
        if not pw_frame or not cdp_frame_id:
            return
        key = self._get_key(pw_frame)
        with self.lock:
            self._mapping[key] = cdp_frame_id

    def get_cdp_frame_id(self, pw_frame):
        if not pw_frame:
            return None
        key = self._get_key(pw_frame)
        with self.lock:
            return self._mapping.get(key) or "unknown"
class NetworkRecorder:
    def __init__(self, storage: StorageManager, gui_queue: queue.Queue):
        self.storage = storage
        self.gui_queue = gui_queue
        self.seq_counter = 0
        self.lock = threading.RLock()
        self.recording = False
        self.frame_registry = FrameRegistry()

        # -------------------------------------------------------------------
        # State-machine maps
        # -------------------------------------------------------------------

        # CDP-only pending: requestId -> PendingCDPRecord
        # Populated by attach_cdp_request. Never enriched with Playwright data.
        self.pending_cdp: Dict[str, PendingCDPRecord] = {}

        # Playwright-only pending: pw_id -> PendingPWRecord
        # Populated by handle_request. No CDP requestId assigned.
        self.pending_pw: Dict[int, PendingPWRecord] = {}

        # CDP timing fingerprint -> list of requestIds that produced that fingerprint.
        # Populated by attach_cdp_response. Consumed at correlation time.
        self.cdp_timing_fps: Dict[str, List[str]] = defaultdict(list)

        # In-flight correlated transactions (CDP_CORRELATED state).
        # Keyed by CDP requestId. Populated by handle_response after fingerprint match.
        # Consumed by handle_request_finished.
        self.active_transactions: Dict[str, TransactionState] = {}

        # After handle_response correlates, record pw_id -> cdp_id so that
        # handle_request_finished can retrieve the transaction without re-doing
        # the fingerprint lookup.
        self.pw_to_active: Dict[int, str] = {}

        # Navigation correlation: CDP loaderId -> sequence number of document request
        self.loader_to_seq: Dict[str, int] = {}

        # WebSocket state (unchanged)
        self.active_websockets: Dict[str, WebSocketState] = {}

        # -------------------------------------------------------------------
        # Instrumentation counters (readable by tests)
        # -------------------------------------------------------------------
        self.counters: Dict[str, int] = {
            # By construction, speculative_assignments stays 0.
            "speculative_assignments": 0,
            # Non-zero would mean a correlation was overwritten after assignment.
            "correlation_corrections": 0,
            # Non-zero would mean identity changed after enrichment was written.
            "post_correlation_identity_changes": 0,
            # Non-zero means Playwright data was attached before fingerprint proof.
            "premature_enrichments": 0,
            # Non-zero means _finalize_txn was called on an already-finalized txn.
            "duplicate_finalizations": 0,
            # Non-zero means request id vs response echo id mismatch detected.
            "cross_wired": 0,
            # Non-zero means multiple CDP requests produced an identical timing fingerprint.
            "fingerprint_collisions": 0,
        }

    # -----------------------------------------------------------------------
    # Lifecycle control
    # -----------------------------------------------------------------------

    def get_next_seq(self):
        with self.lock:
            self.seq_counter += 1
            return self.seq_counter

    def start_recording(self):
        self.recording = True

    
    def handle_page_close(self, page_id: str):
        with self.lock:
            # Clean up pending CDP requests
            for req_id, rec in list(self.pending_cdp.items()):
                if rec.page_id == page_id:
                    del self.pending_cdp[req_id]
                    if rec.fp and rec.fp in self.cdp_timing_fps:
                        if req_id in self.cdp_timing_fps[rec.fp]:
                            self.cdp_timing_fps[rec.fp].remove(req_id)
                        if not self.cdp_timing_fps[rec.fp]:
                            del self.cdp_timing_fps[rec.fp]
                    
            # Clean up active websockets
            for ws_id, ws_state in list(self.active_websockets.items()):
                if ws_state.page_id == page_id:
                    ws_state.status = "CLOSED"
                    ws_state.closed_time = __import__('datetime').datetime.now().isoformat()
                    ws_state.close_reason = "Page Closed"
                    if self.storage:
                        self.storage.write_websocket_state(ws_state)
                    del self.active_websockets[ws_id]

    def stop_recording(self):
        self.recording = False
        self.frame_registry = FrameRegistry()
        logger.info("Stopping recording. Flushing pending records...")

        # Give in-flight requests up to 5 s to complete
        start_wait = time.time()
        while time.time() - start_wait < 5.0:
            with self.lock:
                if not self.active_transactions and not self.pending_cdp and not self.pending_pw:
                    break
            time.sleep(0.1)

        with self.lock:
            # Flush in-flight CDP_CORRELATED transactions (requestfinished never arrived)
            for txn in list(self.active_transactions.values()):
                if not txn.finalized:
                    txn.error = "INCOMPLETE (Recording stopped before requestfinished)"
                    txn.completed = True
                    txn.completion_time = datetime.now().isoformat()
                    txn.correlation_state = "FINALIZED"
                    txn.finalized = True
                    if self.storage:
                        self.storage.finalize_transaction(txn)
            self.active_transactions.clear()
            self.pw_to_active.clear()

            # Flush remaining CDP-only orphans (no Playwright counterpart / no timing proof)
            for request_id, cdp_rec in list(self.pending_cdp.items()):
                txn = TransactionState(
                    id=request_id,
                    sequence=cdp_rec.sequence,
                    url=cdp_rec.url,
                    method=cdp_rec.method,
                    request_headers=cdp_rec.request_headers,
                    request_time=cdp_rec.request_time,
                    resource_type=cdp_rec.resource_type,
                    page_id=cdp_rec.page_id,
                    frame_id=cdp_rec.frame_id,
                    frame_url="",
                    initiator=cdp_rec.initiator,
                    error="INCOMPLETE (CDP-only: no Playwright event observed)",
                    completed=True,
                    completion_time=datetime.now().isoformat(),
                    correlation_state="FINALIZED",
                    finalized=True,
                )
                if self.storage:
                    self.storage.finalize_transaction(txn)
            self.pending_cdp.clear()

            # Flush remaining Playwright-only orphans (no CDP fingerprint observed)
            for pw_id, pw_rec in list(self.pending_pw.items()):
                self.seq_counter += 1
                txn = TransactionState(
                    id=f"pw-{pw_id}",
                    sequence=self.seq_counter,
                    url=pw_rec.url,
                    method=pw_rec.method,
                    request_headers=pw_rec.request_headers,
                    request_time=pw_rec.request_time,
                    resource_type=pw_rec.resource_type,
                    page_id=pw_rec.page_id,
                    frame_id=self.frame_registry.get_cdp_frame_id(pw_rec.pw_frame) if getattr(pw_rec, "pw_frame", None) else "unknown",
                    frame_url=pw_rec.frame_url,
                    post_data=pw_rec.post_data,
                    post_data_file=pw_rec.post_data_file,
                    request_cookies=pw_rec.request_cookies,
                    error="INCOMPLETE (Playwright-only: no CDP fingerprint observed)",
                    completed=True,
                    completion_time=datetime.now().isoformat(),
                    correlation_state="FINALIZED",
                    finalized=True,
                )
                if self.storage:
                    self.storage.finalize_transaction(txn)
            self.pending_pw.clear()

            # Clear timing fingerprints dictionary to prevent stale leaks
            self.cdp_timing_fps.clear()

            # Finalize open websockets
            for ws_id, ws_state in list(self.active_websockets.items()):
                if ws_state.status == "OPEN":
                    ws_state.status = "OPEN_AT_CAPTURE_END"
                    if self.storage:
                        self.storage.write_websocket_state(ws_state)
            self.active_websockets.clear()

        if self.storage:
            self.storage.finalize()

        self.gui_queue.put({"type": "COMPLETED"})

    # -----------------------------------------------------------------------
    # CDP event handlers
    # -----------------------------------------------------------------------

    def attach_cdp_request(self, url: str, method: str, request_id: str,
                           initiator: dict, event: dict, page_id: str):
        """
        Called when CDP Network.requestWillBeSent fires.

        Creates a PendingCDPRecord. Does NOT associate with any Playwright
        Request. Does NOT perform FIFO queue lookup. Does NOT enrich with
        post_data, cookies, or Playwright-side headers.
        """
        if not self.recording:
            return
        with self.lock:
            self.seq_counter += 1
            seq = self.seq_counter
            ts = datetime.now().isoformat()
            req_data = event.get("request", {})
            loader_id = event.get("loaderId")

            record = PendingCDPRecord(
                request_id=request_id,
                sequence=seq,
                url=url,
                method=method,
                request_headers=req_data.get("headers", {}),
                initiator=initiator,
                resource_type=event.get("type", "Fetch"),
                page_id=page_id,
                frame_id=event.get('frameId', 'unknown'),
                fp=None,
                request_time=ts,
                loader_id=loader_id,
            )
            self.pending_cdp[request_id] = record

            # Navigation correlation bookkeeping (unchanged behaviour)
            if loader_id and event.get("type") == "Document":
                self.loader_to_seq[loader_id] = seq

            # Emit a GUI STARTED event with partial CDP data so the UI shows activity.
            # This transaction object is ephemeral — it is not persisted.
            partial_txn = TransactionState(
                id=request_id,
                sequence=seq,
                url=url,
                method=method,
                request_headers=record.request_headers,
                request_time=ts,
                resource_type=record.resource_type,
                page_id=page_id,
                frame_id=event.get('frameId', 'unknown'),
                frame_url="",
                initiator=initiator,
                correlation_state="PENDING_CDP",
            )
            self.gui_queue.put(GUIEvent(type="STARTED", transaction=partial_txn))

    
    def attach_cdp_loading_finished(self, request_id: str, error: str = None):
        pass

    def attach_cdp_response(self, event: dict):
        """
        Called when CDP Network.responseReceived fires.

        Extracts the server-side timing dictionary from the CDP response and
        stores it keyed by its fingerprint so that handle_response can look
        it up when the Playwright timing values arrive.
        """
        request_id = event.get("requestId")
        resp = event.get("response", {})
        t = resp.get("timing")
        if t and request_id:
            fp = compute_cdp_timing_fingerprint(t)
            if fp:
                with self.lock:
                    if fp in self.cdp_timing_fps and len(self.cdp_timing_fps[fp]) > 0:
                        self.counters["fingerprint_collisions"] += 1
                    self.cdp_timing_fps[fp].append(request_id)
                    if request_id in self.pending_cdp:
                        self.pending_cdp[request_id].fp = fp

    # -----------------------------------------------------------------------
    # Playwright event handlers
    # -----------------------------------------------------------------------

    def handle_request(self, request, page_id: str):
        """
        Called when Playwright fires the 'request' event.

        Creates a PendingPWRecord. Does NOT assign a CDP requestId.
        Does NOT perform FIFO queue lookup. Playwright data stays isolated
        until the timing fingerprint proves which CDP requestId it belongs to.
        """
        if not self.recording:
            return
        impl = getattr(request, "_impl_obj", request)
        pw_id = getattr(impl, "_guid", str(id(request)))
        ts = datetime.now().isoformat()

        # Read post_data here, in the Playwright event callback thread,
        # before entering the lock (I/O should not block under the lock).
        post_data = None
        post_data_file = None
        if config.save_request_bodies:
            try:
                post_data = request.post_data
            except Exception:
                pass
                
            if not post_data:
                try:
                    buf = request.post_data_buffer
                    if buf:
                        post_data_file = self.storage.save_request_body(pw_id, buf)
                except Exception as e:
                    logger.error(f"Request body read error: {e}")

        cookies = []
        cookie_header = request.headers.get("cookie", "")
        if cookie_header:
            cookies = [
                {
                    "name": c.split("=", 1)[0].strip(),
                    "value": c.split("=", 1)[1].strip() if "=" in c else "",
                }
                for c in cookie_header.split(";")
            ]

        with self.lock:
            record = PendingPWRecord(
                pw_id=pw_id,
                page_id=page_id,
                pw_frame=request.frame,
                url=request.url,
                method=request.method,
                request_headers=dict(request.headers),
                resource_type=request.resource_type,
                frame_url=request.frame.url if request.frame else "",
                request_cookies=cookies,
                post_data=post_data,
                post_data_file=post_data_file,
                request_time=ts,
                request_obj=request,
            )
            self.pending_pw[pw_id] = record

    def handle_response(self, response):
        """
        Called when Playwright fires the 'response' event.

        Attempts timing fingerprint correlation. On exact match:
          - Removes PendingCDPRecord and PendingPWRecord from their maps.
          - Merges them into a TransactionState (CDP_CORRELATED).
          - Stores the merged transaction in active_transactions.
          - Records pw_to_active so handle_request_finished can retrieve it.

        No Playwright data is written to any CDP transaction before this point.
        The merge is atomic under self.lock.

        After releasing the lock, reads the response body (blocking I/O).
        """
        impl = getattr(response.request, "_impl_obj", response.request)
        pw_id = getattr(impl, "_guid", str(id(response.request)))
        ts = datetime.now().isoformat()

        txn: Optional[TransactionState] = None

        with self.lock:
            t = response.request.timing
            if t:
                fp = compute_pw_timing_fingerprint(t)
                if fp and fp in self.cdp_timing_fps and self.cdp_timing_fps[fp]:
                    cdp_id = self.cdp_timing_fps[fp].pop(0)
                    if not self.cdp_timing_fps[fp]:
                        del self.cdp_timing_fps[fp]

                    cdp_rec = self.pending_cdp.pop(cdp_id, None)
                    pw_rec = self.pending_pw.pop(pw_id, None)

                    if cdp_rec:
                        # Merge: Playwright headers supplement CDP headers
                        merged_headers = dict(cdp_rec.request_headers)
                        if pw_rec:
                            merged_headers.update(pw_rec.request_headers)

                        txn = TransactionState(
                            id=cdp_rec.request_id,
                            sequence=cdp_rec.sequence,
                            url=cdp_rec.url,
                            method=cdp_rec.method,
                            request_headers=merged_headers,
                            request_time=cdp_rec.request_time,
                            resource_type=pw_rec.resource_type if pw_rec else cdp_rec.resource_type,
                            page_id=cdp_rec.page_id,
                            frame_id=cdp_rec.frame_id,
                            frame_url=pw_rec.frame_url if pw_rec else "",
                            initiator=cdp_rec.initiator,
                            post_data=pw_rec.post_data if pw_rec else None,
                            post_data_file=pw_rec.post_data_file if pw_rec else None,
                            request_cookies=pw_rec.request_cookies if pw_rec else [],
                            response_time=ts,
                            status=response.status,
                            status_text=response.status_text,
                            response_headers=dict(response.headers),
                            content_type=response.headers.get("content-type", ""),
                            correlation_state="CDP_CORRELATED",
                        )
                        self.active_transactions[cdp_id] = txn
                        self.pw_to_active[pw_id] = cdp_id

        if txn is None:
            # Fingerprint did not match. This can happen legitimately when:
            #   - Playwright's response event fires before CDP responseReceived
            #     (the fingerprint will arrive later via attach_cdp_response and
            #     will be consumed by handle_request_finished)
            #   - The request has no timing (cached, main-document suppressed by PW)
            # Stash the response metadata in pending_pw so handle_request_finished can use it.
            with self.lock:
                pw_rec = self.pending_pw.get(pw_id)
                if pw_rec:
                    pw_rec.response_status = response.status
                    pw_rec.response_status_text = response.status_text
                    pw_rec.response_headers = dict(response.headers)
                    pw_rec.content_type = response.headers.get("content-type", "")
            return

        # Response body read — outside the lock to avoid blocking other callbacks
        if config.save_response_bodies:
            try:
                body = response.body()
                if body:
                    ext = determine_extension(txn.url, txn.content_type, txn.resource_type)
                    res_info = self.storage.save_resource(txn.sequence, body, ext)
                    if res_info:
                        with self.lock:
                            if not txn.finalized:
                                txn.resource = ResourceRecord(
                                    file_path=res_info["file_path"],
                                    size=res_info["size"],
                                    sha256=res_info["sha256"],
                                    mime_type=txn.content_type,
                                    extension=ext,
                                )
            except Exception as e:
                logger.debug(f"Response body unavailable for {txn.url}: {e}")

        self.gui_queue.put(GUIEvent(type="UPDATED", transaction=txn))

    def handle_request_finished(self, request):
        """
        Called when Playwright fires the 'requestfinished' event.

        Deterministic 3-path resolution:
        1. CDP_CORRELATED via handle_response: pw_to_active has an entry.
           Retrieve and finalize the transaction.
        2. Fingerprint present but handle_response didn't run first (e.g. CDP
           responseReceived arrived between Playwright response and requestfinished):
           correlate here via exact timing fingerprint.
        3. No timing proof: PLAYWRIGHT_ONLY fallback. Build transaction from PendingPWRecord
           without speculative CDP ID guessing.
        """
        impl = getattr(request, "_impl_obj", request)
        pw_id = getattr(impl, "_guid", str(id(request)))
        ts = datetime.now().isoformat()

        txn: Optional[TransactionState] = None

        with self.lock:
            # Path 1: already correlated by handle_response
            cdp_id = self.pw_to_active.pop(pw_id, None)
            if cdp_id:
                txn = self.active_transactions.pop(cdp_id, None)

            # Path 2: fingerprint present but handle_response didn't correlate yet
            if txn is None:
                t = getattr(request, "timing", None)
                if t:
                    fp = compute_pw_timing_fingerprint(t)
                    if fp and fp in self.cdp_timing_fps and self.cdp_timing_fps[fp]:
                        cdp_id = self.cdp_timing_fps[fp].pop(0)
                        if not self.cdp_timing_fps[fp]:
                            del self.cdp_timing_fps[fp]
                        cdp_rec = self.pending_cdp.pop(cdp_id, None)
                        pw_rec = self.pending_pw.pop(pw_id, None)
                        if cdp_rec and pw_rec and pw_rec.pw_frame and cdp_rec.frame_id:
                            self.frame_registry.map_frame(pw_rec.pw_frame, cdp_rec.frame_id)

                        if cdp_rec:
                            merged_headers = dict(cdp_rec.request_headers)
                            if pw_rec:
                                merged_headers.update(pw_rec.request_headers)

                            txn = TransactionState(
                                id=cdp_rec.request_id,
                                sequence=cdp_rec.sequence,
                                url=cdp_rec.url,
                                method=cdp_rec.method,
                                request_headers=merged_headers,
                                request_time=cdp_rec.request_time,
                                resource_type=pw_rec.resource_type if pw_rec else cdp_rec.resource_type,
                                page_id=cdp_rec.page_id,
                                frame_id=cdp_rec.frame_id,
                                frame_url=pw_rec.frame_url if pw_rec else "",
                                initiator=cdp_rec.initiator,
                                post_data=pw_rec.post_data if pw_rec else None,
                                post_data_file=pw_rec.post_data_file if pw_rec else None,
                                request_cookies=pw_rec.request_cookies if pw_rec else [],
                                correlation_state="CDP_CORRELATED",
                            )
                            if pw_rec and pw_rec.response_status is not None:
                                txn.status = pw_rec.response_status
                                txn.status_text = pw_rec.response_status_text
                                txn.response_headers = pw_rec.response_headers or {}
                                txn.content_type = pw_rec.content_type or ""
                            else:
                                try:
                                    resp = request.response()
                                    if resp:
                                        txn.status = resp.status
                                        txn.status_text = resp.status_text
                                        txn.response_headers = dict(resp.headers)
                                        txn.content_type = resp.headers.get("content-type", "")
                                except Exception:
                                    pass

            # Path 3: Deterministic PLAYWRIGHT_ONLY fallback (no speculative CDP ID guessing)
            if txn is None:
                pw_rec = self.pending_pw.pop(pw_id, None)
                self.seq_counter += 1
                seq = self.seq_counter

                if pw_rec:
                    post_data = pw_rec.post_data
                    post_data_file = pw_rec.post_data_file
                    req_headers = pw_rec.request_headers
                    req_cookies = pw_rec.request_cookies
                    frame_url = pw_rec.frame_url
                    resource_type = pw_rec.resource_type
                    req_time = pw_rec.request_time
                else:
                    post_data = None
                    post_data_file = None
                    req_headers = dict(request.headers)
                    req_cookies = []
                    frame_url = request.frame.url if request.frame else ""
                    resource_type = request.resource_type
                    req_time = ts
                    if config.save_request_bodies:
                        try:
                            post_data = request.post_data
                        except Exception:
                            pass

                txn = TransactionState(
                    id=f"pw-{pw_id}",
                    sequence=seq,
                    url=request.url,
                    method=request.method,
                    request_headers=req_headers,
                    request_time=req_time,
                    resource_type=resource_type,
                    page_id=pw_rec.page_id if pw_rec else "",
                    frame_id=self.frame_registry.get_cdp_frame_id(pw_rec.pw_frame) if (pw_rec and getattr(pw_rec, "pw_frame", None)) else "unknown",
                    frame_url=frame_url,
                    post_data=post_data,
                    post_data_file=post_data_file,
                    request_cookies=req_cookies,
                    correlation_state="PLAYWRIGHT_ONLY",
                )

                # Get response metadata for PLAYWRIGHT_ONLY
                if pw_rec and pw_rec.response_status is not None:
                    txn.status = pw_rec.response_status
                    txn.status_text = pw_rec.response_status_text
                    txn.response_headers = pw_rec.response_headers or {}
                    txn.content_type = pw_rec.content_type or ""
                else:
                    try:
                        resp = request.response()
                        if resp:
                            txn.status = resp.status
                            txn.status_text = resp.status_text
                            txn.response_headers = dict(resp.headers)
                            txn.content_type = resp.headers.get("content-type", "")
                    except Exception:
                        pass

        if txn is None:
            return

        txn.response_time = txn.response_time or ts
        txn.completed = True
        txn.completion_time = ts

        # Read response body for PLAYWRIGHT_ONLY (not already done by handle_response)
        if txn.correlation_state == "PLAYWRIGHT_ONLY" and config.save_response_bodies and txn.status:
            try:
                resp = request.response()
                if resp:
                    body = resp.body()
                    if body:
                        ext = determine_extension(txn.url, txn.content_type, txn.resource_type)
                        res_info = self.storage.save_resource(txn.sequence, body, ext)
                        if res_info:
                            txn.resource = ResourceRecord(
                                file_path=res_info["file_path"],
                                size=res_info["size"],
                                sha256=res_info["sha256"],
                                mime_type=txn.content_type,
                                extension=ext,
                            )
            except Exception:
                pass

        if (
            not txn.error
            and not txn.resource
            and config.save_response_bodies
            and txn.status not in (None, 204, 301, 302, 303, 304, 307, 308)
        ):
            txn.error = "Body unavailable"

        self._finalize_txn(txn)

    def handle_request_failed(self, request):
        """
        Called when Playwright fires the 'requestfailed' event.

        Follows the same deterministic correlation paths as handle_request_finished,
        but sets txn.error = request.failure and skips body reads.
        """
        impl = getattr(request, "_impl_obj", request)
        pw_id = getattr(impl, "_guid", str(id(request)))
        ts = datetime.now().isoformat()

        txn: Optional[TransactionState] = None

        with self.lock:
            # Path 1: already correlated
            cdp_id = self.pw_to_active.pop(pw_id, None)
            if cdp_id:
                txn = self.active_transactions.pop(cdp_id, None)

            # Path 2: fingerprint-based correlation (rare for failures, but possible)
            if txn is None:
                t = getattr(request, "timing", None)
                if t:
                    fp = compute_pw_timing_fingerprint(t)
                    if fp and fp in self.cdp_timing_fps and self.cdp_timing_fps[fp]:
                        cdp_id = self.cdp_timing_fps[fp].pop(0)
                        if not self.cdp_timing_fps[fp]:
                            del self.cdp_timing_fps[fp]
                        cdp_rec = self.pending_cdp.pop(cdp_id, None)
                        pw_rec = self.pending_pw.pop(pw_id, None)
                        if cdp_rec and pw_rec and pw_rec.pw_frame and cdp_rec.frame_id:
                            self.frame_registry.map_frame(pw_rec.pw_frame, cdp_rec.frame_id)
                        if cdp_rec:
                            merged_headers = dict(cdp_rec.request_headers)
                            if pw_rec:
                                merged_headers.update(pw_rec.request_headers)
                            txn = TransactionState(
                                id=cdp_rec.request_id,
                                sequence=cdp_rec.sequence,
                                url=cdp_rec.url,
                                method=cdp_rec.method,
                                request_headers=merged_headers,
                                request_time=cdp_rec.request_time,
                                resource_type=pw_rec.resource_type if pw_rec else cdp_rec.resource_type,
                                page_id=cdp_rec.page_id,
                                frame_id=cdp_rec.frame_id,
                                frame_url=pw_rec.frame_url if pw_rec else "",
                                initiator=cdp_rec.initiator,
                                post_data=pw_rec.post_data if pw_rec else None,
                                post_data_file=pw_rec.post_data_file if pw_rec else None,
                                request_cookies=pw_rec.request_cookies if pw_rec else [],
                                correlation_state="CDP_CORRELATED",
                            )

            # Path 3: Deterministic PLAYWRIGHT_ONLY fallback
            if txn is None:
                pw_rec = self.pending_pw.pop(pw_id, None)
                self.seq_counter += 1
                seq = self.seq_counter
                if pw_rec:
                    txn = TransactionState(
                        id=f"pw-{pw_id}",

                        sequence=seq,

                        url=pw_rec.url,

                        method=pw_rec.method,

                        request_headers=pw_rec.request_headers,

                        request_time=pw_rec.request_time,

                        resource_type=pw_rec.resource_type,

                        page_id=pw_rec.page_id,

                        frame_id=self.frame_registry.get_cdp_frame_id(pw_rec.pw_frame) if getattr(pw_rec, "pw_frame", None) else "unknown",

                        frame_url=pw_rec.frame_url,
                        post_data=pw_rec.post_data,
                        post_data_file=pw_rec.post_data_file,
                        request_cookies=pw_rec.request_cookies,
                        correlation_state="PLAYWRIGHT_ONLY",
                    )
                else:
                    txn = TransactionState(
                        id=f"pw-{pw_id}",

                        sequence=seq,

                        url=request.url,

                        method=request.method,

                        request_headers=dict(request.headers),

                        request_time=ts,

                        resource_type=request.resource_type,

                        page_id="",

                        frame_id=self.frame_registry.get_cdp_frame_id(request.frame) if getattr(request, "frame", None) else "unknown",

                        frame_url=request.frame.url if request.frame else "",
                        correlation_state="PLAYWRIGHT_ONLY",
                    )

        if txn is None:
            return

        txn.completed = True
        txn.completion_time = ts
        txn.error = request.failure

        self._finalize_txn(txn)

        # Navigation failure bookkeeping (unchanged from original)
        if request.is_navigation_request():
            from models import NavigationRecord
            frame_id = (
                "main"
                if request.frame == request.frame.page.main_frame
                else (request.frame.name or f"frame-{id(request.frame)}")
            )
            nav_id = f"nav_fail_{txn.sequence:06d}"
            nav_record = NavigationRecord(
                navigation_id=nav_id,
                page_id=txn.page_id,
                frame_id=frame_id,
                timestamp=ts,
                from_url=None,
                to_url=request.url,
                type="document_navigation",
                reason="Navigation",
                status=None,
                success=False,
                document_request_sequence=txn.sequence,
                dom_snapshot_id=None,
                error=request.failure,
            )
            self.storage.save_navigation_record(nav_record)

    # -----------------------------------------------------------------------
    # Finalization (idempotent)
    # -----------------------------------------------------------------------

    def _finalize_txn(self, txn: TransactionState):
        """
        Write txn to manifest exactly once.
        If called on an already-finalized transaction, increments the
        duplicate_finalizations counter and returns without writing.
        """
        with self.lock:
            if txn.finalized:
                self.counters["duplicate_finalizations"] += 1
                return
            txn.finalized = True
            txn.correlation_state = "FINALIZED"

        self.storage.finalize_transaction(txn)
        self.gui_queue.put(GUIEvent(type="COMPLETED", transaction=txn))

    # -----------------------------------------------------------------------
    # WebSocket event handlers (unchanged from original)
    # -----------------------------------------------------------------------

    def attach_cdp_websocket(self, url: str, request_id: str, page_id: str, event: dict):
        if not self.recording:
            return
        with self.lock:
            ts = datetime.now().isoformat()
            ws_state = WebSocketState(
                id=request_id, page_id=page_id, url=url, created_time=ts
            )
            self.active_websockets[request_id] = ws_state
            if self.storage:
                self.storage.write_websocket_state(ws_state)

    def attach_cdp_websocket_handshake(
        self,
        request_id: str,
        is_request: bool,
        headers: dict,
        status: int = None,
        status_text: str = None,
    ):
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state:
                return
            if is_request:
                ws_state.handshake_request_headers = headers
            else:
                ws_state.handshake_response_headers = headers
                if status is not None:
                    ws_state.handshake_status = status
                    ws_state.handshake_status_text = status_text
            if self.storage:
                self.storage.write_websocket_state(ws_state)

    def attach_cdp_websocket_close(
        self, request_id: str, ts_close: str, reason: str = None, code: int = None
    ):
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state:
                return
            ws_state.closed_time = ts_close
            if code:
                ws_state.close_code = code
            if reason:
                ws_state.close_reason = reason
            ws_state.status = "CLOSED"
            if self.storage:
                self.storage.write_websocket_state(ws_state)
            del self.active_websockets[request_id]

    def attach_cdp_websocket_frame(self, request_id: str, direction: str, response: dict):
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state:
                return
            self.seq_counter += 1
            seq = self.seq_counter

        ts = datetime.now().isoformat()
        opcode = response.get("opcode", 1)
        payload_data = response.get("payloadData", "")

        is_text = opcode == 1
        payload_type = "text" if is_text else "binary"

        import base64

        if is_text:
            raw_bytes = payload_data.encode("utf-8")
            payload_str = payload_data
        else:
            try:
                raw_bytes = base64.b64decode(payload_data)
            except Exception:
                raw_bytes = payload_data.encode("utf-8")
            payload_str = None

        payload_size = len(raw_bytes)

        payload_file = None
        if config.save_response_bodies:
            if not is_text or payload_size >= 1000:
                if self.storage:
                    payload_file = self.storage.save_websocket_frame(
                        request_id, seq, raw_bytes, is_text
                    )
                    if payload_file:
                        payload_str = None

        frame = WebSocketFrame(
            sequence=seq,
            websocket_id=request_id,
            timestamp=ts,
            direction=direction,
            payload_type=payload_type,
            payload_size=payload_size,
            payload_file=payload_file,
            payload=payload_str,
        )

        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state:
                return
            ws_state.frames.append(frame)
            if self.storage:
                self.storage.write_websocket_frame(ws_state, frame)
                self.gui_queue.put(
                    {"type": "WS_UPDATED", "stats": len(self.active_websockets)}
                )
