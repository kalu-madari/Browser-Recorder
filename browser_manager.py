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
        self.command_queue = queue.Queue()
        self.nav_counter = 0
        self.frame_urls = {} # frame_id -> url
        self.recent_navs = {} # nav_id -> NavigationRecord
        
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
                        cmd = self.command_queue.get_nowait()
                        if cmd.get("action") == "capture_dom":
                            reason = cmd.get("reason", "manual")
                            nav_id = cmd.get("nav_id")
                            nav_frame_id = cmd.get("nav_frame_id")
                            dom_snapshot_id = cmd.get("dom_snapshot_id")
                            nav_page_id = cmd.get("page_id")
                            if self.context.pages:
                                for page in self.context.pages:
                                    page_id = getattr(page, '_page_id', 'unknown')
                                    p_dom_snapshot_id = dom_snapshot_id if (nav_page_id == page_id or not nav_page_id) else None
                                    self.capture_all_frames(page, page_id, reason, nav_frame_id=nav_frame_id, dom_snapshot_id=p_dom_snapshot_id)
                        elif cmd.get("action") == "navigation":
                            self._handle_navigation_event(cmd.get("page_id"), cmd.get("event"), is_within_document=False)
                        elif cmd.get("action") == "navigatedWithinDocument":
                            self._handle_navigation_event(cmd.get("page_id"), cmd.get("event"), is_within_document=True)
                        elif cmd.get("action") == "navigation_failed":
                            self._handle_navigation_failed(cmd.get("page_id"), cmd.get("event"))
                        elif cmd.get("action") == "evaluate":
                            if self.context.pages:
                                try:
                                    self.context.pages[0].evaluate(cmd.get("js"))
                                except Exception as e:
                                    logger.error(f"Evaluate error: {e}")
                        elif cmd.get("action") == "goto":
                            if self.context.pages:
                                try: self.context.pages[0].goto(cmd.get("url"))
                                except Exception as e: logger.error(f"goto error: {e}")
                        elif cmd.get("action") == "go_back":
                            if self.context.pages:
                                try: self.context.pages[0].go_back()
                                except Exception as e: logger.error(f"go_back error: {e}")
                        elif cmd.get("action") == "go_forward":
                            if self.context.pages:
                                try: self.context.pages[0].go_forward()
                                except Exception as e: logger.error(f"go_forward error: {e}")
                        elif cmd.get("action") == "reload":
                            if self.context.pages:
                                try: self.context.pages[0].reload()
                                except Exception as e: logger.error(f"reload error: {e}")
                        elif cmd.get("action") == "iframe_eval":
                            if self.context.pages:
                                try: self.context.pages[0].frame_locator("#ifr").evaluate(cmd.get("js"))
                                except Exception as e: logger.error(f"iframe_eval error: {e}")
                        elif cmd.get("action") == "new_page":
                            try:
                                page = self.context.new_page()
                                page.wait_for_timeout(1000) # Wait for CDP to attach via event loop
                                page.goto(cmd.get("url"))
                            except Exception as e: logger.error(f"new_page error: {e}")
                        elif cmd.get("action") == "close_page":
                            if len(self.context.pages) > 1:
                                try: self.context.pages[-1].close()
                                except Exception as e: logger.error(f"close_page error: {e}")
                    except queue.Empty:
                        pass
                        
                    try:
                        if self.context.pages:
                            self.context.pages[0].wait_for_timeout(100)
                    except Exception:
                        if not self.context.pages:
                            break
            except Exception as e:
                logger.error(f"Browser thread error: {e}")
                self.gui_msg_queue.put({"type": "STATE", "state": "ERROR"})
            finally:
                if self.browser:
                    try:
                        if self.context:
                            if config.enable_dom_snapshots:
                                for page in self.context.pages:
                                    page_id = getattr(page, '_page_id', 'unknown')
                                    try:
                                        self.capture_all_frames(page, page_id, "shutdown")
                                    except Exception:
                                        pass
                            
                            cookies = self.context.cookies()
                            self.recorder.storage.final_cookies = cookies
                        self.browser.close()
                    except:
                        pass
                self.gui_msg_queue.put({"type": "STATE", "state": "CLOSED"})
                
    def on_page(self, page):
        self.page_counter += 1
        page_id = f"page-{self.page_counter:03d}"
        page._page_id = page_id
        
        # Playwright Events
        page.on("request", lambda r: self.recorder.handle_request(r, page_id))
        page.on("response", lambda r: self.recorder.handle_response(r))
        page.on("requestfinished", lambda r: self.recorder.handle_request_finished(r))
        page.on("requestfailed", lambda r: self.recorder.handle_request_failed(r))
        page.on("close", lambda p: self._on_page_close(page_id))
        page.on("load", lambda p: self.command_queue.put({"action": "capture_dom", "reason": "initial_load"}))
        
        # CDP Integration for Initiator
        if config.enable_cdp:
            try:
                cdp = self.context.new_cdp_session(page)
                self.cdp_sessions[page_id] = cdp
                cdp.send("Network.enable")
                cdp.send("Page.enable")
                import datetime
                cdp.on("Network.requestWillBeSent", lambda event: self._on_cdp_request(event, page_id))
                cdp.on("Network.webSocketCreated", lambda event: self.recorder.attach_cdp_websocket(event.get("url"), event.get("requestId"), page_id, event))
                cdp.on("Network.webSocketWillSendHandshakeRequest", lambda event: self.recorder.attach_cdp_websocket_handshake(event.get("requestId"), True, event.get("request", {}).get("headers", {})))
                cdp.on("Network.webSocketHandshakeResponseReceived", lambda event: self.recorder.attach_cdp_websocket_handshake(event.get("requestId"), False, event.get("response", {}).get("headers", {}), event.get("response", {}).get("status"), event.get("response", {}).get("statusText")))
                cdp.on("Network.webSocketClosed", lambda event: self.recorder.attach_cdp_websocket_close(event.get("requestId"), datetime.datetime.now().isoformat(), event.get("reason"), event.get("code")))
                cdp.on("Network.webSocketFrameSent", lambda event: self.recorder.attach_cdp_websocket_frame(event.get("requestId"), "SENT", event.get("response", {})))
                cdp.on("Network.webSocketFrameReceived", lambda event: self.recorder.attach_cdp_websocket_frame(event.get("requestId"), "RECEIVED", event.get("response", {})))
                
                cdp.on("Page.frameNavigated", lambda event: self.command_queue.put({"action": "navigation", "page_id": page_id, "event": event}))
                cdp.on("Page.navigatedWithinDocument", lambda event: self.command_queue.put({"action": "navigatedWithinDocument", "page_id": page_id, "event": event}))
            except Exception as e:
                logger.warning(f"Could not attach CDP session to {page_id}: {e}")

    def _handle_navigation_event(self, page_id, event, is_within_document):
        frame = event.get("frame", {}) if not is_within_document else event
        is_main = frame.get("parentId") is None
        frame_id = "main" if is_main else (frame.get("id") or frame.get("frameId") or "unknown")
        to_url = frame.get("url", "")
        
        nav_type_cdp = event.get("type", "unknown") if not is_within_document else "navigatedWithinDocument"
        from_url = self.frame_urls.get(frame_id)
        self.frame_urls[frame_id] = to_url
        
        self.nav_counter += 1
        nav_id = f"nav-{self.nav_counter:06d}"
        
        import datetime
        from models import NavigationRecord
        
        reason = nav_type_cdp
        nav_type = "document_navigation"
        if is_within_document:
            nav_type = "history_push_state"
            reason = "history_api_or_anchor"

        dom_snapshot_id = None
        if config.enable_dom_snapshots:
            dom_snapshot_id = self.recorder.storage.get_next_dom_snapshot_id(page_id, frame_id)

        document_request_sequence = None
        loader_id = frame.get("loaderId")
        if loader_id and not is_within_document:
            document_request_sequence = self.recorder.loader_to_seq.get(loader_id)

        record = NavigationRecord(
            navigation_id=nav_id,
            page_id=page_id,
            frame_id=frame_id,
            timestamp=datetime.datetime.now().isoformat(),
            from_url=from_url,
            to_url=to_url,
            type=nav_type,
            reason=reason,
            status=None,
            success=True,
            document_request_sequence=document_request_sequence,
            dom_snapshot_id=dom_snapshot_id,
            error=None
        )
        self.recent_navs[nav_id] = record
        self.recorder.storage.save_navigation_record(record)
        
        # Request DOM snapshot for this navigation
        self.command_queue.put({"action": "capture_dom", "reason": "Navigation", "nav_id": nav_id, "nav_frame_id": frame_id, "dom_snapshot_id": dom_snapshot_id, "page_id": page_id})

    def _handle_navigation_failed(self, page_id, event):
        pass

    def _on_page_close(self, page_id):
        if page_id in self.cdp_sessions:
            try:
                # Playwright automatically detaches CDP on page close, 
                # we just need to clean up our reference.
                del self.cdp_sessions[page_id]
            except Exception as e:
                logger.error(f"Error cleaning up CDP session for {page_id}: {e}")

    def capture_frame_dom(self, frame, page_id, reason, nav_frame_id=None, dom_snapshot_id=None):
        if not config.enable_dom_snapshots:
            return
        try:
            # Sync values to attributes so outerHTML captures them
            sync_js = """(() => {
                try {
                    for (const el of document.querySelectorAll('input, textarea, select')) {
                        if (el.type === 'checkbox' || el.type === 'radio') {
                            if (el.checked) el.setAttribute('checked', 'checked');
                            else el.removeAttribute('checked');
                        } else {
                            el.setAttribute('value', el.value);
                        }
                    }
                } catch(e) {}
                return document.documentElement ? document.documentElement.outerHTML : '';
            })()"""
            html = frame.evaluate(sync_js)
            if html:
                html_bytes = html.encode('utf-8')
                import datetime
                from models import DOMSnapshotRecord
                
                # Determine frame id string
                f_id = "main" if frame == frame.page.main_frame else (frame.name or f"frame-{id(frame)}")
                
                # Use predefined dom_snapshot_id if this is the exact frame that navigated
                assigned_id = ""
                if nav_frame_id and dom_snapshot_id and f_id == nav_frame_id:
                    assigned_id = dom_snapshot_id

                record = DOMSnapshotRecord(
                    snapshot_id=assigned_id,
                    page_id=page_id,
                    frame_id=f_id,
                    url=frame.url,
                    timestamp=datetime.datetime.now().isoformat(),
                    reason=reason,
                    title=frame.page.title() if frame == frame.page.main_frame else "",
                    html_path="",
                    html_sha256="",
                    html_size=0
                )
                self.recorder.storage.save_dom_snapshot(record, html_bytes)
        except Exception as e:
            logger.error(f"DOM capture failed for {page_id}: {e}")

    def capture_all_frames(self, page, page_id, reason, nav_frame_id=None, dom_snapshot_id=None):
        if not config.enable_dom_snapshots:
            return
        try:
            for frame in page.frames:
                self.capture_frame_dom(frame, page_id, reason, nav_frame_id=nav_frame_id, dom_snapshot_id=dom_snapshot_id)
        except Exception as e:
            logger.error(f"Failed to capture all frames for {page_id}: {e}")

    def _on_cdp_request(self, event, page_id):
        req = event.get("request", {})
        url = req.get("url")
        method = req.get("method")
        initiator = event.get("initiator")
        request_id = event.get("requestId")
        if url and initiator and request_id:
            self.recorder.attach_cdp_request(url, method, request_id, initiator, event, page_id)

    def trigger_dom_snapshot(self):
        self.command_queue.put({"action": "capture_dom"})

    def stop(self):
        self.running = False
