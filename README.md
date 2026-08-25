# Browser Network Recorder

A production-quality Python desktop application that acts as a browser network/session recorder. Built with Tkinter and Playwright Chromium.

## Overview

This application launches a visible Chromium browser and records the browser's network activity while you browse normally. It captures requests, responses, and actively saves resources (HTML, JavaScript, CSS, JSON, Images, Fonts, Media) directly to the filesystem. 

## Features

- **Tkinter GUI:** Desktop interface to start, stop, and manage recordings.
- **Playwright + Chromium:** Uses Playwright's network interception and event handlers to capture traffic accurately.
- **Resource Saving:** Downloads the actual response bodies (e.g., HTML, JS, CSS, PNG) to disk.
- **Chronological Logging:** Maintains a detailed `network_log.txt` sequence.
- **Manifest Generation:** Creates a machine-readable `manifest.json`.
- **Thread Safety:** Safely manages the Playwright context in a background thread while keeping the Tkinter UI responsive via a synchronized event queue.
- **Multi-Tab Support:** Automatically tracks and attaches to new tabs/popups.

## Installation

1. **Install Python Requirements:**
   ```bash
   pip install playwright
   ```

2. **Install Playwright Browsers:**
   ```bash
   playwright install chromium
   ```

## Usage

Run the main file:
```bash
python main.py
```

1. **Launch Browser:** Click "Launch Browser". This opens a visible Chromium window.
2. **Start Recording:** Click "Start Recording" to begin logging network activity.
3. **Browse Normally:** Use the Chromium browser to navigate to websites. The application will track the activity in real-time.
4. **Stop Recording:** Click "Stop Recording" to finalize the log.

## Directory Structure

When a recording is stopped, the data is saved in a session folder (e.g., `captures/2026-08-25_18-52-31/`):
- `network_log.txt` - Chronological human-readable capture.
- `manifest.json` - JSON structured data for all events.
- `session.json` - Top-level session statistics.
- `resources/` - Actual downloaded responses like `000001.html`, `000002.js`.
- `request_bodies/` - POST/PUT payload bodies that were captured.

## Important Limitations

- **Not a Raw Packet Sniffer:** This is a browser-level recorder. Traffic outside the controlled browser context is not captured.
- **Service Workers & Caches:** Service worker requests or cached responses may limit body availability depending on what the browser exposes.
- **Huge Responses:** Media or massive files are bounded by a configurable size limit to protect memory.

## Security Warning

**Network captures may contain passwords, cookies, authentication tokens, personal data, and other highly sensitive information.** Be careful when sharing capture directories. Only record traffic from websites or accounts you are authorized to inspect.
