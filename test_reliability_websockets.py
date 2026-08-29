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

PORT = 8204

def run_server():
    import asyncio
    import websockets
    async def echo(websocket):
        try:
            async for message in websocket:
                await websocket.send(message)
        except Exception:
            pass
    
    async def serve():
        async with websockets.serve(echo, "127.0.0.1", PORT):
            await asyncio.Future()  # run forever
            
    asyncio.run(serve())

def main():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(1) # wait for ws server to start

    storage = StorageManager("test_ws_lifecycle")
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    bm = BrowserManager(recorder, gui_q, "about:blank")
    
    recorder.start_recording()
    bm.start()
    
    while True:
        try:
            msg = gui_q.get(timeout=0.1)
            if isinstance(msg, dict) and msg.get("type") == "STATE" and msg.get("state") == "READY":
                break
        except queue.Empty:
            continue
            
    # Open 100 WebSockets
    js = "window.sockets = []; for (let i=0; i<100; i++) { let ws = new WebSocket(ws://127.0.0.1:{PORT}); window.sockets.push(ws); }"
    bm.command_queue.put({"action": "evaluate", "js": js})
    time.sleep(2)
    
    # Close 50 normally
    js_close = "for (let i=0; i<50; i++) { window.sockets[i].close(); }"
    bm.command_queue.put({"action": "evaluate", "js": js_close})
    time.sleep(1)
    
    with recorder.lock:
        remaining = len(recorder.active_websockets)
        
    recorder.stop_recording()
    bm.stop()
    bm.join(timeout=5)
    
    print(f"Created: 100")
    print(f"Remaining active before shutdown: {remaining}")
    # After shutdown, active_websockets must be 0
    print(f"Remaining active after shutdown: {len(recorder.active_websockets)}")

if __name__ == '__main__':
    main()
