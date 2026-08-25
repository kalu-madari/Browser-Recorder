import threading
import logging
import queue
from playwright.sync_api import sync_playwright
from network_recorder import NetworkRecorder
from config import config

logger = logging.getLogger(__name__)

class BrowserManager(threading.Thread):
    def __init__(self, recorder: NetworkRecorder, gui_msg_queue: queue.Queue, start_url: str = None):
        super().__init__()
        self.recorder = recorder
        self.gui_msg_queue = gui_msg_queue
        self.playwright = None
        self.browser = None
        self.context = None
        self.running = True
        self.daemon = True
        self.page_counter = 0
        self.cdp_sessions = {}
        self.start_url = start_url

    def run(self):
        print("DEBUG: BROWSER MANAGER RUN THREAD STARTED")
        self.gui_msg_queue.put({"type": "STATE", "state": "STARTING"})
        with sync_playwright() as p:
            self.playwright = p
            try:
                # ignore_https_errors should be False by default per requirements
                self.browser = p.chromium.launch(headless=False, args=['--disable-popup-blocking'])
                self.context = self.browser.new_context(ignore_https_errors=False)
                
                self.context.on("page", self.on_page)
                
                page = self.context.new_page()
                self.gui_msg_queue.put({"type": "STATE", "state": "READY"})
                
                # Give UI test a bit of time to start recording before navigating
                import time
                time.sleep(1)
                
                if self.start_url:
                    print("DEBUG: Navigating to", self.start_url)
                    page.goto(self.start_url)
                    print("DEBUG: Navigated successfully")
                    # Trigger popup after short delay
                    page.evaluate("setTimeout(() => window.open('http://127.0.0.1:8080/popup.html'), 1000)")
                else:
                    page.goto("about:blank")
                
                while self.running:
                    if not self.context.pages:
                        break
                    try:
                        page.wait_for_timeout(100)
                    except Exception:
                        if len(self.context.pages) > 0:
                            page = self.context.pages[0]
                        else:
                            break
            except Exception as e:
                logger.error(f"Browser thread error: {e}")
                self.gui_msg_queue.put({"type": "STATE", "state": "ERROR"})
            finally:
                if self.browser:
                    try:
                        if self.context:
                            cookies = self.context.cookies()
                            self.recorder.storage.final_cookies = cookies
                        self.browser.close()
                    except:
                        pass
                self.gui_msg_queue.put({"type": "STATE", "state": "CLOSED"})
                
    def on_page(self, page):
        self.page_counter += 1
        page_id = f"page-{self.page_counter:03d}"
        
        # Playwright Events
        page.on("request", lambda r: self.recorder.handle_request(r, page_id))
        page.on("response", lambda r: self.recorder.handle_response(r))
        page.on("requestfinished", lambda r: self.recorder.handle_request_finished(r))
        page.on("requestfailed", lambda r: self.recorder.handle_request_failed(r))
        page.on("close", lambda p: self._on_page_close(page_id))
        
        # CDP Integration for Initiator
        if config.enable_cdp:
            try:
                cdp = self.context.new_cdp_session(page)
                self.cdp_sessions[page_id] = cdp
                cdp.send("Network.enable")
                import datetime
                cdp.on("Network.requestWillBeSent", lambda event: self._on_cdp_request(event, page_id))
                cdp.on("Network.webSocketCreated", lambda event: self.recorder.attach_cdp_websocket(event.get("url"), event.get("requestId"), page_id, event))
                cdp.on("Network.webSocketWillSendHandshakeRequest", lambda event: self.recorder.attach_cdp_websocket_handshake(event.get("requestId"), True, event.get("request", {}).get("headers", {})))
                cdp.on("Network.webSocketHandshakeResponseReceived", lambda event: self.recorder.attach_cdp_websocket_handshake(event.get("requestId"), False, event.get("response", {}).get("headers", {}), event.get("response", {}).get("status"), event.get("response", {}).get("statusText")))
                cdp.on("Network.webSocketClosed", lambda event: self.recorder.attach_cdp_websocket_close(event.get("requestId"), datetime.datetime.now().isoformat(), event.get("reason"), event.get("code")))
                cdp.on("Network.webSocketFrameSent", lambda event: self.recorder.attach_cdp_websocket_frame(event.get("requestId"), "SENT", event.get("response", {})))
                cdp.on("Network.webSocketFrameReceived", lambda event: self.recorder.attach_cdp_websocket_frame(event.get("requestId"), "RECEIVED", event.get("response", {})))
            except Exception as e:
                logger.warning(f"Could not attach CDP session to {page_id}: {e}")

    def _on_page_close(self, page_id):
        if page_id in self.cdp_sessions:
            try:
                # Playwright automatically detaches CDP on page close, 
                # we just need to clean up our reference.
                del self.cdp_sessions[page_id]
            except Exception as e:
                logger.error(f"Error cleaning up CDP session for {page_id}: {e}")

    def _on_cdp_request(self, event, page_id):
        req = event.get("request", {})
        url = req.get("url")
        method = req.get("method")
        initiator = event.get("initiator")
        request_id = event.get("requestId")
        if url and initiator and request_id:
            self.recorder.attach_cdp_request(url, method, request_id, initiator, event, page_id)

    def stop(self):
        self.running = False
