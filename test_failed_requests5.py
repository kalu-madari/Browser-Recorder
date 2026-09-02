import time
import os
import queue
import threading
import json
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from network_recorder import NetworkRecorder
from browser_manager import BrowserManager
from storage import StorageManager
from config import config

config.enable_dom_snapshots = False
config.enable_interactions = False
config.output_dir = "test_failed_requests_out"
config.user_data_dir = "test_failed_requests_user_data"

PORT = 9105

class TestServer(BaseHTTPRequestHandler):
    def do_GET(self):
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
    
    if os.path.exists(config.output_dir):
        shutil.rmtree(config.output_dir)
    if os.path.exists(config.user_data_dir):
        shutil.rmtree(config.user_data_dir, ignore_errors=True)
        
    storage = StorageManager(config.output_dir)
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    
    # Force headless for background task stability
    import sys
    sys.argv = [] # ensure no side effects
    class Dummy: pass
    
    bm = BrowserManager(recorder, gui_q, f"http://127.0.0.1:{PORT}/")
    # Patch to inject headless=True in BrowserManager (if it wasn't already)
    # Wait, we can't easily patch it unless we modify config
    
    recorder.start_recording()
    bm.start()
    
    ready = False
    for _ in range(60):
        try:
            msg = gui_q.get(timeout=0.1)
            if isinstance(msg, dict) and msg.get("type") == "STATE" and msg.get("state") == "READY":
                ready = True
                break
        except queue.Empty:
            continue
            
    assert ready, "Browser didn't start"
    time.sleep(1)
    
    # 1. Non-existent domain (DNS resolution failure)
    js = "fetch('http://this-domain-does-not-exist-at-all-12345.com/').catch(e => console.log(e));"
    bm.command_queue.put({"action": "evaluate", "js": js})
    time.sleep(1)
    
    # 2. Connection refused
    js = "fetch('http://127.0.0.1:54321/').catch(e => console.log(e));"
    bm.command_queue.put({"action": "evaluate", "js": js})
    time.sleep(1)
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)
    
    cap_dir = storage.capture_dir
    manifest_path = os.path.join(cap_dir, "manifest.jsonl")
    
    with open(manifest_path, 'r') as f:
        txns = [json.loads(l) for l in f.read().splitlines()]
        
    for t in txns:
        print(f"URL: {t['url']} | CORR: {t.get('correlation_state', 'FINALIZED')} | ERR: {t.get('error')}")
        
    dns_fails = [t for t in txns if 'this-domain-does-not' in t['url']]
    conn_fails = [t for t in txns if '54321' in t['url']]
    
    print(f"DNS Fails generated {len(dns_fails)} records.")
    print(f"Conn Fails generated {len(conn_fails)} records.")

if __name__ == '__main__':
    main()
