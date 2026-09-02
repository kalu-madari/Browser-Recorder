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
config.output_dir = "test_registry_stress_out"
config.user_data_dir = "test_registry_stress_data"

PORT = 9200

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
    
    storage = StorageManager(config.output_dir)
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    bm = BrowserManager(recorder, gui_q, f"http://127.0.0.1:{PORT}/")
    
    recorder.start_recording()
    bm.start()
    
    for _ in range(60):
        try:
            msg = gui_q.get(timeout=0.1)
            if isinstance(msg, dict) and msg.get("type") == "STATE" and msg.get("state") == "READY":
                break
        except queue.Empty:
            continue
            
    time.sleep(1)
    
    # Check registry before
    with recorder.frame_registry.lock:
        size_before = len(recorder.frame_registry._mapping)
        print(f"Registry size before: {size_before}")
        
    js = '''
    for (let i = 0; i < 500; i++) {
        let f = document.createElement('iframe');
        f.id = 'frame_' + i;
        document.body.appendChild(f);
        document.body.removeChild(f);
    }
    '''
    bm.command_queue.put({"action": "evaluate", "js": js})
    time.sleep(2)
    
    with recorder.frame_registry.lock:
        size_after = len(recorder.frame_registry._mapping)
        print(f"Registry size after dynamic creation/deletion: {size_after}")
        
    bm.command_queue.put({"action": "evaluate", "js": "window.open('about:blank');"})
    time.sleep(1)
    
    with recorder.frame_registry.lock:
        size_new_page = len(recorder.frame_registry._mapping)
        print(f"Registry size after new page: {size_new_page}")
        
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)

if __name__ == '__main__':
    main()
