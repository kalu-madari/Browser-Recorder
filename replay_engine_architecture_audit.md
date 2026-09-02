REPLAY ENGINE ARCHITECTURE AUDIT

Current Recording Model:
The recorder produces a strictly ordered, append-only JSONL event stream mapped to a shared monotonic sequence integer. The core relational models are:
- TransactionState: Network requests, responses, binary body references, and explicitly forked failure states.
- NavigationRecord: Page traversals tied to document network sequences.
- InteractionRecord: User inputs with deterministic 	arget_id, scroll_x/y, and boolean states.
- MutationRecord: DOM changes (childList, attributes, characterData) batched before interactions.
- DOMSnapshotRecord: Full declarative HTML checkpoints.
- DownloadRecord: Asynchronous file save events.
- WebSocketState/Frame: Bidirectional message streams.

Replay Execution Model:
The system requires a **hybrid** execution model. 
It must be **state-driven** for network and DOM (allowing the real application's JavaScript to execute and naturally reconstruct the DOM by reacting to mocked network responses), while being **event-driven** for interactions (injecting recorded clicks/keys when the DOM state is ready). Manually applying MutationRecords (like rrweb) would irreversibly break the live JavaScript context (e.g., React hydration crashes).

Network Replay:
Replay must intercept all network requests (page.route("**/*")) and serve recorded responses (Mock Mode).
- **Matching rules:** Match incoming requests against the pool of pending recorded TransactionStates using exact URL, Method, and Post Body (sha256). 
- **Concurrent identical requests:** Are fungible. The first matching intercepted request consumes the first recorded response in the sequence. 
- **Strictness:** Unexpected requests must trigger a FATAL mismatch to guarantee determinism.
- **Speculation:** URL+Method fuzzy correlation is banned.

DOM Replay:
- Snapshots and Mutations act as **oracles for synchronization**, not instructions for reconstruction.
- Before firing an interaction, the replay engine waits for the target element to appear in the live browser DOM.
- If a node is missing, the engine pauses until a timeout, then fails visibly. Silent skips are prohibited.

Interaction Replay:
- **Target resolution:** Prefer 	arget_id ([data-r-id="X"]). 
- **Fallback strategy:** If 	arget_id diverges, fallback strictly to topological 	arget_selector + 	arget_tag + 	arget_text. 
- **Action:** Playwright native actions (locator.click(), locator.evaluate(el => el.scrollTop = X)).
- Fuzzy AI/visual matching is prohibited.

Page/Frame Replay:
- page_id maps 1:1 to Playwright Page instances.
- Same-origin iframes resolve via CDP rame_id.
- **OOPIF Cross-Origin Frames:** Currently record as rame_id = "unknown". Replay must fallback to locating the frame via structural hierarchy or URL.

Shadow DOM:
Open shadow roots reconstruct perfectly during replay because the mock HTML/network responses will drive the app to naturally call ttachShadow. The replay engine simply uses Playwright's native shadow-piercing locators to interact with elements inside them. Closed roots remain explicitly unsupported.

Downloads:
Replay intercepts the download-triggering network request, serving the recorded headers (Content-Disposition: attachment). Playwright natively handles the download. Replay validates the emitted download event metadata against the DownloadRecord but intentionally ignores the binary file contents.

Failure Replay:
- Recorded CDP_ONLY_FAILED and PLAYWRIGHT_ONLY_FAILED must be actively reproduced via oute.abort("failed").
- Ambiguous failures must not be heuristically recombined; the engine faithfully reproduces exactly the sequence of aborts captured.

Timing & Ordering:
- sequence is the absolute arbiter of causality.
- Execution occurs as fast as possible (Accelerated Replay) while respecting the sequence dependencies between network responses and user interactions. Wall-clock delays (	imestamp deltas) are only applied between consecutive human interactions to simulate realistic pacing, avoiding race conditions.

Mismatch Model:
- **Unexpected Network Request:** FATAL (App state has diverged).
- **Missing Target Node:** FATAL (Cannot proceed with interaction).
- **Target ID Mismatch:** RECOVERABLE (Counter divergence is expected; fallback to structural selector).
- **DOM Mutation Mismatch:** WARNING (Internal render batches may safely differ).

Recording Format Gaps:
| Requirement | Available Data | Sufficient? | Missing Data | Proposed Change |
|---|---|---|---|---|
| Stable Element Identity | Monotonic data-r-id | NO | Structural hashes | The 
extId++ counter will inevitably diverge during replay due to async rendering timing differences. Must add a structural/hierarchical hash or explicit app key attribute capture. |
| Cross-Origin Frame Identity | rame_id="unknown" | NO | rame_url | InteractionRecord lacks rame_url. When rame_id is unknown, replay has literally zero information to locate the correct OOPIF iframe. |
| Sibling Text Mutations | 	arget_id of parent | NO | Child index | characterData mutations against elements with multiple text nodes cannot be deterministically resolved. |

Security Model:
- Replay runs in **Strict Mock Mode** by default. Zero outbound network traffic is permitted.
- Live Replay Mode requires explicit opt-in to prevent accidental execution of destructive recorded actions (e.g., DELETE /api/users).

Performance:
JSONL streaming is required for manifests exceeding 50MB. WebSockets and binary bodies must stream from disk to avoid OOM crashes during long-session replay.

Test Strategy:
- Independent Oracles: Build a mock HTTP server that asserts exact request sequences and verifies the final server-side state matches the recorded session.
- Chaos Testing: Introduce artificial network latency during replay to prove sequence-based synchronization prevents interaction race conditions.

Required Changes Before Implementation:
1. (CRITICAL) Add rame_url or a hierarchical frame selector to InteractionRecord and MutationRecord to solve the OOPIF "unknown" identity gap.
2. (HIGH) Enhance 	arget_id generation in rowser_manager.py to use deterministic structural hashing (e.g., path + tag + index) rather than a fragile 
extId++ monotonic counter.
3. (LOW) Add child_index to MutationRecord characterData events.

Architecture Decisions:
The event and network architectures are highly robust, but element and frame identity in cross-origin/dynamic contexts are mathematically guaranteed to diverge in their current format during replay.

Known Limitations:
- Closed shadow roots remain un-serializable.
- Network and frame-identity correlation for cross-origin OOPIFs remains untracked due to CDP isolation boundaries.

Final Recommendation:
RECORDING FORMAT CHANGES REQUIRED
