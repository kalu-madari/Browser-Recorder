import os
import json
import time
import shutil
from pathlib import Path
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys

# Add current dir to path to import the app
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

class TestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # suppress logs

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.send_header('Set-Cookie', 'test_cookie=123; Path=/')
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <head>
                    <link rel="stylesheet" href="/style.css">
                    <script src="/app.js"></script>
                </head>
                <body>
                    <h1>Test</h1>
                    <img src="/image.png">
                    <iframe src="/frame.html"></iframe>
                    <script>
                        setTimeout(() => {
                            fetch('/data.json');
                            fetch('/delayed'); // Out of order
                            fetch('/fast'); // Out of order
                            fetch('/duplicate');
                            fetch('/duplicate');
                            fetch('/fail').catch(e => console.log('expected fail'));
                            
                            // POST
                            fetch('/post', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({hello: 'world'})
                            });
                            
                            // popup
                            setTimeout(() => window.open('/popup.html'), 500);
                        }, 500);
                    </script>
                </body>
                </html>
            """)
        elif self.path == '/style.css':
            self.send_response(200)
            self.send_header('Content-type', 'text/css')
            self.end_headers()
            self.wfile.write(b"body { background: #f0f0f0; }")
        elif self.path == '/app.js':
            self.send_response(200)
            self.send_header('Content-type', 'application/javascript')
            self.end_headers()
            self.wfile.write(b"console.log('app running');")
        elif self.path == '/image.png':
            self.send_response(200)
            self.send_header('Content-type', 'image/png')
            self.end_headers()
            self.wfile.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
        elif self.path == '/data.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        elif self.path == '/delayed':
            time.sleep(2.0)
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"delayed")
        elif self.path == '/fast':
            time.sleep(0.1)
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"fast")
        elif self.path == '/duplicate':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"dup")
        elif self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/redirected')
            self.end_headers()
        elif self.path == '/redirected':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"redirected")
        elif self.path == '/frame.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body>frame</body></html>")
        elif self.path == '/popup.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body>popup</body></html>")
        elif self.path == '/fail':
            # drop conn
            self.connection.close()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/post':
            length = int(self.headers.get('content-length', 0))
            body = self.rfile.read(length)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"received": true}')
            
def run_server():
    server = HTTPServer(('127.0.0.1', 8080), TestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

def test_integration():
    print("Starting test server...")
    server = run_server()
    time.sleep(1)
    
    # We will simulate AppGUI programmatically without Tkinter mainloop to avoid blocking
    import queue
    from browser_manager import BrowserManager
    from network_recorder import NetworkRecorder
    from storage import StorageManager
    from config import config
    
    config.output_dir = "test_captures"
    if os.path.exists("test_captures"):
        shutil.rmtree("test_captures")
        
    storage = StorageManager("test_captures")
    gui_queue = queue.Queue()
    gui_msg_queue = queue.Queue()
    recorder = NetworkRecorder(storage, gui_queue)
    bm = BrowserManager(recorder, gui_msg_queue, start_url="http://127.0.0.1:8080/")
    
    print("Starting BrowserManager...")
    bm.start()
    
    # Wait for READY
    ready = False
    for _ in range(60):
        try:
            msg = gui_msg_queue.get_nowait()
            if msg["type"] == "STATE" and msg["state"] == "READY":
                ready = True
                break
        except queue.Empty:
            time.sleep(0.5)
            
    if not ready:
        print("Browser failed to start.")
        return
        
    print("Browser READY. Starting recording...")
    recorder.start_recording()
    
    print("Waiting for network activity to settle (incl. delayed requests)...")
    time.sleep(6)
    

    
    print("Stopping recording...")
    recorder.stop_recording()
    
    # Shutdown
    bm.stop()
    bm.join(timeout=5)
    
    # Check outputs
    print("Checking outputs...")
    cap_dir = storage.capture_dir
    manifest_path = os.path.join(cap_dir, "manifest.jsonl")
    network_log_path = os.path.join(cap_dir, "network_log.txt")
    
    with open(manifest_path, 'r') as f:
        manifest_lines = f.read().splitlines()
        
    txns = [json.loads(l) for l in manifest_lines]
    urls = [t['url'] for t in txns]
    print(f"Captured {len(txns)} transactions.")
    
    assert any('/app.js' in u for u in urls), "Missing app.js"
    assert any('/delayed' in u for u in urls), "Missing delayed"
    assert any('/duplicate' in u for u in urls), "Missing duplicate"
    assert urls.count('http://127.0.0.1:8080/duplicate') >= 2, "Duplicates not separate"
    assert any('/post' in u for u in urls), "Missing post"

    assert any('/fail' in u for u in urls), "Missing fail"
    
    # verify sequence matches resource filenames
    for txn in txns:
        if txn.get('resource'):
            file_path = txn['resource']['file_path']
            seq_str = f"{txn['sequence']:06d}"
            assert seq_str in file_path, f"Resource {file_path} doesn't match seq {seq_str}"
            
    print("Tests passed successfully!")

if __name__ == '__main__':
    test_integration()
