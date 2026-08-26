import queue
import logging
import threading
from datetime import datetime
import time
from collections import defaultdict
from typing import Dict, Tuple
from models import TransactionState, ResourceRecord, GUIEvent, WebSocketFrame, WebSocketState
from resource_classifier import determine_extension
from storage import StorageManager
from config import config

logger = logging.getLogger(__name__)

class NetworkRecorder:
    def __init__(self, storage: StorageManager, gui_queue: queue.Queue):
        self.storage = storage
        self.gui_queue = gui_queue
        self.seq_counter = 0
        self.lock = threading.RLock()
        self.recording = False
        
        # Authoritative mapping: CDP requestId -> TransactionState
        self.active_transactions = {}
        self.loader_to_seq = {}
        
        # Bridging maps
        self.pw_to_cdp = {} # Playwright id(request) -> CDP requestId
        
        # FIFO queues used exclusively as a bridge, because Playwright Python 
        # does not expose the underlying CDP requestId on the Request object.
        # LIMITATION: This relies on the strict sequential emission order of Chromium events.
        self.unmatched_playwright_requests: Dict[Tuple[str, str], list] = defaultdict(list)
        self.unmatched_cdp_requests: Dict[Tuple[str, str], list] = defaultdict(list)
        # Timestamps for TTL eviction of stale unmatched entries (keyed by id())
        self._unmatched_timestamps: Dict[int, float] = {}
        self._UNMATCHED_TTL_SEC = 60  # evict after 60 seconds if never bridged
        
        self.active_websockets: Dict[str, WebSocketState] = {}
        self.unmatched_cdp_websockets: Dict[str, list] = defaultdict(list)
        self.unmatched_pw_websockets: Dict[str, list] = defaultdict(list)
        
    def get_next_seq(self):
        with self.lock:
            self.seq_counter += 1
            return self.seq_counter

    def _evict_stale_unmatched(self):
        """Remove unmatched entries older than _UNMATCHED_TTL_SEC. Call under self.lock."""
        now = time.time()
        cutoff = now - self._UNMATCHED_TTL_SEC
        for key in list(self.unmatched_playwright_requests.keys()):
            alive = [r for r in self.unmatched_playwright_requests[key]
                     if self._unmatched_timestamps.get(id(r), now) >= cutoff]
            evicted = len(self.unmatched_playwright_requests[key]) - len(alive)
            if evicted:
                logger.debug(f"Evicted {evicted} stale unmatched PW requests for {key}")
                self._unmatched_timestamps = {k: v for k, v in self._unmatched_timestamps.items()
                                              if any(id(r) == k for r in alive)}
            self.unmatched_playwright_requests[key] = alive
        for key in list(self.unmatched_cdp_requests.keys()):
            alive = [r for r in self.unmatched_cdp_requests[key]
                     if self._unmatched_timestamps.get(id(r), now) >= cutoff]
            evicted = len(self.unmatched_cdp_requests[key]) - len(alive)
            if evicted:
                logger.debug(f"Evicted {evicted} stale unmatched CDP requests for {key}")
            self.unmatched_cdp_requests[key] = alive
            
    def start_recording(self):
        self.recording = True
        
    def stop_recording(self):
        self.recording = False
        logger.info(f"Stopping recording. Waiting for pending requests...")
        
        # Graceful shutdown: wait for active requests to finish (max 5 seconds)
        start_wait = time.time()
        while time.time() - start_wait < 5.0:
            with self.lock:
                if not self.active_transactions:
                    break
            time.sleep(0.1)
            
        with self.lock:
            # Finalize remaining HTTP transactions
            for txn in list(self.active_transactions.values()):
                txn.error = "INCOMPLETE (Recording stopped)"
                txn.completed = True
                txn.completion_time = datetime.now().isoformat()
                if self.storage:
                    self.storage.finalize_transaction(txn)
            self.active_transactions.clear()
            
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

    def attach_cdp_request(self, url: str, method: str, request_id: str, initiator: dict, event: dict, page_id: str):
        if not self.recording: return
        key = (url, method)
        
        with self.lock:
            self._evict_stale_unmatched()
            
            # 1. Create the authoritative transaction state mapped to CDP requestId
            self.seq_counter += 1
            seq = self.seq_counter
            ts = datetime.now().isoformat()
            
            # Use CDP data as baseline
            req_data = event.get("request", {})
            txn = TransactionState(
                id=request_id, # Authoritative CDP Request ID
                sequence=seq,
                url=url,
                method=method,
                request_headers=req_data.get("headers", {}),
                request_time=ts,
                resource_type=event.get("type", "Fetch"),
                page_id=page_id,
                frame_url="",
                initiator=initiator
            )
            
            self.active_transactions[request_id] = txn
            
            loader_id = event.get("loaderId")
            if loader_id and event.get("type") == "Document":
                self.loader_to_seq[loader_id] = seq
            
            # 2. Bridge to Playwright
            if self.unmatched_playwright_requests[key]:
                # A Playwright request already arrived and is waiting for CDP
                pw_req = self.unmatched_playwright_requests[key].pop(0)
                pw_id = id(pw_req)
                self._unmatched_timestamps.pop(pw_id, None)
                self.pw_to_cdp[pw_id] = request_id
                self._enrich_transaction_from_playwright(txn, pw_req)
            else:
                self.unmatched_cdp_requests[key].append(request_id)
                self._unmatched_timestamps[id(request_id)] = time.time()
                
            self.gui_queue.put(GUIEvent(type="STARTED", transaction=txn))



    def attach_cdp_websocket(self, url: str, request_id: str, page_id: str, event: dict):
        if not self.recording: return
        
        with self.lock:
            ts = datetime.now().isoformat()
            
            ws_state = WebSocketState(
                id=request_id,
                page_id=page_id,
                url=url,
                created_time=ts
            )
            self.active_websockets[request_id] = ws_state
            
            if self.storage:
                self.storage.write_websocket_state(ws_state)
            
            self.unmatched_cdp_websockets[url].append(request_id)

    def attach_cdp_websocket_handshake(self, request_id: str, is_request: bool, headers: dict, status: int = None, status_text: str = None):
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state: return
            
            if is_request:
                ws_state.handshake_request_headers = headers
            else:
                ws_state.handshake_response_headers = headers
                if status is not None:
                    ws_state.handshake_status = status
                    ws_state.handshake_status_text = status_text
                    
            if self.storage:
                self.storage.write_websocket_state(ws_state)

    def attach_cdp_websocket_close(self, request_id: str, ts_close: str, reason: str = None, code: int = None):
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state: return
            
            ws_state.closed_time = ts_close
            if code: ws_state.close_code = code
            if reason: ws_state.close_reason = reason
            ws_state.status = "CLOSED"
            
            if self.storage:
                self.storage.write_websocket_state(ws_state)
            
            del self.active_websockets[request_id]

    def attach_cdp_websocket_frame(self, request_id: str, direction: str, response: dict):
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state: return
            
            self.seq_counter += 1
            seq = self.seq_counter
            
        ts = datetime.now().isoformat()
        opcode = response.get("opcode", 1)
        payload_data = response.get("payloadData", "")
        
        is_text = (opcode == 1)
        payload_type = "text" if is_text else "binary"
        
        import base64
        if is_text:
            raw_bytes = payload_data.encode('utf-8')
            payload_str = payload_data
        else:
            try:
                raw_bytes = base64.b64decode(payload_data)
            except:
                raw_bytes = payload_data.encode('utf-8')
            payload_str = None
            
        payload_size = len(raw_bytes)
        
        payload_file = None
        if config.save_response_bodies:
            if not is_text or payload_size >= 1000:
                if self.storage:
                    payload_file = self.storage.save_websocket_frame(request_id, seq, raw_bytes, is_text)
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
            payload=payload_str
        )
        
        with self.lock:
            ws_state = self.active_websockets.get(request_id)
            if not ws_state: return
            ws_state.frames.append(frame)
            if self.storage:
                self.storage.write_websocket_frame(ws_state, frame)
                self.gui_queue.put({"type": "WS_UPDATED", "stats": len(self.active_websockets)})

    def handle_request(self, request, page_id: str):
        if not self.recording: return
        key = (request.url, request.method)
        pw_id = id(request)
        
        with self.lock:
            if self.unmatched_cdp_requests[key]:
                cdp_id = self.unmatched_cdp_requests[key].pop(0)
                self.pw_to_cdp[pw_id] = cdp_id
                txn = self.active_transactions.get(cdp_id)
                if txn:
                    self._enrich_transaction_from_playwright(txn, request)
            else:
                self.unmatched_playwright_requests[key].append(request)



    def _enrich_transaction_from_playwright(self, txn: TransactionState, request):
        # Override with rich Playwright data if available
        txn.frame_url = request.frame.url if request.frame else ""
        txn.resource_type = request.resource_type
        
        # Merge headers to ensure Playwright's generated headers are included
        txn.request_headers.update(request.headers)
        
        if config.save_request_bodies:
            try:
                post_data = request.post_data
                if post_data:
                    txn.post_data = post_data
                else:
                    buffer = request.post_data_buffer
                    if buffer:
                        saved_path = self.storage.save_request_body(txn.sequence, buffer)
                        txn.post_data_file = saved_path
            except Exception as e:
                logger.error(f"Req body error: {e}")

        cookie_header = request.headers.get("cookie", "")
        if cookie_header:
            txn.request_cookies = [{"name": c.split('=', 1)[0].strip(), "value": c.split('=', 1)[1].strip() if '=' in c else ""} for c in cookie_header.split(';')]

    def handle_response(self, response):
        if not self.recording: return
        pw_id = id(response.request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            cdp_id = self.pw_to_cdp.get(pw_id)
            if not cdp_id: return
            txn = self.active_transactions.get(cdp_id)
            if not txn: return
            
            # All mutations inside the lock to prevent concurrent thread corruption
            txn.response_time = ts
            txn.status = response.status
            txn.status_text = response.status_text
            txn.response_headers = response.headers
            txn.content_type = response.headers.get("content-type", "")
        
        # Body read is blocking I/O — do outside the lock but after fields are safely set
        if config.save_response_bodies:
            try:
                body = response.body()
                if body:
                    ext = determine_extension(txn.url, txn.content_type, txn.resource_type)
                    res_info = self.storage.save_resource(txn.sequence, body, ext)
                    if res_info:
                        txn.resource = ResourceRecord(
                            file_path=res_info["file_path"],
                            size=res_info["size"],
                            sha256=res_info["sha256"],
                            mime_type=txn.content_type,
                            extension=ext
                        )
            except Exception as e:
                logger.debug(f"Response body unavailable for {txn.url}: {e}")
                
        self.gui_queue.put(GUIEvent(type="UPDATED", transaction=txn))

    def handle_request_finished(self, request):
        if not self.recording: return
        pw_id = id(request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            cdp_id = self.pw_to_cdp.pop(pw_id, None)
            txn = self.active_transactions.pop(cdp_id, None) if cdp_id else None
            
            if not txn:
                # Fallback for requests missed by CDP
                key = (request.url, request.method)
                if request in self.unmatched_playwright_requests[key]:
                    self.unmatched_playwright_requests[key].remove(request)
                
                self.seq_counter += 1
                seq = self.seq_counter
                txn = TransactionState(
                    id=f"pw-{pw_id}",
                    sequence=seq,
                    url=request.url,
                    method=request.method,
                    request_headers=request.headers,
                    request_time=ts,
                    resource_type=request.resource_type,
                    page_id="",
                    frame_url=request.frame.url if request.frame else ""
                )

                self._enrich_transaction_from_playwright(txn, request)
                txn.response_time = ts
                
                # simulate response
                try:
                    resp = request.response()
                    if resp:
                        txn.status = resp.status
                        txn.status_text = resp.status_text
                        txn.response_headers = resp.headers
                        txn.content_type = resp.headers.get("content-type", "")
                        if config.save_response_bodies:
                            body = resp.body()
                            if body:
                                ext = determine_extension(txn.url, txn.content_type, txn.resource_type)
                                res_info = self.storage.save_resource(txn.sequence, body, ext)
                                if res_info:
                                    txn.resource = ResourceRecord(
                                        file_path=res_info["file_path"], size=res_info["size"],
                                        sha256=res_info["sha256"], mime_type=txn.content_type, extension=ext
                                    )
                except:
                    pass

        txn.completed = True
        txn.completion_time = ts
        if not txn.error and not txn.resource and config.save_response_bodies and txn.status not in (204, 301, 302, 303, 304, 307, 308):
            txn.error = "Body unavailable"
        self.storage.finalize_transaction(txn)
        self.gui_queue.put(GUIEvent(type="COMPLETED", transaction=txn))

            
    def handle_request_failed(self, request):
        if not self.recording: return
        pw_id = id(request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            cdp_id = self.pw_to_cdp.pop(pw_id, None)
            txn = self.active_transactions.pop(cdp_id, None) if cdp_id else None
            
            if not txn:
                key = (request.url, request.method)
                if request in self.unmatched_playwright_requests[key]:
                    self.unmatched_playwright_requests[key].remove(request)
                self.seq_counter += 1
                txn = TransactionState(
                    id=f"pw-{pw_id}", sequence=self.seq_counter,
                    url=request.url, method=request.method,
                    request_headers=request.headers, request_time=ts,
                    resource_type=request.resource_type, page_id="",
                    frame_url=request.frame.url if request.frame else ""
                )

                self._enrich_transaction_from_playwright(txn, request)
                
        txn.completed = True
        txn.completion_time = ts
        txn.error = request.failure
        self.storage.finalize_transaction(txn)
        self.gui_queue.put(GUIEvent(type="FAILED", transaction=txn))
        
        if request.is_navigation_request():
            # Create a failed navigation record
            from models import NavigationRecord
            frame_id = "main" if request.frame == request.frame.page.main_frame else (request.frame.name or f"frame-{id(request.frame)}")
            nav_id = f"nav_fail_{self.seq_counter:06d}"
            
            nav_record = NavigationRecord(
                navigation_id=nav_id,
                page_id=txn.page_id,
                frame_id=frame_id,
                timestamp=ts,
                from_url=None, # Cannot easily get from here, but this is a failure
                to_url=request.url,
                type="document_navigation",
                reason="Navigation",
                status=None,
                success=False,
                document_request_sequence=txn.sequence,
                dom_snapshot_id=None,
                error=request.failure
            )
            self.storage.save_navigation_record(nav_record)

