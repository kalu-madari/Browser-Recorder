import os
import json
import time
import shutil
import queue
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading

import sys
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from browser_manager import BrowserManager
from network_recorder import NetworkRecorder
from storage import StorageManager
from config import config

class AdvTestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""<html><body>
                <iframe src='/frame'></iframe>
                <script>
                    window.open('/popup');
                    setTimeout(() => {
                        window.location.href = '/redirect-a';
                    }, 500);
                </script>
            </body></html>""")
        elif self.path == '/frame':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><script>fetch('/frame-fetch');</script></body></html>")
        elif self.path == '/popup':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""<html><body>
                <script>
                    setTimeout(() => {
                        fetch('/popup-fetch');
                        document.cookie = 'foo=bar; path=/';
                        setTimeout(() => {
                            fetch('/cookie-test').then(() => {
                                fetch('/hanging-request');
                            });
                        }, 200);
                    }, 500);
                </script>
            </body></html>""")
        elif self.path == '/frame-fetch' or self.path == '/popup-fetch':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == '/redirect-a':
            self.send_response(302)
            self.send_header('Location', '/redirect-b')
            self.end_headers()
        elif self.path == '/redirect-b':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"redirected!")
        elif self.path == '/cookie-test':
            self.send_response(200)
            self.send_header('Set-Cookie', 'foo=bar; Path=/')
            self.end_headers()
            self.wfile.write(b"<script>document.cookie = 'foo=bar; path=/';</script>cookie set!")
        elif self.path == '/hanging-request':
            # deliberately hang
            time.sleep(10)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"too late")
            except:
                pass
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    server = ThreadingHTTPServer(('127.0.0.1', 8082), AdvTestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

def run_test():
    server = run_server()
    time.sleep(1)
    
    config.output_dir = "test_captures_adv"
    if os.path.exists("test_captures_adv"): shutil.rmtree("test_captures_adv")
        
    storage = StorageManager(config.output_dir)
    gui_msg_queue = queue.Queue()
    recorder = NetworkRecorder(storage, queue.Queue())
    recorder.start_recording()
    bm = BrowserManager(recorder, gui_msg_queue, start_url="http://127.0.0.1:8082/")
    bm.start()
    
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
            
    assert ready
    
    # Wait for JS to execute everything and hang
    time.sleep(4)
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)
    
    # Verify outputs
    cap_dir = storage.capture_dir
    with open(os.path.join(cap_dir, "manifest.jsonl"), 'r') as f:
        txns = [json.loads(l) for l in f.read().splitlines()]
        
    # Redirect check
    redirect_a = next(t for t in txns if '/redirect-a' in t['url'])
    redirect_b = next(t for t in txns if '/redirect-b' in t['url'])
    assert redirect_a['status'] == 302
    assert redirect_b['status'] == 200
    assert redirect_a['sequence'] != redirect_b['sequence']
    
    # Frames check
    frame_fetch = next(t for t in txns if '/frame-fetch' in t['url'])
    assert 'frame' in frame_fetch['frame_url'], f"Frame URL mismatch: {frame_fetch['frame_url']}"
    
    # Multiple pages check
    main_page_id = next(t for t in txns if '/frame-fetch' in t['url'])['page_id']
    popup_fetch = next(t for t in txns if '/popup-fetch' in t['url'])
    assert popup_fetch['page_id'] != main_page_id, f"Popup should have different page_id"
    
    # Shutdown / Hanging request check
    hanging = next(t for t in txns if '/hanging-request' in t['url'])
    assert hanging['completed'] == True
    assert "INCOMPLETE" in hanging['error']
    
    # Context Cookies check
    with open(os.path.join(cap_dir, "session.json"), 'r') as f:
        sess = json.load(f)
    print("Cookies:", sess.get('context_cookies'))
    
    print("Advanced features test passed!")

if __name__ == '__main__':
    run_test()
