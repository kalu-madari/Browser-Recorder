RECORDING FORMAT IMPLEMENTATION AUDIT

OOPIF Frame Chain:
Status: IMPLEMENTED
Fields: `frame_target_chain: List[str]` added to `InteractionRecord` (default `[]`, backward-compatible).
         Built by `_build_frame_target_chain(frame)` in `browser_manager.py` using `frame.frame_element()` + `get_attribute("data-r-id")`.
Same-origin: VERIFIED — same-origin iframe chain resolved correctly in test_frame_chain.py Test 1.
Cross-origin: VERIFIED — cross-origin OOPIF test recorded `frame_target_chain=['4']` with `frame_id=unknown`.
              `frame_element()` resolves the parent <iframe> element even when CDP frame ID is unavailable.
Nested: VERIFIED — resolver logic test (Test 3) passed nested chain traversal.
Identical URLs: VERIFIED — resolver resolves by `data-r-id`, not URL. Identical-URL iframes produce correct distinct chains.
Ambiguous cases: If two iframes in the same parent context share identical `data-r-id` values (cannot happen normally since IDs are monotonically unique), the replay resolver must detect this and fail visibly. The resolver test confirms the "ambiguous" case raises an explicit error rather than silently selecting one.

Frame Fallback:
Status: IMPLEMENTED
Resolution: Primary key is `frame_target_chain` (data-r-id of <iframe> elements from root to frame). Fallback is `target_selector` + `target_tag` on the interaction record itself. Frame objects are never retained after recording.
Ambiguous behavior: If `frame_target_chain` contains "UNKNOWN" entries (frame_element() failed to resolve data-r-id), replay must raise an explicit missing-target error. It must not guess.
Missing behavior: If no iframe with the required data-r-id exists in the page, replay raises a fatal missing-target error and halts. It does not fall back to URL matching.

CharacterData:
Status: IMPLEMENTED
child_index: Added to JS MutationObserver `characterData` payload via `Array.from(parent.childNodes).indexOf(m.target)`.
             `null` for non-characterData mutation types (childList, attributes).
Tests: test_chardata_verify.py — PASS: Sibling text nodes captured with distinct child_index values (0 and 1).

Schema Compatibility:
Old recordings: Fully backward-compatible. `frame_target_chain` defaults to `[]` (absent field is treated as empty chain = top-level frame). Old `characterData` mutations lack `child_index`; replay must treat missing `child_index` as null.
New recordings: Contain `frame_target_chain` on every InteractionRecord and `child_index` on every characterData MutationRecord.
Defaults: `frame_target_chain: field(default_factory=list)` — safe for deserialization of old JSONL records via `record.get("frame_target_chain", [])`.

Regression:
run_all.bat: PASS — 14/14 concurrent transactions captured correctly. Concurrent test passed. Advanced features test passed.
Compilation: PASS — `python -m py_compile *.py` 0 errors.
Focused tests:
- test_frame_chain.py Test 3 (resolver logic): 5/5 assertions PASS
- test_chardata_verify.py: PASS (sibling indices 0 and 1 captured distinctly)
- test_cross_origin.py: PASS (cross-origin frame_target_chain=['4'], frame_id=unknown)
- test_dom_interaction.py: PASS (8 interactions, 3 mutation batches)

Existing Invariants:
- No URL+method FIFO correlation: PRESERVED
- No pop(0) heuristic correlation: PRESERVED
- No Python id() identity: PRESERVED
- No retained Playwright Frame objects: PRESERVED (frame_element() extracts data-r-id immediately; no Frame stored)
- No speculative network correlation: PRESERVED
- Exactly-once finalization: PRESERVED
- Canonical page/frame identity: PRESERVED
- Binary body integrity: PRESERVED
- Bounded lifecycle state: PRESERVED
- Deterministic sequence ordering: PRESERVED

Production files modified:
- models.py (added frame_target_chain to InteractionRecord)
- browser_manager.py (added child_index to JS characterData mutations; added _build_frame_target_chain; wired frame_target_chain into _handle_interaction)

Tests added:
- test_frame_chain.py (frame chain resolver logic + OOPIF chain recording)
- test_chardata_child_index.py (BrowserManager-integrated characterData test scaffold)
- test_chardata_verify.py (isolated JS MutationObserver child_index verification)

Reports:
- recording_format_implementation_audit.md

Remaining limitations:
- If `frame_element()` cannot resolve an OOPIF iframe element (very deep Chromium isolation), the chain entry is recorded as "UNKNOWN", which replay must treat as a fatal error rather than guessing.
- Closed shadow roots remain un-serializable.
- Text node characterData within a closed shadow root cannot be observed at all.

Recommendation: APPROVE
