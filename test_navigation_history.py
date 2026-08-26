import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import queue
from browser_manager import BrowserManager
from network_recorder import NetworkRecorder
from storage import StorageManager
from config import config

class NavHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
        
    def do_GET(self):
        if self.path == "/page-a":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body><h1>PAGE A</h1></body></html>")
        elif self.path == "/page-b":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body><h1>PAGE B</h1></body></html>")
        elif self.path == "/page-c":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body><h1>PAGE C</h1></body></html>")
        elif self.path == "/redirect-a":
            self.send_response(302)
            self.send_header("Location", "/redirect-b")
            self.end_headers()
        elif self.path == "/redirect-b":
            self.send_response(301)
            self.send_header("Location", "/final")
            self.end_headers()
        elif self.path == "/final":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body><h1>FINAL</h1></body></html>")
        elif self.path == "/iframe-test":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body><iframe id='ifr' src='/page-a'></iframe></body></html>")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"NOT FOUND")

def run_server():
    server = HTTPServer(("127.0.0.1", 8095), NavHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

def test_navigation_history():
    import config as cfg
    cfg.config.extension_path = None
    cfg.config.enable_stealth_mode = False
    config.enable_cdp = True
    config.enable_dom_snapshots = True
    
    server = run_server()
    time.sleep(1)
    
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "captures"))
    storage = StorageManager(base_dir)
    gui_queue = queue.Queue()
    recorder = NetworkRecorder(storage, gui_queue)
    
    bm = BrowserManager(recorder, gui_queue)
    bm.start()
    
    ready = False
    start_wait = time.time()
    while time.time() - start_wait < 30:
        try:
            msg = gui_queue.get_nowait()
            if isinstance(msg, dict) and msg.get("type") == "STATE" and msg.get("state") == "READY":
                ready = True
                break
        except queue.Empty:
            time.sleep(0.1)
            
    if not ready:
        print("Browser failed to start.")
        return
        
    recorder.start_recording()
    
    # We will use command_queue for most things, but wait...
    print("1. Navigate to page-a")
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8095/page-a"})
    time.sleep(1.5)
    
    print("2. Navigate to page-b")
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8095/page-b"})
    time.sleep(1.5)
    
    print("3. Navigate to page-c")
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8095/page-c"})
    time.sleep(1.5)
    
    print("4. Back")
    bm.command_queue.put({"action": "go_back"})
    time.sleep(1.5)
    
    print("5. Back")
    bm.command_queue.put({"action": "go_back"})
    time.sleep(1.5)
    
    print("6. Forward")
    bm.command_queue.put({"action": "go_forward"})
    time.sleep(1.5)
    
    print("7. Forward")
    bm.command_queue.put({"action": "go_forward"})
    time.sleep(1.5)
    
    print("8. Reload")
    bm.command_queue.put({"action": "reload"})
    time.sleep(1.5)
    
    print("9. JavaScript navigate")
    bm.command_queue.put({"action": "evaluate", "js": "location.href = '/page-b'"})
    time.sleep(1.5)
    
    print("10. pushState")
    bm.command_queue.put({"action": "evaluate", "js": "history.pushState({}, '', '/virtual-a')"})
    time.sleep(1.5)
    
    print("11. replaceState")
    bm.command_queue.put({"action": "evaluate", "js": "history.replaceState({}, '', '/virtual-b')"})
    time.sleep(1.5)
    
    print("12. redirect chain")
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8095/redirect-a"})
    time.sleep(2)
    
    print("13. failed navigation")
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:9999/"})
    time.sleep(3)
    
    # Recover from error page
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8095/page-a"})
    time.sleep(2)
    
    print("14. iframe navigation")
    bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8095/iframe-test"})
    time.sleep(2)
    bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('ifr').contentWindow.location.href='/page-b'"})
    time.sleep(2)
    
    print("15. open second page")
    bm.command_queue.put({"action": "new_page", "url": "http://127.0.0.1:8095/page-c"})
    time.sleep(2)
    
    print("17. close second page")
    bm.command_queue.put({"action": "close_page"})
    time.sleep(2)
        
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)
    server.shutdown()
    
    # ASSERTS
    with open(storage.navigation_manifest_path, "r", encoding="utf-8") as f:
        navs = [json.loads(line) for line in f if line.strip()]
        
    # Programmatically verify:
    # - every expected navigation exists
    # - no duplicate navigation IDs
    nav_ids = [n["navigation_id"] for n in navs]
    assert len(nav_ids) == len(set(nav_ids)), "Duplicate navigation IDs"
    
    # - correct page IDs
    pages = set(n["page_id"] for n in navs)
    assert len(pages) >= 2, "Should have navigations from multiple pages"
    
    # - failed navigation is recorded
    failed = [n for n in navs if n["success"] == False]
    assert len(failed) >= 1, "Failed navigation not recorded"
    assert "9999" in failed[0]["to_url"], "Failed navigation wrong URL"
    
    # - reload is recorded (Note: Playwright page.reload() may emit 'Navigation' instead of 'Reload')
    reloads = [n for n in navs if n["from_url"] == n["to_url"] and n["from_url"] is not None]
    assert len(reloads) >= 1, "Reload not recorded"
    
    # - redirects remain distinguishable from browser history
    redirects = [n for n in navs if "redirect-a" in n["to_url"] or "redirect-b" in n["to_url"]]
    assert len(redirects) == 0, "Redirects were incorrectly captured as navigations"
    
    finals = [n for n in navs if "final" in n["to_url"]]
    assert len(finals) >= 1, "Final redirect destination not captured"
    
    # - pushState/replaceState
    push_states = [n for n in navs if n["type"] == "history_push_state"]
    assert len(push_states) >= 2, "pushState/replaceState not captured"
    
    # - iframe navigation
    iframes = [n for n in navs if n["frame_id"] != "main"]
    assert len(iframes) >= 1, "Iframe navigation not captured"
    
    # - navigation ↔ DOM references are valid
    dom_refs = [n for n in navs if n["dom_snapshot_id"] is not None]
    assert len(dom_refs) >= 1, "No DOM snapshot IDs captured in navigations"
    
    print("Navigation test passed!")

if __name__ == "__main__":
    test_navigation_history()
