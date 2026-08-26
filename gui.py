import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import queue
import os
import threading
from browser_manager import BrowserManager
from network_recorder import NetworkRecorder
from storage import StorageManager
from config import config
import logging
from models import GUIEvent

logger = logging.getLogger(__name__)

class AppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Browser Network Recorder")
        self.root.geometry("1100x700")
        
        # Apply theme and styles
        self.style = ttk.Style()
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
            
        self.bg_color = "#f5f5f5"
        self.root.configure(background=self.bg_color)
        
        self.style.configure("TFrame", background=self.bg_color)
        self.style.configure("TLabel", background=self.bg_color, font=("Segoe UI", 10))
        self.style.configure("TLabelframe", background=self.bg_color)
        self.style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"), background=self.bg_color)
        self.style.configure("TButton", font=("Segoe UI", 10))
        self.style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        self.style.configure("Treeview", font=("Segoe UI", 9), rowheight=25)
        
        self.gui_queue = queue.Queue()
        self.gui_msg_queue = queue.Queue()
        self.storage = None
        self.recorder = None
        self.browser_manager = None
        
        self.setup_ui()
        self.poll_queues()
        
    def setup_ui(self):
        # Top Controls Frame
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=15, pady=10)
        
        self.btn_launch = ttk.Button(top_frame, text="Launch Browser", command=self.launch_browser, width=15)
        self.btn_launch.pack(side=tk.LEFT, padx=(0, 5))
        self.btn_start = ttk.Button(top_frame, text="Start Recording", command=self.start_recording, state=tk.DISABLED, width=15)
        self.btn_start.pack(side=tk.LEFT, padx=5)
        self.btn_stop = ttk.Button(top_frame, text="Stop Recording", command=self.stop_recording, state=tk.DISABLED, width=15)
        self.btn_stop.pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="New Session", command=self.new_session, width=15).pack(side=tk.LEFT, padx=5)
        
        # Status indicator
        self.lbl_browser_status = tk.Label(top_frame, text="Browser: Stopped", fg="#666666", bg=self.bg_color, font=("Segoe UI", 10, "bold"))
        self.lbl_browser_status.pack(side=tk.RIGHT, padx=10)
        
        # Directory configuration
        dir_frame = ttk.Frame(self.root)
        dir_frame.pack(fill=tk.X, padx=15, pady=5)
        ttk.Label(dir_frame, text="Output Directory:").pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value=os.path.abspath(config.output_dir))
        ttk.Entry(dir_frame, textvariable=self.dir_var, width=60).pack(side=tk.LEFT, padx=10)
        ttk.Button(dir_frame, text="Browse", command=self.browse_dir).pack(side=tk.LEFT)
        
        # Statistics Panel
        stats_frame = ttk.LabelFrame(self.root, text="Statistics", padding=10)
        stats_frame.pack(fill=tk.X, padx=15, pady=10)
        
        self.lbl_stats = tk.Label(stats_frame, text="Ready. Click 'Launch Browser' to begin.", justify=tk.LEFT, font=("Consolas", 10), bg=self.bg_color)
        self.lbl_stats.pack(anchor=tk.W)
        
        # Data View (Treeview)
        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=(5, 15))
        
        cols = ("Seq", "Time", "Type", "Method", "Status", "Size", "URL")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="browse")
        
        # Tag configuration for alternating rows
        self.tree.tag_configure('evenrow', background="#fdfdfd")
        self.tree.tag_configure('oddrow', background="#f4f4f9")
        self.tree.tag_configure('error', foreground="#d32f2f")
        
        self.tree.heading("Seq", text="Seq")
        self.tree.column("Seq", width=70, anchor=tk.CENTER)
        self.tree.heading("Time", text="Time")
        self.tree.column("Time", width=100, anchor=tk.CENTER)
        self.tree.heading("Type", text="Type")
        self.tree.column("Type", width=90, anchor=tk.CENTER)
        self.tree.heading("Method", text="Method")
        self.tree.column("Method", width=70, anchor=tk.CENTER)
        self.tree.heading("Status", text="Status")
        self.tree.column("Status", width=70, anchor=tk.CENTER)
        self.tree.heading("Size", text="Size")
        self.tree.column("Size", width=90, anchor=tk.E)
        self.tree.heading("URL", text="URL")
        self.tree.column("URL", width=500, anchor=tk.W)
        
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree_item_map = {} # Maps transaction sequence to tree item ID
        
    def browse_dir(self):
        d = filedialog.askdirectory()
        if d: self.dir_var.set(d)
        
    def launch_browser(self):
        if self.browser_manager and self.browser_manager.is_alive():
            messagebox.showinfo("Info", "Browser is already running.")
            return
            
        self.btn_launch.config(state=tk.DISABLED)
        self.lbl_browser_status.config(text="Browser: Starting...", fg="#ff9800")
        
        base_dir = self.dir_var.get()
        self.storage = StorageManager(base_dir)
        self.recorder = NetworkRecorder(self.storage, self.gui_queue)
        self.browser_manager = BrowserManager(self.recorder, self.gui_msg_queue)
        self.browser_manager.start()
        
    def start_recording(self):
        if self.recorder:
            self.recorder.start_recording()
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.NORMAL)
            
    def stop_recording(self):
        if self.recorder:
            self.recorder.stop_recording()
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            
    def new_session(self):
        self.stop_recording()
        
        def wait_and_restart():
            if self.browser_manager and self.browser_manager.is_alive():
                self.browser_manager.stop()
                self.browser_manager.join(timeout=3)
            self.root.after(0, self._reset_ui_for_new_session)
            
        threading.Thread(target=wait_and_restart, daemon=True).start()

    def _reset_ui_for_new_session(self):
        self.tree_item_map.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.btn_launch.config(state=tk.NORMAL)
        self.launch_browser()
        
    def poll_queues(self):
        # Poll Browser messages
        try:
            while True:
                msg = self.gui_msg_queue.get_nowait()
                if msg["type"] == "STATE":
                    state = msg["state"]
                    if state == "READY":
                        self.lbl_browser_status.config(text="Browser: Running", fg="#4caf50")
                        self.btn_start.config(state=tk.NORMAL)
                    elif state == "CLOSED" or state == "ERROR":
                        self.lbl_browser_status.config(text=f"Browser: {state}", fg="#f44336")
                        self.btn_start.config(state=tk.DISABLED)
                        self.btn_launch.config(state=tk.NORMAL)
                        self.stop_recording()
        except queue.Empty:
            pass

        # Poll Network events
        try:
            updates = 0
            while updates < 100:
                event: GUIEvent = self.gui_queue.get_nowait()
                self.handle_gui_event(event)
                updates += 1
            if updates > 0 and self.storage:
                self.update_stats()
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self.poll_queues)
            
    def handle_gui_event(self, event):
        # Handle dictionary events (e.g. {"type": "COMPLETED"} or {"type": "WS_UPDATED"})
        if isinstance(event, dict):
            return
            
        txn = event.transaction
        seq = f"{txn.sequence:06d}"
        time_str = txn.request_time.split('T')[-1][:12]
        
        if event.type == "STARTED":
            tags = ('evenrow',) if txn.sequence % 2 == 0 else ('oddrow',)
            item = self.tree.insert("", "end", values=(
                seq, time_str, txn.resource_type, txn.method,
                "(pending)", "", txn.url
            ), tags=tags)
            self.tree_item_map[txn.sequence] = item
            
            # Auto-truncate UI tree to avoid memory bloat
            if len(self.tree_item_map) > 2000:
                oldest_seq = min(self.tree_item_map.keys())
                oldest_item = self.tree_item_map.pop(oldest_seq)
                if self.tree.exists(oldest_item):
                    self.tree.delete(oldest_item)

        elif event.type in ["UPDATED", "COMPLETED", "FAILED"]:
            item = self.tree_item_map.get(txn.sequence)
            if item and self.tree.exists(item):
                size_str = ""
                if txn.resource:
                    size_str = f"{txn.resource.size} B"
                
                status_str = str(txn.status) if txn.status else ""
                
                tags = ('evenrow',) if txn.sequence % 2 == 0 else ('oddrow',)
                if event.type == "FAILED" or (txn.status and txn.status >= 400):
                    if event.type == "FAILED":
                        status_str = "FAILED"
                    tags = ('error',)
                    
                self.tree.item(item, values=(
                    seq, time_str, txn.resource_type, txn.method,
                    status_str, size_str, txn.url
                ), tags=tags)
            
    def update_stats(self):
        if self.storage:
            s = self.storage.stats
            mb_saved = s['bytes_saved'] / (1024 * 1024)
            txt = (f"Requests: {s['requests']:<8} Responses: {s['responses']:<8} Failed: {s['failed']:<8} Saved Data: {mb_saved:.2f} MB\n"
                   f"HTML: {s['html']:<12} JS: {s['js']:<15} CSS: {s['css']:<14} JSON: {s['json']:<12}\n"
                   f"Images: {s['images']:<10} Fonts: {s['fonts']:<12} Media: {s['media']:<12} Other: {s['other']:<11}")
            self.lbl_stats.config(text=txt)

    def on_closing(self):
        self.stop_recording()
        if self.browser_manager and self.browser_manager.is_alive():
            self.browser_manager.stop()
            self.browser_manager.join(timeout=1.0)
        self.root.destroy()
