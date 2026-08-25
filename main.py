import tkinter as tk
import logging
import sys
import os
from gui import AppGUI

def main():
    # Setup application logging
    logging.basicConfig(
        level=logging.INFO, 
        filename="application.log", 
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    logger = logging.getLogger("main")
    
    # Check if playwright is installed by importing
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Please install it using:")
        print("pip install playwright")
        sys.exit(1)
        
    logger.info("Application starting")
    
    root = tk.Tk()
    app = AppGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    
    # Simple warning label at bottom
    warn_lbl = tk.Label(root, text="WARNING: Network captures may contain passwords, cookies, tokens, and other sensitive personal data.", fg="red")
    warn_lbl.pack(side=tk.BOTTOM, pady=5)
    
    root.mainloop()
    
    logger.info("Application closed")

if __name__ == "__main__":
    main()
