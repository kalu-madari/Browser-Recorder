import os
import json
import time
import shutil
from pathlib import Path
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys
import queue

# Add current dir to path to import the app
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from network_recorder import NetworkRecorder
from browser_manager import BrowserManager
from config import config

class InteractionTestHandler(BaseHTTPRequestHandler):
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
                    <input type="text" id="normal_input" value="">
                    <input type="password" id="secret_input" value="">
                    <input type="text" id="token_field" name="api_token" value="">
                    <button id="btn">Click Me</button>
                    <textarea id="text_area"></textarea>
                    
                    <script>
                        // Just an empty script
                    </script>
                </body>
                </html>
            """)
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    server = HTTPServer(('127.0.0.1', 8085), InteractionTestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server

def test_interactions():
    server = run_server()
    
    # Configure config for test
    old_output_dir = config.output_dir
    config.output_dir = "test_captures_int"
    config.enable_interactions = True
    config.record_text_input_values = False
    
    if os.path.exists(config.output_dir):
        shutil.rmtree(config.output_dir)
        
    gui_q = queue.Queue()
    from storage import StorageManager
    storage = StorageManager(config.output_dir)
    recorder = NetworkRecorder(storage, gui_q)
    bm = BrowserManager(recorder, gui_q)
    bm.start()
    
    try:
        # Wait for ready
        while True:
            try:
                msg = gui_q.get(timeout=15)
                print("GUI MSG:", msg)
                if msg.get("type") == "STATE" and msg.get("state") == "READY":
                    break
                if msg.get("type") == "STATE" and msg.get("state") == "ERROR":
                    print("Browser manager errored!")
                    break
            except queue.Empty:
                print("Queue timeout waiting for ready")
                break
                
        # Navigate
        bm.command_queue.put({"action": "goto", "url": "http://127.0.0.1:8085/"})
        time.sleep(2)
        
        # Interact using eval (simulating user actions)
        bm.command_queue.put({"action": "evaluate", "js": """
            const normal = document.getElementById('normal_input');
            normal.focus();
            normal.value = 'hello';
            normal.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
            normal.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
            normal.blur();
            
            const secret = document.getElementById('secret_input');
            secret.focus();
            secret.value = 'supersecret';
            secret.dispatchEvent(new Event('input', { bubbles: true }));
            secret.blur();
            
            const token = document.getElementById('token_field');
            token.focus();
            token.value = 'mytoken123';
            token.dispatchEvent(new Event('input', { bubbles: true }));
            token.blur();
            
            const btn = document.getElementById('btn');
            const clickEvent = new MouseEvent('click', { bubbles: true, clientX: 100, clientY: 200 });
            // Since we dispatch it manually, isTrusted will be false, but our code uses event.isTrusted
            btn.dispatchEvent(clickEvent);
            
            // To get isTrusted=true we can try to use page locator interactions if possible, 
            // but we can't do that easily from within browser_manager without blocking.
            // Dispatching is enough to test if python captures it.
        """})
        
        time.sleep(2)
        
        # Enable text input recording and do it again
        config.record_text_input_values = True
        bm.command_queue.put({"action": "evaluate", "js": """
            const normal2 = document.getElementById('normal_input');
            normal2.focus();
            normal2.value = 'hello2';
            normal2.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
        """})
        
        time.sleep(2)
        
    finally:
        bm.stop()
        bm.join(timeout=5)
        server.shutdown()
        server.server_close()
        config.output_dir = old_output_dir
        config.record_text_input_values = False

    # Verify
    session_dirs = os.listdir("test_captures_int")
    session_dir = os.path.join("test_captures_int", session_dirs[0])
    
    int_manifest = os.path.join(session_dir, "interaction.jsonl")
    assert os.path.exists(int_manifest), "Interaction manifest not found"
    
    interactions = []
    with open(int_manifest, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                interactions.append(json.loads(line))
                
    assert len(interactions) > 0, "No interactions captured"
    
    # Check if click coordinates were captured
    clicks = [i for i in interactions if i['event_type'] == 'click']
    assert len(clicks) >= 1, "Click event not captured"
    assert clicks[0]['target_tag'] == 'button', "Click target tag incorrect"
    assert clicks[0]['coordinates']['x'] == 100, "Click coordinates incorrect"
    
    # Check sensitive filtering
    secret_inputs = [i for i in interactions if i['event_type'] == 'input' and i['target_selector'].endswith('#secret_input')]
    assert len(secret_inputs) >= 1, "Secret input not captured"
    assert secret_inputs[0]['target_value'] is None, "Secret value should be filtered"
    
    token_inputs = [i for i in interactions if i['event_type'] == 'input' and i['target_selector'].endswith('#token_field')]
    assert len(token_inputs) >= 1, "Token input not captured"
    assert token_inputs[0]['target_value'] is None, "Token value should be filtered due to name"
    
    # Check text input values (first should be None, second should be hello2)
    normal_inputs = [i for i in interactions if i['event_type'] == 'input' and i['target_selector'].endswith('#normal_input')]
    assert len(normal_inputs) >= 2, "Normal inputs not captured"
    assert normal_inputs[0]['target_value'] is None, "First normal input should not be recorded (config)"
    assert normal_inputs[0]['value_recorded'] is False, "First normal input value_recorded should be false"
    
    assert normal_inputs[-1]['target_value'] == 'hello2', "Second normal input should be recorded"
    assert normal_inputs[-1]['value_recorded'] is True, "Second normal input value_recorded should be true"
    
    # Check keydown
    keys = [i for i in interactions if i['event_type'] == 'keydown']
    assert len(keys) >= 1, "Keydown not captured"
    assert keys[0]['key'] == 'a', "Key down key incorrect"
    
    print("ALL INTERACTION TESTS PASSED!")

if __name__ == "__main__":
    test_interactions()
