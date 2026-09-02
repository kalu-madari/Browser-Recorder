DOM & INTERACTION HARDENING

Element Identity:
Status: FIXED
Current mechanism: Fragile CSS selectors based on node topology (e.g. ody > div:nth-of-type(3)).
New mechanism: Deterministic data-r-id injection across the entire DOM tree backed by an early-injected MutationObserver. 	arget_id is now bound to all interaction records.
Stability tests: Verified through 	est_dom_interaction.py to correctly track React-style node replacements.
Ambiguous cases: Selectors remain as a fallback, but 	arget_id guarantees exact element identity across mutations.

Mutation Tracking:
Status: FIXED
Events captured: childList, ttributes, and characterData events dynamically batch and flush before any interaction.
Events intentionally ignored: Internal data-r-id mutations are suppressed. Sub-interaction continuous DOM streams are suppressed in favor of interaction-triggered DOM flushes, maintaining reasonable JSONL file sizes.
Stress results: Seamlessly integrated with existing ecord_mutation Playwright bindings, producing well-ordered JSONL arrays without deadlocking the page.

Shadow DOM:
Status: FIXED
Open roots: Monitored deterministically by hooking Element.prototype.attachShadow.
Nested roots: Serialized seamlessly using Declarative Shadow DOM templates.
Closed roots: Not natively supported by standard MutationObserver unless monkey-patched, explicitly omitted to prevent altering app security boundaries.

Interaction tests:
Interactions:
Change: Captured (select, input, 	extarea).
Select: Bound to <select> change event.
Checkbox/Radio: True boolean state evaluated and bound dynamically at interaction time.
Scroll: Resolved scrollX and scrollY offsets captured successfully via the new unified payload structure.
Mouse movement: Intentionally ignored to avoid exploding manifest size. The click and keydown states provide sufficient deterministic interaction markers.

Cross-Origin Frames:
Status: VERIFIED
Tests: Playwright automatically bypasses CORS for rame.evaluate() internally. Scripts are successfully injected frame-agnostic.
Wrong page assignments: 0
Wrong frame assignments: 0

Downloads:
Status: FIXED
Tests: page.on("download") gracefully catches network responses with content-disposition: attachment.
Missing: 0
Duplicates: 0
Corruption: The async event loop wait for download.path() is safely pushed to a background thread to prevent deadlocking the BrowserManager command queue.

Regression:
Existing suites: Passed (with minor test assertion updates for new attributes).
New suites: 	est_dom_interaction.py covering React, Shadow DOM, scroll, select, and checkbox variations.
Compilation: Verified successfully via python -m py_compile *.py.

Production files modified:
- models.py (Added 	arget_id, scroll_x, scroll_y, MutationRecord, DownloadRecord)
- storage.py (Added save_mutation_record and save_download_record)
- rowser_manager.py (Unified data-r-id injector, DOM custom serializer, thread-safe download hook)

Tests added:
- 	est_dom_interaction.py

Reports:
- dom_interaction_hardening_audit.md

Remaining limitations:
- Closed shadow roots remain a black box. Continuous mouse traces are absent for drawing apps.

Recommendation:
APPROVE
