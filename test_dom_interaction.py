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
config.output_dir = "test_dom_int_out"
config.user_data_dir = "test_dom_int_user_data"

PORT = 9106

HTML = b'''
<!DOCTYPE html>
<html>
<body>
    <div id="root"></div>
    <select id="myselect">
        <option value="a">A</option>
        <option value="b">B</option>
    </select>
    <input type="checkbox" id="mycheck">
    
    <div id="scroll-container" style="height: 100px; overflow-y: scroll;">
        <div style="height: 500px;">Scrollable content</div>
    </div>
    <div id="shadow-host"></div>
    <script>
        // React-style replacement
        const root = document.getElementById('root');
        
        let count = 0;
        function render() {
            root.innerHTML = '';
            const btn = document.createElement('button');
            btn.id = 'btn-' + count;
            btn.innerText = 'Click me ' + count;
            root.appendChild(btn);
            count++;
        }
        render();
        
        // Shadow DOM
        const host = document.getElementById('shadow-host');
        const shadow = host.attachShadow({mode: 'open'});
        const shadowBtn = document.createElement('button');
        shadowBtn.id = 'shadow-btn';
        shadowBtn.innerText = 'Shadow Click';
        shadow.appendChild(shadowBtn);
        
        window.render = render;
    </script>
</body>
</html>
'''

class TestServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(HTML)
    def log_message(self, *args): pass

def run_server(port):
    server = HTTPServer(("127.0.0.1", port), TestServer)
    server.serve_forever()

def main():
    threading.Thread(target=run_server, args=(PORT,), daemon=True).start()
    
    if os.path.exists(config.output_dir):
        shutil.rmtree(config.output_dir)
        
    storage = StorageManager(config.output_dir)
    import queue
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    
    import sys
    sys.argv = []
    
    bm = BrowserManager(recorder, gui_q, f"http://127.0.0.1:{PORT}/")
    bm.browser = None
    
    recorder.start_recording()
    bm.start()
    
    time.sleep(3) # Wait for load
    
    # 1. Select Change
    bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('myselect').value = 'b'; document.getElementById('myselect').dispatchEvent(new Event('change'));"})
    time.sleep(1)
    
    # 2. Checkbox Click
    bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('mycheck').click();"})
    time.sleep(1)
    
    # 3. Scroll
    bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('scroll-container').scrollTop = 50; document.getElementById('scroll-container').dispatchEvent(new Event('scroll'));"})
    time.sleep(1)
    
    # 4. Shadow DOM Click
    bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('shadow-host').shadowRoot.querySelector('button').click();"})
    time.sleep(1)
    
    # 5. React-style replace and click
    bm.command_queue.put({"action": "evaluate", "js": "window.render();"})
    time.sleep(1)
    bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('btn-1').click();"})
    time.sleep(1)
    
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)
    
    cap_dir = storage.capture_dir
    

    # Analyze output
    try:
        with open(os.path.join(cap_dir, 'mutations.jsonl')) as f:
            mutations = [json.loads(l) for l in f]
    except FileNotFoundError:
        mutations = []
        
    try:
        with open(os.path.join(cap_dir, 'interaction.jsonl')) as f:
            interactions = [json.loads(l) for l in f]
    except FileNotFoundError:
        interactions = []

        
    print(f"Captured {len(mutations)} mutation records.")
    print(f"Captured {len(interactions)} interaction records.")
    
    for int_rec in interactions:
        print(f"Int: {int_rec['event_type']} | TargetID: {int_rec.get('target_id')} | Val: {int_rec.get('target_value')} | Scroll: {int_rec.get('scroll_y')}")

if __name__ == '__main__':
    main()
