import queue
import logging
import threading
from datetime import datetime
from models import TransactionState, ResourceRecord, GUIEvent
from resource_classifier import determine_extension
from storage import StorageManager
from config import config

logger = logging.getLogger(__name__)

class NetworkRecorder:
    def __init__(self, storage: StorageManager, gui_queue: queue.Queue):
        self.storage = storage
        self.gui_queue = gui_queue
        self.seq_counter = 0
        self.lock = threading.Lock()
        self.recording = False
        
        # Authoritative mapping: CDP requestId -> TransactionState
        self.active_transactions = {}
        
        # Bridging maps
        self.pw_to_cdp = {} # Playwright id(request) -> CDP requestId
        
        import collections
        # FIFO queues used exclusively as a bridge, because Playwright Python 
        # does not expose the underlying CDP requestId on the Request object.
        # LIMITATION: This relies on the strict sequential emission order of Chromium events.
        self.unmatched_playwright_requests = collections.defaultdict(list)
        self.unmatched_cdp_requests = collections.defaultdict(list)
        
    def get_next_seq(self):
        with self.lock:
            self.seq_counter += 1
            return self.seq_counter
            
    def start_recording(self):
        self.recording = True
        
    def stop_recording(self):
        self.recording = False
        
        # Wait for active transactions to settle naturally
        import time
        for _ in range(50):
            with self.lock:
                if not self.active_transactions:
                    break
            time.sleep(0.1)
            
        if self.storage:
            with self.lock:
                for req_id, txn in list(self.active_transactions.items()):
                    txn.completed = True
                    txn.error = "INCOMPLETE (Recording stopped)"
                    self.storage.finalize_transaction(txn)
                self.active_transactions.clear()
            self.storage.finalize()

    def attach_cdp_request(self, url: str, method: str, request_id: str, initiator: dict, event: dict, page_id: str):
        if not self.recording: return
        key = (url, method)
        
        with self.lock:
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
            
            # 2. Bridge to Playwright
            if self.unmatched_playwright_requests[key]:
                # A Playwright request already arrived and is waiting for CDP
                pw_req = self.unmatched_playwright_requests[key].pop(0)
                pw_id = id(pw_req)
                self.pw_to_cdp[pw_id] = request_id
                self._enrich_transaction_from_playwright(txn, pw_req)
            else:
                # Store CDP request ID waiting for Playwright request
                self.unmatched_cdp_requests[key].append(request_id)
                
            self.gui_queue.put(GUIEvent(type="STARTED", transaction=txn))

    def handle_request(self, request, page_id: str):
        if not self.recording: return
        key = (request.url, request.method)
        pw_id = id(request)
        
        with self.lock:
            if self.unmatched_cdp_requests[key]:
                # A CDP request already arrived
                cdp_id = self.unmatched_cdp_requests[key].pop(0)
                self.pw_to_cdp[pw_id] = cdp_id
                txn = self.active_transactions.get(cdp_id)
                if txn:
                    self._enrich_transaction_from_playwright(txn, request)
            else:
                # Playwright request arrived before CDP
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
            
        txn.response_time = ts
        txn.status = response.status
        txn.status_text = response.status_text
        txn.response_headers = response.headers
        txn.content_type = response.headers.get("content-type", "")
        
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
            if not cdp_id: return
            txn = self.active_transactions.pop(cdp_id, None)
            
        if txn:
            txn.completed = True
            txn.completion_time = ts
            if not txn.error and not txn.resource and config.save_response_bodies and txn.status not in (204, 301, 302, 303, 304, 307, 308):
                # Request finished successfully but body capture failed/unavailable
                txn.error = "Body unavailable"
            self.storage.finalize_transaction(txn)
            self.gui_queue.put(GUIEvent(type="COMPLETED", transaction=txn))
            
    def handle_request_failed(self, request):
        if not self.recording: return
        pw_id = id(request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            cdp_id = self.pw_to_cdp.pop(pw_id, None)
            if not cdp_id: return
            txn = self.active_transactions.pop(cdp_id, None)
            
        if txn:
            txn.completed = True
            txn.completion_time = ts
            txn.error = request.failure
            self.storage.finalize_transaction(txn)
            self.gui_queue.put(GUIEvent(type="FAILED", transaction=txn))
