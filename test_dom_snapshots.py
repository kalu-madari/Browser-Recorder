import os
import sys
import time
import json
import hashlib
import unittest
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import queue
import logging

from browser_manager import BrowserManager
from network_recorder import NetworkRecorder
from storage import StorageManager
from config import config

config.max_dom_snapshot_size = 50 * 1024 * 1024
config.enable_dom_snapshots = True

HTML_INITIAL = """
<!DOCTYPE html>
<html>
<head>
    <title>Initial Title</title>
</head>
<body>
    <div id="status">initial</div>
    <div id="to-remove">remove me</div>
    <div id="to-change-attr" data-val="old">attr</div>
    <div id="to-change-class" class="old-class">class</div>
    <input id="my-input" value="old" />
    <my-component></my-component>
    <iframe src="/iframe.html" id="my-iframe" name="test-iframe"></iframe>
    <script>
        const el = document.querySelector('my-component');
        const shadow = el.attachShadow({mode: 'open'});
        shadow.innerHTML = '<div id="shadow-content">shadow</div>';
    </script>
</body>
</html>
"""

HTML_IFRAME_INITIAL = """
<!DOCTYPE html>
<html>
<body>
    <div id="iframe-status">iframe initial</div>
</body>
</html>
"""

HTML_NAV = """
<!DOCTYPE html>
<html>
<head><title>Page B</title></head>
<body>
    <div id="nav-status">page b</div>
</body>
</html>
"""

class TestServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_INITIAL.encode('utf-8'))
        elif self.path == '/iframe.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_IFRAME_INITIAL.encode('utf-8'))
        elif self.path == '/page_b.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_NAV.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

class TestDOMSnapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = 'test_captures_dom'
        cls.server = HTTPServer(('127.0.0.1', 8086), TestServerHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        if os.path.exists(cls.test_dir):
            import shutil
            shutil.rmtree(cls.test_dir)

    def test_dom_snapshots(self):
        import config as cfg
        cfg.config.extension_path = None
        cfg.config.enable_stealth_mode = False
        import shutil
            
        q = queue.Queue()
        storage = StorageManager('test_captures_dom')
        recorder = NetworkRecorder(storage, q)
        manager = BrowserManager(recorder, q, start_url='http://127.0.0.1:8086/')
        manager.start()

        # wait for READY state
        while True:
            msg = q.get()
            if msg.get("type") == "STATE" and msg.get("state") == "READY":
                break
                
        time.sleep(3) # wait for load

        # JavaScript DOM mutation
        manager.command_queue.put({"action": "evaluate", "js": '''
            document.getElementById("status").textContent = "updated";
            document.getElementById("to-remove").remove();
            let newEl = document.createElement("div");
            newEl.id = "added";
            newEl.textContent = "added text";
            document.body.appendChild(newEl);
            document.getElementById("to-change-attr").setAttribute("data-val", "new");
            document.getElementById("to-change-class").className = "new-class";
            document.getElementById("my-input").value = "new input";
            document.title = "Updated Title";
            
            let iframe = document.getElementById("my-iframe");
            iframe.contentDocument.getElementById("iframe-status").textContent = "iframe updated";
        '''})
        
        # Explicit snapshot
        time.sleep(1)
        manager.trigger_dom_snapshot()
        time.sleep(1)

        # Multiple pages
        manager.command_queue.put({"action": "new_page", "url": "http://127.0.0.1:8086/"})
        time.sleep(2)

        # Navigation
        manager.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8086/page_b.html"})
        time.sleep(2)

        manager.stop()
        manager.join()
        storage.finalize()

        dom_manifest = os.path.join(storage.capture_dir, 'dom.jsonl')
        self.assertTrue(os.path.exists(dom_manifest))
        
        records = []
        with open(dom_manifest, 'r') as f:
            for line in f:
                records.append(json.loads(line))
                
        self.assertTrue(len(records) > 0, 'No DOM snapshots recorded')

        for rec in records:
            if rec.get('truncated'): continue
            file_path = os.path.join(storage.capture_dir, rec['html_path'])
            self.assertTrue(os.path.exists(file_path), f"File {file_path} missing")
            size = os.path.getsize(file_path)
            with open(file_path, 'rb') as f:
                body = f.read()
                expected_sha256 = rec['html_sha256']
                actual_sha256 = hashlib.sha256(body).hexdigest()
                self.assertEqual(actual_sha256, expected_sha256)
                self.assertEqual(size, rec['html_size'])
            
        page_1_records = [r for r in records if r['page_id'] == 'page-001' and r['frame_id'] == 'main']
        page_1_iframe = [r for r in records if r['page_id'] == 'page-001' and 'test-iframe' in r['frame_id']]
        
        initial = next((r for r in page_1_records if r['reason'] == 'initial_load'), None)
        self.assertIsNotNone(initial)
        with open(os.path.join(storage.capture_dir, initial['html_path']), 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn('id="status">initial<', content)
            
        manual = next((r for r in page_1_records if r['reason'] == 'manual'), None)
        self.assertIsNotNone(manual)
        with open(os.path.join(storage.capture_dir, manual['html_path']), 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn('id="status">updated<', content)
            self.assertNotIn('remove me', content)
            self.assertIn('id="added">added text<', content)
            self.assertIn('data-val="new"', content)
            self.assertIn('class="new-class"', content)
            self.assertIn('value="new input"', content)
            
        nav = [r for r in page_1_records if 'page_b' in r['url']]
        self.assertTrue(len(nav) > 0)
        with open(os.path.join(storage.capture_dir, nav[-1]['html_path']), 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn('page b', content)
            self.assertNotIn('updated', content)
            
        iframe_manual = next((r for r in page_1_iframe if r['reason'] == 'manual'), None)
        if iframe_manual:
            with open(os.path.join(storage.capture_dir, iframe_manual['html_path']), 'r', encoding='utf-8') as f:
                content = f.read()
                self.assertIn('iframe updated', content)
                
        print("Test passed successfully.")

if __name__ == '__main__':
    unittest.main()
