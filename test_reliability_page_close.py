import time
import os
import queue
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from network_recorder import NetworkRecorder
from browser_manager import BrowserManager
from storage import StorageManager
from config import config

config.enable_dom_snapshots = False
config.enable_interactions = False

PORT = 8203

class TestServer(BaseHTTPRequestHandler):
    def do_GET(self):
        if 'stall' in self.path:
            time.sleep(10)
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

    storage = StorageManager("test_page_close_isolation")
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
            
    # Open 10 pages and stall them
    for i in range(10):
        js = f"window.p{i} = window.open('about:blank'); setTimeout(() => window.p{i}.fetch('http://127.0.0.1:{PORT}/stall_{i}'), 100);"
        bm.command_queue.put({"action": "evaluate", "js": js})
        time.sleep(0.1)
        
    time.sleep(1)
    
    # Randomly close half
    for i in range(5):
        bm.command_queue.put({"action": "evaluate", "js": f"window.p{i}.close();"})
        time.sleep(0.1)
        
    time.sleep(1)
    
    # Determine surviving pages state vs closed pages state
    closed_leaks = 0
    surviving_leaks = 0
    
    with recorder.lock:
        for rec in recorder.pending_cdp.values():
            if int(rec.page_id.replace('page-', '')) <= 5: # Assuming main page is 001, popups are 002-011. p0 is 002. p0-p4 are 002-006
                if int(rec.page_id.replace('page-', '')) <= 6:
                    closed_leaks += 1
                else:
                    surviving_leaks += 1
        for rec in recorder.pending_pw.values():
            if int(rec.page_id.replace('page-', '')) <= 6:
                closed_leaks += 1
            else:
                surviving_leaks += 1
                
    print(f"Pages tested: 10")
    print(f"Closed: 5")
    print(f"State leaked: {closed_leaks}")
    print(f"Cross-page contamination (surviving state cleared?): {'Yes' if surviving_leaks == 0 else 'No (healthy)'}")
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)

if __name__ == '__main__':
    main()
