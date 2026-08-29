import time
import os
import queue
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from network_recorder import NetworkRecorder
from browser_manager import BrowserManager
from storage import StorageManager
from config import config

config.enable_dom_snapshots = False
config.enable_interactions = False

PORT = 8202
req_count = 0

class TestServer(BaseHTTPRequestHandler):
    def do_GET(self):
        global req_count
        req_count += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args): pass

def run_server():
    server = HTTPServer(("127.0.0.1", PORT), TestServer)
    server.serve_forever()

def main():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    storage = StorageManager("test_tx_id_output_final")
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    bm = BrowserManager(recorder, gui_q, f"http://127.0.0.1:{PORT}/")
    
    recorder.start_recording()
    bm.start()
    
    while True:
        try:
            msg = gui_q.get(timeout=0.1)
            if isinstance(msg, dict) and msg.get("type") == "STATE" and msg.get("state") == "READY":
                break
        except queue.Empty:
            continue
            
    # Send 10,000 requests in batches
    for i in range(100):
        js = f"window.w = window.open('about:blank'); setTimeout(() => {{ Promise.all(Array.from({{length: 100}}).map(() => window.w.fetch('http://127.0.0.1:{PORT}/'))).then(() => window.w.close()); }}, 50);"
        bm.command_queue.put({"action": "evaluate", "js": js})
        time.sleep(0.1)
        
    start_wait = time.time()
    while req_count < 10000 and time.time() - start_wait < 60:
        time.sleep(1)
        
    print(f"Server processed {req_count} requests.")
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=10)
    
    cap_dir = storage.capture_dir
    manifest_path = os.path.join(cap_dir, "manifest.jsonl")
    
    with open(manifest_path, 'r') as f:
        txns = [json.loads(l) for l in f.read().splitlines() if 'id' in json.loads(l)]
        
    ids = [t['id'] for t in txns]
    pw_txns = [t for t in txns if str(t['id']).startswith('pw-')]
    pw_ids = [t['id'] for t in pw_txns]
    
    unique_ids = set(ids)
    unique_pw_ids = set(pw_ids)
    
    print(f"Transactions: {len(ids)}")
    print(f"Unique IDs: {len(unique_ids)}")
    print(f"Collisions: {len(ids) - len(unique_ids)}")
    print(f"PLAYWRIGHT_ONLY total: {len(pw_txns)}")
    print(f"PLAYWRIGHT_ONLY collisions: {len(pw_txns) - len(unique_pw_ids)}")

if __name__ == '__main__':
    main()
