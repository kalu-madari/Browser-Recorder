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
        self.active_transactions = {}
        import collections
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
        if self.storage:
            with self.lock:
                for req_id, txn in self.active_transactions.items():
                    txn.completed = True
                    txn.error = "INCOMPLETE (Recording stopped)"
                    self.storage.finalize_transaction(txn)
                self.active_transactions.clear()
            self.storage.finalize()

    def handle_request(self, request, page_id: str):
        if not self.recording: return
        
        req_id = id(request)
        seq = self.get_next_seq()
        ts = datetime.now().isoformat()
        
        txn = TransactionState(
            id=req_id,
            sequence=seq,
            url=request.url,
            method=request.method,
            request_headers=request.headers,
            request_time=ts,
            resource_type=request.resource_type,
            page_id=page_id,
            frame_url=request.frame.url if request.frame else ""
        )
        
        if config.save_request_bodies:
            try:
                post_data = request.post_data
                if post_data:
                    txn.post_data = post_data
                else:
                    buffer = request.post_data_buffer
                    if buffer:
                        saved_path = self.storage.save_request_body(seq, buffer)
                        txn.post_data_file = saved_path
            except Exception as e:
                logger.error(f"Req body error: {e}")

        # Try to parse cookies from headers roughly if context cookies aren't synced yet
        cookie_header = request.headers.get("cookie", "")
        if cookie_header:
            txn.request_cookies = [{"name": c.split('=')[0].strip(), "value": c.split('=')[1].strip() if '=' in c else ""} for c in cookie_header.split(';')]

        key = (request.url, request.method)
        with self.lock:
            self.active_transactions[req_id] = txn
            if self.unmatched_cdp_requests[key]:
                txn.initiator = self.unmatched_cdp_requests[key].pop(0)
            else:
                self.unmatched_playwright_requests[key].append(txn)

        self.gui_queue.put(GUIEvent(type="STARTED", transaction=txn))
        
    def handle_response(self, response):
        if not self.recording: return
        
        req_id = id(response.request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            txn = self.active_transactions.get(req_id)
            
        if not txn:
            return # Not tracked, maybe started before recording
            
        txn.response_time = ts
        txn.status = response.status
        txn.status_text = response.status_text
        txn.response_headers = response.headers
        txn.content_type = response.headers.get("content-type", "")
        
        if config.save_response_bodies:
            try:
                # This can block, Playwright allows it in sync_api but better to be safe
                body = response.body()
                if body:
                    ext = determine_extension(txn.url, txn.content_type, txn.resource_type)
                    # Note we use the transaction sequence here!
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
        
        req_id = id(request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            txn = self.active_transactions.pop(req_id, None)
            
        if txn:
            txn.completed = True
            txn.completion_time = ts
            self.storage.finalize_transaction(txn)
            self.gui_queue.put(GUIEvent(type="COMPLETED", transaction=txn))
            
    def handle_request_failed(self, request):
        if not self.recording: return
        
        req_id = id(request)
        ts = datetime.now().isoformat()
        
        with self.lock:
            txn = self.active_transactions.pop(req_id, None)
            
        if txn:
            txn.completed = True
            txn.completion_time = ts
            txn.error = request.failure
            self.storage.finalize_transaction(txn)
            self.gui_queue.put(GUIEvent(type="FAILED", transaction=txn))

    def attach_cdp_request(self, url: str, method: str, request_id: str, initiator: dict):
        key = (url, method)
        with self.lock:
            if self.unmatched_playwright_requests[key]:
                txn = self.unmatched_playwright_requests[key].pop(0)
                txn.initiator = initiator
                # Could optionally store request_id in txn as well
            else:
                self.unmatched_cdp_requests[key].append(initiator)
