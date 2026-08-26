import threading, time, asyncio, websockets
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import queue

async def echo(websocket):
    try:
        async for message in websocket:
            await websocket.send(message)
    except Exception as e:
        pass

class HTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = """
            <html><body><script>
            let ws1 = new WebSocket('ws://127.0.0.1:8768/ws/a');
            ws1.binaryType = 'arraybuffer';
            ws1.onopen = () => {
                console.log("WS1 OPENED");
                ws1.send('Hello');
                ws1.send(new Uint8Array([1, 2, 3]));
            };
            ws1.onmessage = (e) => console.log("WS1 MSG:", e.data);
            
            let ws2 = new WebSocket('ws://127.0.0.1:8768/ws/b');
            ws2.binaryType = 'arraybuffer';
            ws2.onopen = () => {
                console.log("WS2 OPENED");
                ws2.send('Hello2');
            };
            
            // Third connection stays open
            window.ws3 = new WebSocket('ws://127.0.0.1:8768');
            </script></body></html>
            """
            self.wfile.write(html.encode())
        else:
            self.send_response(404)
            self.end_headers()

def run_http():
    server = ThreadingHTTPServer(('127.0.0.1', 8093), HTMLHandler)
    server.serve_forever()

def run_ws():
    async def serve():
        async with websockets.serve(echo, "127.0.0.1", 8768):
            await asyncio.Future()
    asyncio.run(serve())

def main():
    threading.Thread(target=run_http, daemon=True).start()
    threading.Thread(target=run_ws, daemon=True).start()
    time.sleep(1)
    
    from network_recorder import NetworkRecorder
    from browser_manager import BrowserManager
    from storage import StorageManager
    from playwright.sync_api import sync_playwright
    import os, shutil
    
    storage = StorageManager("captures")
    gui_q = queue.Queue()
    recorder = NetworkRecorder(storage, gui_q)
    recorder.recording = True
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        
        # Attach hooks manually
        page.on("request", lambda r: recorder.handle_request(r, "page-001"))
        page.on("response", recorder.handle_response)
        page.on("requestfinished", recorder.handle_request_finished)
        page.on("requestfailed", recorder.handle_request_failed)
        page.on("console", lambda msg: print("PAGE LOG:", msg.text))
        
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        import datetime
        cdp.on("Network.webSocketCreated", lambda e: recorder.attach_cdp_websocket(e.get("url"), e.get("requestId"), "page-001", e))
        cdp.on("Network.webSocketWillSendHandshakeRequest", lambda e: recorder.attach_cdp_websocket_handshake(e.get("requestId"), True, e.get("request", {}).get("headers", {})))
        cdp.on("Network.webSocketHandshakeResponseReceived", lambda e: recorder.attach_cdp_websocket_handshake(e.get("requestId"), False, e.get("response", {}).get("headers", {}), e.get("response", {}).get("status"), e.get("response", {}).get("statusText")))
        cdp.on("Network.webSocketClosed", lambda e: recorder.attach_cdp_websocket_close(e.get("requestId"), datetime.datetime.now().isoformat(), e.get("reason"), e.get("code")))
        cdp.on("Network.webSocketFrameSent", lambda e: recorder.attach_cdp_websocket_frame(e.get("requestId"), "SENT", e.get("response", {})))
        cdp.on("Network.webSocketFrameReceived", lambda e: recorder.attach_cdp_websocket_frame(e.get("requestId"), "RECEIVED", e.get("response", {})))
        
        page.goto("http://127.0.0.1:8093/")
        page.wait_for_timeout(3000)
        
        recorder.stop_recording()
        
    ws_jsonl = storage.websocket_jsonl_path
    import json
    lines = []
    with open(ws_jsonl, 'r') as f:
        for line in f:
            if line.strip(): lines.append(json.loads(line))
            
    conns = [line for line in lines if line['type'] == 'connection']
    frames = [line for line in lines if line['type'] == 'frame']
    
    print(f"Captured {len(conns)} WS connections and {len(frames)} frames.")
    for c in conns:
        print(f"WS {c['id']} -> URL: {c['url']}")
    for f in frames:
        print(f"  Frame {f['sequence']}: {f['direction']} {f['payload_type']} size={f['payload_size']} id={f['websocket_id']}")

if __name__ == '__main__':
    main()
