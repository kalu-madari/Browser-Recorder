import time
import os
import shutil
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from network_recorder import NetworkRecorder
from browser_manager import BrowserManager
from storage import StorageManager
from config import config

config.enable_dom_snapshots = True
config.enable_interactions = True
config.output_dir = "test_cross_out"
config.user_data_dir = "test_cross_user"

PORT_MAIN = 9200
PORT_CROSS = 9201

HTML_MAIN = b'''
<!DOCTYPE html>
<html>
<body>
    <div id="main-div">Main</div>
    <iframe src="http://127.0.0.1:9201/" id="cross-frame"></iframe>
</body>
</html>
'''

HTML_CROSS = b'''
<!DOCTYPE html>
<html>
<body>
    <button id="cross-btn">Cross Button</button>
</body>
</html>
'''

class TestServerMain(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(HTML_MAIN)
    def log_message(self, *args): pass

class TestServerCross(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(HTML_CROSS)
    def log_message(self, *args): pass

def run_server(port, handler):
    server = HTTPServer(("127.0.0.1", port), handler)
    server.serve_forever()

def main():
    threading.Thread(target=run_server, args=(PORT_MAIN, TestServerMain), daemon=True).start()
    threading.Thread(target=run_server, args=(PORT_CROSS, TestServerCross), daemon=True).start()
    
    if os.path.exists(config.output_dir):
        shutil.rmtree(config.output_dir)
        
    storage = StorageManager(config.output_dir)
    import queue
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    
    import sys
    sys.argv = []
    
    bm = BrowserManager(recorder, gui_q, f"http://127.0.0.1:{PORT_MAIN}/")
    bm.browser = None
    
    recorder.start_recording()
    bm.start()

    
    time.sleep(3) # Wait for load
    
    # Send interaction to cross origin frame
    # We find the canonical frame ID

    
    # We must evaluate it directly using playwright API because our command queue 
    # evaluate goes to main page.
    bm.command_queue.put({"action": "evaluate", "frame_url": "9201", "js": "document.getElementById('cross-btn').click();"})
                
    time.sleep(2)
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)
    
    cap_dir = storage.capture_dir
    with open(os.path.join(cap_dir, 'interaction.jsonl')) as f:
        interactions = [json.loads(l) for l in f]
        
    print(f"Captured {len(interactions)} interactions.")
    for i in interactions:
        print(f"Frame ID: {i['frame_id']} | Target: {i['target_id']} | Tag: {i['target_tag']}")
        if i['frame_id'] == "unknown":
            print("CROSS ORIGIN MATCH SUCCESS!")

if __name__ == '__main__':
    main()
