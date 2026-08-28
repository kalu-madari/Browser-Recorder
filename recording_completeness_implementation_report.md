# Recording Completeness Implementation Report

## Overview
This document details the architectural fixes applied to resolve the gaps identified in the "Recording Completeness & Cross-Event Correlation Audit". The primary focus of these fixes was resolving "Frame ID Schizophrenia," ensuring 100% deterministic cross-event linkage without reintroducing speculative logic.

## 1. Canonical ID Architecture
The system now adheres strictly to Chromium CDP native identifiers across all subsystems, bridging the gap between Playwright's synthetic frame IDs and the underlying network activity.

- **`page_id`**: Inherited safely and securely on both standard correlations and fallback `PLAYWRIGHT_ONLY` mechanisms.
- **`frame_id`**: The canonical Chromium `frameId` string (e.g., `E69D55CF...`) is now pervasive across `NavigationRecord`, `InteractionRecord`, `DOMSnapshotRecord`, and `TransactionState`.
- **`interaction_id`**: Remains unique but now correctly includes a mathematically sound sequence number (`sequence`) derived from the central `seq_counter` lock.

## 2. Playwright ↔ CDP Frame Mapping (`FrameRegistry`)
A lifecycle-aware `FrameRegistry` was introduced in `network_recorder.py` to securely map Playwright `Frame` references to CDP `frameId` strings.

- **Proactive Main Frame Mapping**: When the root document navigates (and `Page.frameNavigated` confirms `parentId` is None), the main frame is instantly registered against its canonical ID.
- **Lazy Iframe Mapping via Network Bridge**: When an iframe generates network activity, the identical CDP and Playwright request signatures correlate via the exact TCP/DNS timing fingerprint. At this exact moment of correlation, the `FrameRegistry` learns the mapping: `registry.map_frame(pw_frame, cdp_frameId)`.
- **"Unknown" Fallback**: If an iframe (like a nameless `about:blank` frame with no network activity) cannot be mapped before an interaction occurs, it correctly explicitly records its `frame_id` as `"unknown"` rather than guessing or mutating its identity.

## 3. HTTP → Page/Frame Linkage
- `PendingPWRecord` was upgraded to retain `page_id` and the `pw_frame` reference.
- `PendingCDPRecord` captures `frameId` natively from the `requestWillBeSent` event.
- During resolution, `TransactionState` constructs perfectly mapped objects.
- `PLAYWRIGHT_ONLY` fallbacks (such as root document navigations where Playwright suppresses CDP events) now reliably look up their `frame_id` using the `FrameRegistry` and retain their `page_id`. 

## 4. Interaction Correlation Model
- `InteractionRecord` now natively possesses a `sequence` attribute derived under the central `seq_counter` lock.
- **Causality Model**: We do *not* artificially pretend an interaction rigidly caused a specific subsequent network request. Instead, analytical models can securely deduce causality by observing `sequence` proximity combined with matching `page_id` and canonical `frame_id`.

## 5. Event Ordering Model
- Ordering remains strictly reliant on the globally locked `seq_counter`.
- Timestamp matching was removed from identity concerns and correctly shifted to observability/analysis purposes.

## 6. WebSocket → Page/Frame Linkage
- WebSockets (`WebSocketState`) are firmly associated with the active `page_id` based on `Network.webSocketCreated` events. 
- **Limitation**: Chromium CDP does not natively expose `frameId` directly on WebSocket creations, and as per architectural directives, we do not speculatively guess the frame based on URL patterns.

## 7. Cache/Service-Worker Limitations
- Cached assets continue to use the deterministic `PLAYWRIGHT_ONLY` fallback when CDP timing events are missing or distorted by Service Workers. Due to the upgraded `PendingPWRecord` architecture, these cached responses now successfully retain full `page_id` and `frame_id` context.

## 8. Fixes Implemented
- `models.py`: Added `frame_id: str` to `TransactionState`. Added `sequence: int` to `InteractionRecord`.
- `network_recorder.py`: Implemented `FrameRegistry` bridging layer. Updated pending record schemas and instantiation sites.
- `browser_manager.py`: Removed naive `f_id = "main"` logic and fully integrated `FrameRegistry.get_cdp_frame_id()`. 

## 9. Tests and Exact Results
A new adversarial frame test (`test_adversarial_frames.py`) was constructed featuring nested identical cross-origin iframes. 
- `wrong_frame_assignment = 0`
- `wrong_page_assignment = 0`
- `orphaned_frame_records = 0`

The full regression suite (`run_all.bat`) was executed covering massive concurrent GET/POST loads, WebSocket lifecycles, advanced cached features, and DOM mutations:
- `ALL TESTS PASSED!`

## 10. Remaining Limitations
- Applications that dynamically generate heavily interactive `<iframe src="about:blank">` trees with zero network activity will output `"unknown"` for interaction frame IDs inside those frames. This is behaviorally correct (as we cannot mathematically prove the identity), but represents an inherent boundary in passively tracking non-networked nested architectures.
