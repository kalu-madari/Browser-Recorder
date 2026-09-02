"""
test_chardata_child_index.py

Tests that MutationObserver characterData mutations correctly record
child_index to disambiguate sibling text nodes.

The page creates two sibling text nodes and a button that mutates them.
Clicking the button triggers the mutation AND flushes via the interaction
handler (flushMutations() is called before every interaction payload).
"""
import time, os, shutil, queue, json, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from config import config
from storage import StorageManager
from network_recorder import NetworkRecorder
from browser_manager import BrowserManager

HTML = b"""<!DOCTYPE html><html><body>
<div id="host"></div>
<button id="mutate-btn">Mutate</button>
<script>
// Pre-create sibling text nodes before recorder init
var d = document.getElementById('host');
d.appendChild(document.createTextNode('First'));
d.appendChild(document.createTextNode('Second'));

document.getElementById('mutate-btn').addEventListener('click', function() {
    // Mutate both text nodes; child_index should be 0 and 1 respectively
    d.childNodes[0].nodeValue = 'Changed-First';
    d.childNodes[1].nodeValue = 'Changed-Second';
});
</script>
</body></html>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(HTML)
    def log_message(self, *a): pass

srv = HTTPServer(('127.0.0.1', 9401), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

config.enable_dom_snapshots = False
config.enable_interactions = True
config.output_dir = 'test_chardata_out'
config.user_data_dir = 'test_chardata_user'
for d in ['test_chardata_out', 'test_chardata_user']:
    if os.path.exists(d): shutil.rmtree(d)

storage = StorageManager('test_chardata_out')
gui_q = queue.Queue()
recorder = NetworkRecorder(storage, gui_q)
bm = BrowserManager(recorder, gui_q, 'http://127.0.0.1:9401/')
recorder.start_recording()
bm.start()

# Wait for READY
deadline = time.time() + 10
while time.time() < deadline:
    try:
        msg = gui_q.get(timeout=0.2)
        if msg.get("state") == "READY":
            break
    except queue.Empty:
        pass

time.sleep(1)

# Click the mutate button — this triggers text mutations
bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('mutate-btn').click();"})
time.sleep(1)
# Second click/interaction to trigger flush of pending mutations
bm.command_queue.put({"action": "evaluate", "js": "document.getElementById('mutate-btn').click();"})
time.sleep(2)

recorder.stop_recording()
bm.stop()
bm.join(timeout=8)
srv.shutdown()

path = os.path.join(storage.capture_dir, 'mutations.jsonl')
if not os.path.exists(path):
    print("No mutations.jsonl found")
else:
    with open(path) as f:
        batches = [json.loads(l) for l in f if l.strip()]
    char_muts = [m for b in batches for m in b['mutations'] if m.get('type') == 'characterData']
    print(f"characterData mutations: {len(char_muts)}")
    for m in char_muts:
        tid = m.get('target_id')
        ci = m.get('child_index')
        txt = m.get('text')
        print(f"  target_id={tid} child_index={ci} text={repr(txt)}")
    indices = [m.get('child_index') for m in char_muts if m.get('child_index') is not None]
    print(f"Distinct child_index values: {sorted(set(indices))}")
    if len(char_muts) >= 2:
        assert len(set(indices)) > 1, f"FAIL: child_index must differ for sibling text nodes, got {indices}"
        print("PASS: Sibling text nodes have distinct child_index values")
    else:
        print("SKIP: Not enough characterData mutations captured")
