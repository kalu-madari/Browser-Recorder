import threading
import logging
import queue
from playwright.sync_api import sync_playwright
from network_recorder import NetworkRecorder
from config import config

try:
    from playwright_stealth import Stealth
except ImportError:
    Stealth = None

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
        self.latest_nav_id = {}
        self.latest_dom_id = {}
        self.interaction_counter = 0
        self.primary_page = None
        
    def run(self):
        print("DEBUG: BROWSER MANAGER RUN THREAD STARTED")
        self.gui_msg_queue.put({"type": "STATE", "state": "STARTING"})
        with sync_playwright() as p:
            self.playwright = p
            try:
                # ignore_https_errors should be False by default per requirements
                args = ['--disable-popup-blocking']
                ignore_default_args = None
                
                if getattr(config, 'enable_stealth_mode', False):
                    args.extend([
                        '--disable-blink-features=AutomationControlled',
                        '--disable-infobars',
                    ])
                    ignore_default_args = ['--enable-automation']

                if config.extension_path:
                    args.extend([
                        f'--disable-extensions-except={config.extension_path}',
                        f'--load-extension={config.extension_path}'
                    ])
                    self.browser = None
                    self.context = p.chromium.launch_persistent_context(
                        config.user_data_dir or "./user_data",
                        headless=True,
                        args=args,
                        ignore_default_args=ignore_default_args,
                        ignore_https_errors=False, accept_downloads=True
                    )
                else:
                    self.browser = p.chromium.launch(
                        headless=True,
                        args=args,
                        ignore_default_args=ignore_default_args
                    )
                    self.context = self.browser.new_context(ignore_https_errors=False, accept_downloads=True)

                if getattr(config, 'enable_stealth_mode', False) and Stealth:
                    Stealth().apply_stealth_sync(self.context)
                
                if config.enable_interactions:
                    self.context.expose_binding("record_interaction", self._on_interaction)
                    self.context.expose_binding("record_mutation", self._on_mutation)
                    self.context.add_init_script("""
(() => {
    // 1. Stable Identity Assignment
    let nextId = 1;
    function assignId(node) {
        if (node.nodeType === Node.ELEMENT_NODE) {
            if (!node.hasAttribute('data-r-id')) {
                node.setAttribute('data-r-id', (nextId++).toString());
            }
            for (let child of node.childNodes) {
                assignId(child);
            }
        }
    }

    // 2. Shadow DOM support
    const originalAttachShadow = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function(init) {
        const shadowRoot = originalAttachShadow.call(this, init);
        this.setAttribute('data-r-shadow-host', 'true');
        observeMutations(shadowRoot);
        return shadowRoot;
    };

    // 3. Mutation Tracking
    let pendingMutations = [];
    function observeMutations(root) {
        const observer = new MutationObserver((mutations) => {
            for (let m of mutations) {
                if (m.type === 'childList') {
                    for (let n of m.addedNodes) assignId(n);
                    
                    pendingMutations.push({
                        type: 'childList',
                        target_id: m.target.getAttribute ? m.target.getAttribute('data-r-id') : null,
                        added_count: m.addedNodes.length,
                        removed_count: m.removedNodes.length
                    });
                } else if (m.type === 'attributes') {
                    // Ignore our own attribute
                    if (m.attributeName !== 'data-r-id') {
                        pendingMutations.push({
                            type: 'attributes',
                            target_id: m.target.getAttribute('data-r-id'),
                            attr: m.attributeName,
                            val: m.target.getAttribute(m.attributeName)
                        });
                    }
                } else if (m.type === 'characterData') {
                    const parent = m.target.parentNode;
                    // Compute ordinal of this text node among all parent childNodes.
                    // This disambiguates multiple sibling text nodes sharing the same parent target_id.
                    let child_index = null;
                    if (parent) {
                        child_index = Array.from(parent.childNodes).indexOf(m.target);
                    }
                    pendingMutations.push({
                        type: 'characterData',
                        target_id: parent && parent.getAttribute ? parent.getAttribute('data-r-id') : null,
                        child_index: child_index,
                        text: m.target.nodeValue
                    });
                }
            }
            if (pendingMutations.length > 50) flushMutations(); // Batch threshold
        });
        observer.observe(root, { childList: true, attributes: true, characterData: true, subtree: true });
    }

    function flushMutations() {
        if (pendingMutations.length > 0 && window.record_mutation) {
            window.record_mutation(pendingMutations).catch(() => {});
            pendingMutations = [];
        }
    }

    // Initial setup
    document.addEventListener('DOMContentLoaded', () => {
        if (document.documentElement) assignId(document.documentElement);
        observeMutations(document);
    });
    // In case script loads after DOMContentLoaded
    if (document.documentElement) {
        assignId(document.documentElement);
        observeMutations(document);
    } else {
        observeMutations(document);
    }

    // 4. Custom DOM Serializer for Shadow DOM (Declarative Shadow DOM format)
    window.__recorder_serializeDOM = function() {
        function serializeNode(node) {
            if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
            if (node.nodeType === Node.COMMENT_NODE) return '<!--' + node.nodeValue + '-->';
            if (node.nodeType !== Node.ELEMENT_NODE) return '';

            let html = '<' + node.tagName.toLowerCase();
            for (let attr of node.attributes) {
                html += ' ' + attr.name + '="' + attr.value.replace(/"/g, '&quot;') + '"';
            }
            html += '>';

            if (node.shadowRoot) {
                html += '<template shadowrootmode="' + node.shadowRoot.mode + '">';
                for (let child of node.shadowRoot.childNodes) {
                    html += serializeNode(child);
                }
                html += '</template>';
            }

            for (let child of node.childNodes) {
                html += serializeNode(child);
            }
            html += '</' + node.tagName.toLowerCase() + '>';
            return html;
        }
        return serializeNode(document.documentElement);
    };

    // 5. Interaction Tracking (Fixed)
    const eventsToCapture = ['click', 'dblclick', 'mousedown', 'mouseup', 'keydown', 'keyup', 'input', 'change', 'submit', 'scroll', 'focus', 'blur', 'copy', 'cut', 'paste'];

    function getCssSelector(el) {
        if (!(el instanceof Element)) return '';
        let path = [];
        while (el && el.nodeType === Node.ELEMENT_NODE) {
            let selector = el.nodeName.toLowerCase();
            if (el.id) {
                selector += '#' + el.id;
                path.unshift(selector);
                break;
            } else {
                let sib = el, nth = 1;
                while (sib = sib.previousElementSibling) {
                    if (sib.nodeName.toLowerCase() == selector) nth++;
                }
                if (nth != 1) selector += ":nth-of-type("+nth+")";
            }
            path.unshift(selector);
            el = el.parentNode;
        }
        return path.join(" > ");
    }

    function interactionHandler(event) {
        // Shadow DOM: use composedPath to get the true target inside shadow root
        const target = (event.composed && event.composedPath().length > 0) ? event.composedPath()[0] : event.target;
        
        let targetTag = target ? (target.tagName ? target.tagName.toLowerCase() : 'unknown') : 'unknown';
        let targetSelector = target ? getCssSelector(target) : '';
        let targetId = target && target.getAttribute ? target.getAttribute('data-r-id') : null;
        let targetText = target ? (target.innerText ? target.innerText.substring(0, 100) : '') : '';
        
        let targetValue = null;
        if (targetTag === 'input' || targetTag === 'textarea' || targetTag === 'select') {
            if (target.type === 'checkbox' || target.type === 'radio') {
                targetValue = target.checked ? 'true' : 'false';
            } else {
                targetValue = target.value;
            }
        }
        
        let coordinates = null;
        if (['click', 'dblclick', 'mousedown', 'mouseup'].includes(event.type)) {
            coordinates = {x: event.clientX, y: event.clientY};
        }
        
        let scrollX = null, scrollY = null;
        if (event.type === 'scroll') {
            scrollX = target === document ? window.scrollX : target.scrollLeft;
            scrollY = target === document ? window.scrollY : target.scrollTop;
        }
        
        let key = null;
        if (['keydown', 'keyup'].includes(event.type)) {
            key = event.key;
        }
        
        // Flush mutations before interaction
        flushMutations();

        const payload = {
            event_type: event.type,
            target_tag: targetTag,
            target_selector: targetSelector,
            target_id: targetId,
            target_text: targetText,
            target_value: targetValue,
            coordinates: coordinates,
            scroll_x: scrollX,
            scroll_y: scrollY,
            key: key,
            is_trusted: event.isTrusted
        };
        
        if (window.record_interaction) {
            window.record_interaction(payload).catch(() => {});
        }
    }

    eventsToCapture.forEach(ev => {
        window.addEventListener(ev, interactionHandler, { capture: true, passive: true });
    });
})();
""")
                
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
                else:
                    page.goto("about:blank")
                
                # Track the primary page so command handlers always target the right page
                self.primary_page = page
                
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
                            pg = self.primary_page
                            if pg and not pg.is_closed():
                                try:
                                    f_url = cmd.get("frame_url")
                                    if f_url:
                                        target_frame = next((f for f in pg.frames if f_url in f.url), None)
                                        if target_frame:
                                            target_frame.evaluate(cmd.get("js"))
                                        else:
                                            logger.error("Evaluate frame not found")
                                    else:
                                        pg.evaluate(cmd.get("js"))
                                except Exception as e:
                                    logger.error(f"Evaluate error: {e}")
                        elif cmd.get("action") == "goto":
                            pg = self.primary_page
                            if pg and not pg.is_closed():
                                try: pg.goto(cmd.get("url"))
                                except Exception as e: logger.error(f"goto error: {e}")
                        elif cmd.get("action") == "go_back":
                            pg = self.primary_page
                            if pg and not pg.is_closed():
                                try: pg.go_back()
                                except Exception as e: logger.error(f"go_back error: {e}")
                        elif cmd.get("action") == "go_forward":
                            pg = self.primary_page
                            if pg and not pg.is_closed():
                                try: pg.go_forward()
                                except Exception as e: logger.error(f"go_forward error: {e}")
                        elif cmd.get("action") == "reload":
                            pg = self.primary_page
                            if pg and not pg.is_closed():
                                try: pg.reload()
                                except Exception as e: logger.error(f"reload error: {e}")
                        elif cmd.get("action") == "iframe_eval":
                            pg = self.primary_page
                            if pg and not pg.is_closed():
                                try: pg.frame_locator("#ifr").evaluate(cmd.get("js"))
                                except Exception as e: logger.error(f"iframe_eval error: {e}")
                        elif cmd.get("action") == "new_page":
                            try:
                                new_pg = self.context.new_page()
                                new_pg.wait_for_timeout(1000)
                                new_pg.goto(cmd.get("url"))
                            except Exception as e: logger.error(f"new_page error: {e}")
                        elif cmd.get("action") == "close_page":
                            if len(self.context.pages) > 1:
                                try: self.context.pages[-1].close()
                                except Exception as e: logger.error(f"close_page error: {e}")
                        elif cmd.get("action") == "record_interaction":
                            self._handle_interaction(cmd.get("source"), cmd.get("payload"))
                        elif cmd.get("action") == "record_mutation":
                            self._handle_mutation(cmd.get("source"), cmd.get("payload"))
                        elif cmd.get("action") == "record_download":
                            self._handle_download(cmd.get("page_id"), cmd.get("download"))
                    except queue.Empty:
                        pass
                        
                    try:
                        pg = self.primary_page
                        if pg and not pg.is_closed():
                            pg.wait_for_timeout(100)
                        elif self.context.pages:
                            self.context.pages[0].wait_for_timeout(100)
                    except Exception:
                        if not self.context.pages:
                            break
            except Exception as e:
                logger.error(f"Browser thread error: {e}")
                self.gui_msg_queue.put({"type": "STATE", "state": "ERROR"})
            finally:
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
                        self.context.close()
                    if self.browser:
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
        page.on("framedetached", lambda f: self.recorder.frame_registry.remove_frame(f))
        page.on("download", lambda d: self._on_download(page_id, d))
        
        # CDP Integration for Initiator
        if config.enable_cdp:
            try:
                cdp = self.context.new_cdp_session(page)
                self.cdp_sessions[page_id] = cdp
                cdp.send("Network.enable")
                cdp.send("Page.enable")
                import datetime
                cdp.on("Network.requestWillBeSent", lambda event: self._on_cdp_request(event, page_id))
                cdp.on("Network.responseReceived", lambda event: self.recorder.attach_cdp_response(event))
                cdp.on("Network.loadingFinished", lambda event: self.recorder.attach_cdp_loading_finished(event.get("requestId")))
                cdp.on("Network.loadingFailed", lambda event: self.recorder.attach_cdp_loading_finished(event.get("requestId"), event.get("errorText")))
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
        frame_id = frame.get("id") or frame.get("frameId") or "unknown"
        
        # Proactively map main frame
        if is_main and self.primary_page and not self.primary_page.is_closed():
            self.recorder.frame_registry.map_frame(self.primary_page.main_frame, frame_id)
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
        self.latest_nav_id[frame_id] = nav_id
        
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
                self.recorder.handle_page_close(page_id)
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
                
                # Determine canonical frame id
                
                f_id = self.recorder.frame_registry.get_cdp_frame_id(frame) or "unknown"
                
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
                self.latest_dom_id[f_id] = record.snapshot_id
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

    def _on_download(self, page_id, download):
        # Playwright download object
        self.command_queue.put({
            "action": "record_download",
            "page_id": page_id,
            "download": download
        })

    def _handle_download(self, page_id, download):
        url = download.url
        suggested_filename = download.suggested_filename
        
        with self.recorder.lock:
            self.recorder.seq_counter += 1
            seq = self.recorder.seq_counter
            self.interaction_counter += 1
            dl_id = f"dl-{self.interaction_counter:06d}"
            
        import datetime
        from models import DownloadRecord
        
        record = DownloadRecord(
            download_id=dl_id,
            sequence=seq,
            page_id=page_id,
            frame_id="unknown", # Downloads originate from page context in PW
            timestamp=datetime.datetime.now().isoformat(),
            url=url,
            suggested_filename=suggested_filename
        )
        
        # We do not call download.path() because doing so in the Sync API blocks 
        # the Playwright event loop, preventing concurrent network capture. 
        # Furthermore, moving it to a background thread violates Playwright's greenlet 
        # thread-ownership rules and crashes.
        # Therefore, we only record the metadata of the download initiation.
        record.success = True
        record.path = "UNSAVED_METADATA_ONLY"
        self.recorder.storage.save_download_record(record)

    def _on_mutation(self, source, payload):
        self.command_queue.put({
            "action": "record_mutation",
            "source": source,
            "payload": payload
        })

    def _on_interaction(self, source, payload):
        self.command_queue.put({
            "action": "record_interaction",
            "source": source,
            "payload": payload
        })

    def _handle_mutation(self, source, payload):
        page = source.get("page")
        frame = source.get("frame")
        if not page or not frame:
            return
            
        page_id = getattr(page, '_page_id', 'unknown')
        canonical_frame_id = self.recorder.frame_registry.get_cdp_frame_id(frame) or "unknown"
        
        with self.recorder.lock:
            self.recorder.seq_counter += 1
            seq = self.recorder.seq_counter
            self.interaction_counter += 1
            mut_id = f"mut-{self.interaction_counter:06d}"
        
        import datetime
        from models import MutationRecord
        record = MutationRecord(
            mutation_id=mut_id,
            sequence=seq,
            page_id=page_id,
            frame_id=canonical_frame_id,
            timestamp=datetime.datetime.now().isoformat(),
            mutations=payload
        )
        self.recorder.storage.save_mutation_record(record)

    def _build_frame_target_chain(self, frame) -> list:
        """Walk the Playwright frame hierarchy from root to `frame`, collecting
        the data-r-id of each <iframe> element that contains a child frame.

        Returns an ordered list of data-r-id strings representing the iframe
        element chain from the top-level document to the given frame.
        Empty list means the frame IS the top-level main frame.

        Never uses CDP frame_id as the sole identity.
        Never uses URL-only matching.
        If the data-r-id cannot be resolved for any iframe element in the chain,
        that entry is recorded as "UNKNOWN" rather than omitted, so replay can
        detect the ambiguity explicitly and fail visibly.
        """
        if frame is None:
            return []
        chain = []
        current = frame
        try:
            while True:
                parent = current.parent_frame
                if parent is None:
                    # current is the top-level main frame; no iframe element above it
                    break
                try:
                    iframe_el = current.frame_element()
                    rid = iframe_el.get_attribute("data-r-id")
                    chain.append(rid if rid else "UNKNOWN")
                except Exception:
                    chain.append("UNKNOWN")
                current = parent
        except Exception:
            pass
        chain.reverse()  # root-to-leaf order
        return chain

    def _handle_interaction(self, source, payload):
        page = source.get("page")
        frame = source.get("frame")
        if not page or not frame:
            return
            
        page_id = getattr(page, '_page_id', 'unknown')
        canonical_frame_id = self.recorder.frame_registry.get_cdp_frame_id(frame) or "unknown"
        self.interaction_counter += 1
        interaction_id = f"int-{self.interaction_counter:06d}"
        
        with self.recorder.lock:
            self.recorder.seq_counter += 1
            seq = self.recorder.seq_counter
        
        import datetime
        from models import InteractionRecord
        
        target_value = payload.get("target_value")
        # Always record value — no filtering
        value_recorded = target_value is not None

        # Build the iframe element chain for OOPIF-safe frame identity.
        # This does NOT retain the frame object; it extracts data-r-id strings immediately.
        frame_target_chain = self._build_frame_target_chain(frame)

        record = InteractionRecord(
            interaction_id=interaction_id,
            sequence=seq,
            page_id=page_id,
            frame_id=canonical_frame_id,
            timestamp=datetime.datetime.now().isoformat(),
            event_type=payload.get("event_type"),
            target_tag=payload.get("target_tag"),
            target_selector=payload.get("target_selector"),
            target_id=payload.get("target_id"),
            target_text=payload.get("target_text"),
            target_value=target_value,
            value_recorded=value_recorded,
            coordinates=payload.get("coordinates"),
            scroll_x=payload.get("scroll_x"),
            scroll_y=payload.get("scroll_y"),
            key=payload.get("key"),
            dom_snapshot_id=self.latest_dom_id.get(canonical_frame_id),
            navigation_id=self.latest_nav_id.get(canonical_frame_id),
            is_trusted=payload.get("is_trusted", False),
            frame_target_chain=frame_target_chain
        )
        self.recorder.storage.save_interaction_record(record)
