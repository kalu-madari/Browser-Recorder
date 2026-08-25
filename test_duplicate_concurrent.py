import os
import json
import time
import shutil
import queue
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

import sys
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from browser_manager import BrowserManager
from network_recorder import NetworkRecorder
from storage import StorageManager
from config import config

class TestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <body>
                    <script>
                        function initiatorA() { fetch('/duplicate?id=A'); }
                        function initiatorB() { fetch('/duplicate?id=B'); }
                        function initiatorC() { fetch('/duplicate?id=C'); }
                        function initiatorD() { fetch('/duplicate?id=D'); }
                        function initiatorE() { fetch('/duplicate?id=E'); }
                        
                        function initiator20(i) { fetch('/duplicate20', {headers: {'X-Test-Id': i.toString()}}); }
                        
                        setTimeout(() => {
                            // Simultaneous firing!
                            initiatorA();
                            initiatorB();
                            initiatorC();
                            initiatorD();
                            initiatorE();
                            
                            for(let i=0; i<20; i++) {
                                initiator20(i);
                            }
                        }, 500);
                    </script>
                </body>
                </html>
            """)
        elif self.path.startswith('/duplicate?id='):
            delay = {'A': 0.5, 'B': 0.05, 'C': 1.0, 'D': 0.01, 'E': 0.25}.get(self.path.split('=')[-1], 0)
            time.sleep(delay)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == '/duplicate20':
            import random
            time.sleep(random.uniform(0.01, 0.2))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    server = HTTPServer(('127.0.0.1', 8081), TestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

def run_test():
    server = run_server()
    time.sleep(1)
    
    config.output_dir = "test_captures_concurrent"
    if os.path.exists("test_captures_concurrent"):
        shutil.rmtree("test_captures_concurrent")
        
    storage = StorageManager(config.output_dir)
    gui_queue = queue.Queue()
    gui_msg_queue = queue.Queue()
    recorder = NetworkRecorder(storage, queue.Queue())
    
    recorder.start_recording()
    bm = BrowserManager(recorder, gui_msg_queue, start_url="http://127.0.0.1:8081/")
    
    bm.start()
    print("Waiting for browser ready...")
    ready = False
    for _ in range(60):
        try:
            msg = gui_msg_queue.get_nowait()
            print(f"GUI Queue msg: {msg}")
            if msg["type"] == "STATE" and msg["state"] == "READY":
                ready = True
                break
        except queue.Empty:
            time.sleep(0.5)
            
    assert ready, "Browser did not start"
    
    print("Waiting 3s for requests...")
    time.sleep(3)
    print("Stopping recording...")
    recorder.stop_recording()
    print("Stopping browser manager...")
    bm.stop()
    bm.join(timeout=5)
    
    print("Reading outputs...")
    cap_dir = storage.capture_dir
    manifest_path = os.path.join(cap_dir, "manifest.jsonl")
    
    with open(manifest_path, 'r') as f:
        txns = [json.loads(l) for l in f.read().splitlines()]
        
    # Verify 5 duplicates
    dups = [t for t in txns if '/duplicate?id=' in t['url']]
    assert len(dups) == 5, f"Expected 5 duplicates, got {len(dups)}"
    
    for t in dups:
        url_id = t['url'].split('=')[-1]
        stack = str(t.get('initiator', {}))
        assert f"initiator{url_id}" in stack, f"Initiator mismatch for {url_id}: {stack}"
        
    # Verify 20 identical duplicates
    dups20 = [t for t in txns if '/duplicate20' in t['url']]
    assert len(dups20) == 20, f"Expected 20 duplicates, got {len(dups20)}"
    
    ids_found = set()
    for t in dups20:
        req_headers = t['request_headers']
        # Playwright headers are lowercase
        x_test_id = req_headers.get('x-test-id')
        assert x_test_id is not None, "Missing X-Test-Id header"
        ids_found.add(x_test_id)
        
    assert len(ids_found) == 20, f"Expected 20 unique IDs, got {len(ids_found)}"
    
    print("Concurrent requests test passed!")

if __name__ == '__main__':
    run_test()
