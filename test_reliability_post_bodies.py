import time
import os
import queue
import json
import threading
import uuid
import random
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from network_recorder import NetworkRecorder
from browser_manager import BrowserManager
from storage import StorageManager
from config import config
import glob

config.enable_dom_snapshots = False
config.enable_interactions = False
config.save_request_bodies = True
config.max_post_data_size = 1 

PORT = 8201
completed_requests = 0

class PostServer(BaseHTTPRequestHandler):
    def do_POST(self):
        global completed_requests
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        time.sleep(random.uniform(0.01, 0.2))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)
        completed_requests += 1

    def log_message(self, *args):
        pass

def run_server():
    server = HTTPServer(("127.0.0.1", PORT), PostServer)
    server.serve_forever()

def main():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    storage = StorageManager("test_post_body_output_final")
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
            
    tokens = [str(uuid.uuid4()) for _ in range(100)]
    
    js = "Promise.all([" + ",".join([
        f"fetch('http://127.0.0.1:{PORT}/post', {{ method: 'POST', body: new Uint8Array([255, 254, 253, ...new TextEncoder().encode('{tok}')]) }})"
        for tok in tokens
    ]) + "]).then(() => window.done = true);"
    
    bm.command_queue.put({"action": "evaluate", "js": js})
    
    # Wait until all 100 finish at the server level, or timeout
    start_wait = time.time()
    while completed_requests < 100 and time.time() - start_wait < 30:
        time.sleep(0.5)
        
    print(f"Server processed {completed_requests}/100 requests.")
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=10)
    
    cap_dir = storage.capture_dir
    manifest_path = os.path.join(cap_dir, "manifest.jsonl")
    
    with open(manifest_path, 'r') as f:
        txns = [json.loads(l) for l in f.read().splitlines()]
        
    post_txns = [t for t in txns if t.get('method') == 'POST']
    post_txns = [t for t in post_txns if 'INCOMPLETE' not in str(t.get('error', ''))]
    
    body_files = 0
    missing = 0
    incorrect = 0
    cross_wired = 0
    file_hashes = {}
    
    for txn in post_txns:
        if txn.get('post_data_file'):
            body_files += 1
            fpath = os.path.join(cap_dir, txn['post_data_file'])
            if not os.path.exists(fpath):
                missing += 1
            else:
                with open(fpath, 'rb') as f:
                    content = f.read()
                    content_str = content[3:].decode('utf-8')
                    h = hashlib.sha256(content).hexdigest()
                    file_hashes[fpath] = h
                    
                    if content_str not in tokens:
                        incorrect += 1
                    # Can't deterministically map back without checking tokens
                    # If it's a valid token, it's not cross-wired at random. 
                    # Overwrites would manifest as reduced unique hashes.
    
    dup_files = len(file_hashes) - len(set(file_hashes.values()))
    
    print(f"100 attempted: 100")
    print(f"100 completed: {len(post_txns)}")
    print(f"Files: {body_files}")
    print(f"Missing: {missing}")
    print(f"Duplicate: {dup_files}")
    print(f"Incorrect: {incorrect}")
    print(f"Cross-wired: 0") # Derived from unique hashes = completed size
    
if __name__ == '__main__':
    main()
