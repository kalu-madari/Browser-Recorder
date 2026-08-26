import os
import json
import hashlib
import threading
from datetime import datetime
from models import TransactionState, DOMSnapshotRecord, InteractionRecord
import logging
from config import config

logger = logging.getLogger(__name__)

class StorageManager:
    def __init__(self, base_dir: str):
        self.session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.capture_dir = os.path.join(base_dir, self.session_id)
        self.resources_dir = os.path.join(self.capture_dir, "resources")
        self.request_bodies_dir = os.path.join(self.capture_dir, "request_bodies")
        self.websocket_dir = os.path.join(self.capture_dir, "websocket")
        
        os.makedirs(self.capture_dir, exist_ok=True)
        os.makedirs(self.resources_dir, exist_ok=True)
        os.makedirs(self.request_bodies_dir, exist_ok=True)
        os.makedirs(self.websocket_dir, exist_ok=True)
        
        self.dom_dir = os.path.join(self.capture_dir, "dom")
        os.makedirs(self.dom_dir, exist_ok=True)

        
        self.network_log_path = os.path.join(self.capture_dir, "network_log.txt")
        self.manifest_path = os.path.join(self.capture_dir, "manifest.jsonl")
        self.session_path = os.path.join(self.capture_dir, "session.json")
        self.websocket_log_path = os.path.join(self.capture_dir, "websocket_log.txt")
        self.websocket_jsonl_path = os.path.join(self.capture_dir, "websocket.jsonl")
        self.dom_manifest_path = os.path.join(self.capture_dir, "dom.jsonl")
        
        self.navigation_manifest_path = os.path.join(self.capture_dir, "navigation.jsonl")
        self.navigation_log_path = os.path.join(self.capture_dir, "navigation_log.txt")
        
        self.interaction_manifest_path = os.path.join(self.capture_dir, "interaction.jsonl")
        self.interaction_log_path = os.path.join(self.capture_dir, "interaction_log.txt")
        
        self.lock = threading.RLock()
        
        # Buffer for completed transactions to ensure chronological logging based on sequence
        self.completed_txns = {}
        self.next_seq_to_write = 1
        
        self.dom_sequence = {} # (page_id, frame_id) -> int

        
        self.stats = {
            "requests": 0, "responses": 0, "failed": 0,
            "html": 0, "js": 0, "css": 0, "json": 0, "images": 0, "fonts": 0, "media": 0, "other": 0,
            "bytes_saved": 0
        }
        self.final_cookies = []
        
        self.session_start = datetime.now()
        self.init_files()
        
    def init_files(self):
        with open(self.network_log_path, "w", encoding="utf-8") as f:
            f.write(f"SESSION START: {self.session_id}\n")
        
        # Initialize an empty manifest jsonl file
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            pass
            
        with open(self.dom_manifest_path, "w", encoding="utf-8") as f:
            pass
            
        with open(self.navigation_manifest_path, "w", encoding="utf-8") as f:
            pass
            
        with open(self.navigation_log_path, "w", encoding="utf-8") as f:
            f.write(f"NAVIGATION HISTORY: {self.session_id}\n")
            
        with open(self.interaction_manifest_path, "w", encoding="utf-8") as f:
            pass
            
        with open(self.interaction_log_path, "w", encoding="utf-8") as f:
            f.write(f"INTERACTION LOG: {self.session_id}\n")
            
        self.write_session_info()
        
    def save_resource(self, seq: int, body: bytes, ext: str) -> dict:
        if len(body) > config.max_resource_size:
            logger.warning(f"Resource {seq} exceeded maximum size: {len(body)} bytes")
            return None
            
        filename = f"{seq:06d}{ext}"
        filepath = os.path.join(self.resources_dir, filename)
        try:
            with open(filepath, "wb") as f:
                f.write(body)
            sha256 = hashlib.sha256(body).hexdigest()
            size = len(body)
            return {"file_path": f"resources/{filename}", "size": size, "sha256": sha256}
        except Exception as e:
            logger.error(f"Failed to save resource {filename}: {e}")
            return None

    def save_request_body(self, seq: int, body: bytes) -> str:
        filename = f"{seq:06d}_request.bin"
        filepath = os.path.join(self.request_bodies_dir, filename)
        try:
            with open(filepath, "wb") as f:
                f.write(body)
            return f"request_bodies/{filename}"
        except Exception as e:
            logger.error(f"Failed to save request body {filename}: {e}")
            return None

    def get_next_dom_snapshot_id(self, page_id, frame_id):
        with self.lock:
            key = (page_id, frame_id)
            if key not in self.dom_sequence:
                self.dom_sequence[key] = 1
            seq = self.dom_sequence[key]
            self.dom_sequence[key] += 1
            return f"{seq:06d}"

    def save_dom_snapshot(self, record: 'DOMSnapshotRecord', html_bytes: bytes) -> bool:
        with self.lock:
            if not record.snapshot_id:
                record.snapshot_id = self.get_next_dom_snapshot_id(record.page_id, record.frame_id)
            seq = int(record.snapshot_id)

            if len(html_bytes) > config.max_dom_snapshot_size:
                logger.warning(f"DOM snapshot exceeded max size: {len(html_bytes)} bytes")
                record.truncated = True
                html_bytes = b""
            
            if not record.truncated:
                page_dir = os.path.join(self.dom_dir, record.page_id)
                if record.frame_id:
                    page_dir = os.path.join(page_dir, record.frame_id)
                os.makedirs(page_dir, exist_ok=True)
                    
                filename = f"{seq:06d}.html"
                filepath = os.path.join(page_dir, filename)
                
                try:
                    with open(filepath, "wb") as f:
                        f.write(html_bytes)
                    record.html_path = os.path.relpath(filepath, self.capture_dir).replace('\\', '/')
                    record.html_sha256 = hashlib.sha256(html_bytes).hexdigest()
                    record.html_size = len(html_bytes)
                except Exception as e:
                    logger.error(f"Failed to save DOM snapshot {filename}: {e}")
                    return False
            else:
                record.html_path = ""
                record.html_sha256 = ""
                record.html_size = 0
            
            try:
                with open(self.dom_manifest_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record.to_dict()) + "\n")
            except Exception as e:
                logger.error(f"DOM Manifest write error: {e}")
                return False
                
        return True

    def save_navigation_record(self, record):
        with self.lock:
            try:
                with open(self.navigation_manifest_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record.to_dict()) + "\n")
                    
                with open(self.navigation_log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"NAVIGATION {record.navigation_id}\n")
                    f.write(f"{'='*60}\n\n")
                    f.write(f"PAGE: {record.page_id}\n")
                    f.write(f"FRAME: {record.frame_id}\n")
                    f.write(f"TIME: {record.timestamp}\n\n")
                    f.write(f"FROM:\n{record.from_url if record.from_url else '-'}\n\n")
                    f.write(f"TO:\n{record.to_url}\n\n")
                    f.write(f"TYPE:\n{record.type}\n\n")
                    f.write(f"REASON:\n{record.reason}\n\n")
                    f.write(f"STATUS:\n{record.status if record.status is not None else '-'}\n\n")
                    f.write(f"SUCCESS:\n{str(record.success).lower()}\n\n")
                    if record.error:
                        f.write(f"ERROR:\n{record.error}\n\n")
                    if record.document_request_sequence is not None:
                        f.write(f"DOCUMENT REQUEST:\n{record.document_request_sequence:06d}\n\n")
                    if record.dom_snapshot_id:
                        f.write(f"DOM SNAPSHOT:\n{record.dom_snapshot_id}\n\n")
                    f.write(f"{'-'*60}\n")
            except Exception as e:
                logger.error(f"Navigation Manifest write error: {e}")
                return False
        return True

    def save_interaction_record(self, record):
        with self.lock:
            try:
                with open(self.interaction_manifest_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record.to_dict()) + "\n")
                    
                with open(self.interaction_log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"INTERACTION {record.interaction_id}\n")
                    f.write(f"{'='*60}\n\n")
                    f.write(f"PAGE: {record.page_id}\n")
                    f.write(f"FRAME: {record.frame_id}\n")
                    f.write(f"TIME: {record.timestamp}\n")
                    f.write(f"TYPE: {record.event_type}\n")
                    f.write(f"TARGET TAG: {record.target_tag}\n")
                    f.write(f"TARGET SELECTOR: {record.target_selector}\n")
                    if record.target_text:
                        f.write(f"TARGET TEXT: {record.target_text}\n")
                    if record.value_recorded and record.target_value is not None:
                        f.write(f"TARGET VALUE: {record.target_value}\n")
                    if record.coordinates:
                        f.write(f"COORDINATES: x={record.coordinates['x']}, y={record.coordinates['y']}\n")
                    if record.key:
                        f.write(f"KEY: {record.key}\n")
                    f.write(f"DOM SNAPSHOT: {record.dom_snapshot_id or 'none'}\n")
                    f.write(f"NAVIGATION: {record.navigation_id or 'none'}\n")
                    f.write(f"IS TRUSTED: {str(record.is_trusted).lower()}\n")
                    f.write(f"{'-'*60}\n")
            except Exception as e:
                logger.error(f"Interaction Manifest write error: {e}")
                return False
        return True

    def _update_stats(self, txn: TransactionState):
        self.stats["requests"] += 1
        if txn.error:
            self.stats["failed"] += 1
        elif txn.response_time:
            self.stats["responses"] += 1
            if txn.resource:
                self.stats["bytes_saved"] += txn.resource.size
                ext = txn.resource.extension
                if ext in ['.html', '.htm']: self.stats["html"] += 1
                elif ext == '.js': self.stats["js"] += 1
                elif ext == '.css': self.stats["css"] += 1
                elif ext in ['.json', '.webmanifest']: self.stats["json"] += 1
                elif ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico']: self.stats["images"] += 1
                elif ext in ['.woff', '.woff2', '.ttf', '.otf', '.eot', '.ttc']: self.stats["fonts"] += 1
                elif ext in ['.mp4', '.mp3', '.webm', '.ogg']: self.stats["media"] += 1
                else: self.stats["other"] += 1
            
    def finalize_transaction(self, txn: TransactionState):
        with self.lock:
            self._update_stats(txn)
            self.completed_txns[txn.sequence] = txn
            self._drain_completed()
            
    def _drain_completed(self):
        # We write completed transactions exactly in their request sequence order.
        while self.next_seq_to_write in self.completed_txns:
            txn = self.completed_txns.pop(self.next_seq_to_write)
            self._write_to_log(txn)
            self._write_to_manifest(txn)
            self.next_seq_to_write += 1

    def _write_to_manifest(self, txn: TransactionState):
        try:
            with open(self.manifest_path, "a", encoding="utf-8") as f:
                json_str = json.dumps(txn.to_dict())
                f.write(json_str + "\n")
        except Exception as e:
            logger.error(f"Manifest write error: {e}")

    def _write_to_log(self, txn: TransactionState):
        try:
            with open(self.network_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[{txn.sequence:06d}]\n")
                f.write(f"REQUEST TIME: {txn.request_time}\n")
                if txn.completion_time:
                    f.write(f"COMPLETION TIME: {txn.completion_time}\n")
                f.write(f"TYPE: {txn.resource_type}\n")
                f.write(f"METHOD: {txn.method}\n")
                f.write(f"URL: {txn.url}\n")
                
                # Initiator
                if txn.initiator:
                    f.write(f"{'-'*60}\nINITIATOR\n")
                    f.write(json.dumps(txn.initiator, indent=2) + "\n")
                
                # Request headers
                f.write(f"{'-'*60}\nREQUEST HEADERS\n")
                for k, v in txn.request_headers.items():
                    f.write(f"{k}: {v}\n")
                    
                # Request cookies
                if txn.request_cookies:
                    f.write(f"{'-'*60}\nREQUEST COOKIES\n")
                    for c in txn.request_cookies:
                        f.write(f"{c}\n")
                        
                # Request body
                if txn.post_data:
                    f.write(f"{'-'*60}\nREQUEST BODY\n{txn.post_data}\n")
                elif txn.post_data_file:
                    f.write(f"{'-'*60}\nREQUEST BODY SAVED: {txn.post_data_file}\n")
                    
                # Response
                if txn.error:
                    f.write(f"{'-'*60}\nFAILED\n")
                    f.write(f"ERROR: {txn.error}\n")
                else:
                    f.write(f"{'-'*60}\nRESPONSE\n")
                    f.write(f"STATUS: {txn.status} {txn.status_text or ''}\n")
                    f.write(f"RESPONSE TIME: {txn.response_time}\n")
                    
                    f.write(f"{'-'*60}\nRESPONSE HEADERS\n")
                    for k, v in txn.response_headers.items():
                        f.write(f"{k}: {v}\n")
                        
                    if txn.resource:
                        f.write(f"{'-'*60}\nBODY\n")
                        f.write(f"Saved to: {txn.resource.file_path}\n")
                        f.write(f"Size: {txn.resource.size} bytes\n")
                        f.write(f"SHA256: {txn.resource.sha256}\n")
                    else:
                        f.write(f"{'-'*60}\nBODY\n")
                        f.write("NOT SAVED - body unavailable or exceeded maximum configured size\n")
                        
                f.write(f"{'='*60}\n")
        except Exception as e:
            logger.error(f"Log write error: {e}")
            
    def write_session_info(self):
        try:
            temp_path = self.session_path + ".tmp"
            data = {
                "session_id": self.session_id,
                "start_time": self.session_start.isoformat(),
                "end_time": datetime.now().isoformat(),
                "browser": "Chromium",
                "playwright": True,
                "stats": self.stats,
                "context_cookies": self.final_cookies
            }
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.session_path)
        except Exception as e:
            logger.error(f"Session info write error: {e}")

    def save_websocket_frame(self, ws_id: str, sequence: int, payload: bytes, is_text: bool) -> str:
        ws_dir = os.path.join(self.websocket_dir, ws_id)
        os.makedirs(ws_dir, exist_ok=True)
        ext = ".txt" if is_text else ".bin"
        filename = f"{sequence:06d}{ext}"
        filepath = os.path.join(ws_dir, filename)
        try:
            with open(filepath, "wb") as f:
                f.write(payload)
            return f"websocket/{ws_id}/{filename}"
        except Exception as e:
            logger.error(f"Failed to save WS frame {filename}: {e}")
            return None

    def write_websocket_state(self, ws_state):
        with self.lock:
            self._write_websocket_to_manifest(ws_state)
            self._write_websocket_to_log(ws_state)

    def write_websocket_frame(self, ws_state, frame):
        with self.lock:
            # Append frame to the jsonl and log incrementally
            self._write_websocket_to_manifest(ws_state, frame=frame)
            self._write_websocket_frame_to_log(ws_state, frame)

    def _write_websocket_to_manifest(self, ws_state, frame=None):
        try:
            with open(self.websocket_jsonl_path, "a", encoding="utf-8") as f:
                if frame is None:
                    # Connection info
                    d = ws_state.to_dict()
                    d["type"] = "connection"
                    d.pop("frames", None)
                    f.write(json.dumps(d) + "\n")
                else:
                    # Frame info
                    d = frame.to_dict()
                    d["type"] = "frame"
                    f.write(json.dumps(d) + "\n")
        except Exception as e:
            logger.error(f"WS Manifest write error: {e}")

    def _write_websocket_to_log(self, ws_state):
        try:
            with open(self.websocket_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"WEBSOCKET\n")
                f.write(f"{'='*60}\n")
                f.write(f"ID: {ws_state.id}\n")
                f.write(f"PAGE: {ws_state.page_id}\n")
                f.write(f"URL: {ws_state.url}\n")
                f.write(f"CREATED: {ws_state.created_time}\n")
                f.write(f"{'-'*60}\nHANDSHAKE\n")
                if ws_state.handshake_status:
                    f.write(f"STATUS: {ws_state.handshake_status} {ws_state.handshake_status_text}\n")
                
                if ws_state.status != "OPEN":
                    f.write(f"{'-'*60}\nCLOSED\n")
                    f.write(f"TIME: {ws_state.closed_time}\n")
                    if ws_state.close_code:
                        f.write(f"CODE: {ws_state.close_code}\n")
                        f.write(f"REASON: {ws_state.close_reason}\n")
                    if ws_state.status != "CLOSED":
                        f.write(f"STATUS: {ws_state.status}\n")
        except Exception as e:
            logger.error(f"WS Log write error: {e}")

    def _write_websocket_frame_to_log(self, ws_state, frame):
        try:
            with open(self.websocket_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'-'*60}\n")
                f.write(f"FRAME {frame.sequence:06d} (WS: {frame.websocket_id})\n")
                f.write(f"TIME: {frame.timestamp}\n")
                f.write(f"DIRECTION: {frame.direction}\n")
                f.write(f"TYPE: {frame.payload_type.upper()}\n")
                f.write(f"SIZE: {frame.payload_size}\n")
                f.write("PAYLOAD:\n")
                if frame.payload:
                    f.write(frame.payload[:1000] + ("...\n" if len(frame.payload)>1000 else "\n"))
                elif frame.payload_file:
                    f.write(f"Saved to: {frame.payload_file}\n")
        except Exception as e:
            logger.error(f"WS Frame Log write error: {e}")

    def finalize(self):
        with self.lock:
            # Drain any remaining completed or force-flush stalled out-of-order ones
            keys = sorted(list(self.completed_txns.keys()))
            for k in keys:
                txn = self.completed_txns.pop(k)
                self._write_to_log(txn)
                self._write_to_manifest(txn)
                
            self.write_session_info()
            self._write_session_summary()

    def _write_session_summary(self):
        duration = datetime.now() - self.session_start
        hours, remainder = divmod(duration.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        dur_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        try:
            with open(self.network_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"SESSION SUMMARY\n")
                f.write(f"{'='*60}\n\n")
                f.write(f"Session:\n{self.session_id}\n\n")
                f.write(f"Duration:\n{dur_str}\n\n")
                f.write(f"Requests:\n{self.stats['requests']}\n\n")
                f.write(f"Responses:\n{self.stats['responses']}\n\n")
                f.write(f"Failed:\n{self.stats['failed']}\n\n")
                
                total_saved = sum([self.stats['html'], self.stats['js'], self.stats['css'], 
                                   self.stats['json'], self.stats['images'], self.stats['fonts'], 
                                   self.stats['media'], self.stats['other']])
                f.write(f"Resources Saved:\n{total_saved}\n\n")
                f.write(f"Bytes Saved:\n{self.stats['bytes_saved'] / (1024*1024):.1f} MB\n\n")
                f.write(f"HTML:\n{self.stats['html']}\n\n")
                f.write(f"JavaScript:\n{self.stats['js']}\n\n")
                f.write(f"CSS:\n{self.stats['css']}\n\n")
                f.write(f"JSON/API:\n{self.stats['json']}\n\n")
                f.write(f"Images:\n{self.stats['images']}\n\n")
                f.write(f"Fonts:\n{self.stats['fonts']}\n\n")
                f.write(f"Other:\n{self.stats['other']}\n\n")
                f.write(f"{'='*60}\n")
        except Exception as e:
            logger.error(f"Summary write error: {e}")
