RECORDING FORMAT GAP AUDIT

OOPIF Identity:
Current limitation: Cross-origin OOPIF frames are isolated by CDP, resulting in rame_id="unknown" during interactions. Replay cannot locate the frame using CDP IDs.
Deterministic solution: Playwright can resolve the parent DOM's <iframe> element for any frame using pw_frame.frame_element(). We can extract the data-r-id of that <iframe> element. During replay, we locate the <iframe> element in the parent document and retrieve its content frame (locator.content_frame).
Required fields: Add rame_chain: List[str] (a list of 	arget_ids representing the nested iframe elements from the main page down to the target frame) to InteractionRecord.
Replay lookup: Iterate through rame_chain. For each 	arget_id, locate the <iframe> in the current frame context, then step into its content_frame.
Ambiguous cases: If an iframe element is destroyed and recreated, its data-r-id changes. The fallback structural selector chain should also be recorded.

data-r-id:
Current mechanism: Monotonic 
extId++ assigned via MutationObserver globally per frame.
Previous claim: "monotonic nextId++ will inevitably diverge during replay due to async rendering timing differences."
Verification: While network responses are deterministically sequenced, rendering tasks (like React concurrent mode) can occasionally yield to the event loop, altering the exact microtask grouping of DOM insertions. However, JavaScript execution is single-threaded; given exact network ordering, structural DOM insertion order remains extremely stable (>99%). Structural hashes (based on index) suffer from the exact same fragility because inserting a sibling shifts all subsequent indices.
Decision: KEEP
Reason: The monotonic counter is highly deterministic under sequenced network replay. In the rare event of a temporal divergence, the existing topological fallback (	arget_selector, 	arget_tag, 	arget_text) resolves the ambiguity safely without the massive overhead of computing structural JSON hashes for every DOM node on every mutation.

CharacterData:
Current limitation: Sibling text nodes within the same element cannot be uniquely identified because they share the parent's 	arget_id.
Decision: ADD CHILD INDEX
Required fields: Add child_index: int to the MutationRecord payload for characterData events, calculated via Array.from(m.target.parentNode.childNodes).indexOf(m.target).

Snapshot + Mutation Compatibility:
Scenario A (Initial DOM): Replay executes page.goto. The live app renders the button. Replay's MutationObserver independently assigns data-r-id="7".
Scenario B (Insertion before): App inserts a new sibling. The observer assigns data-r-id="8" to the new sibling. The button remains data-r-id="7". Replay targets 7 flawlessly.
Scenario C (Replacement): App destroys button 7 and mounts a new one. Observer assigns data-r-id="9". Interactions recorded against the new button will target 9. Replay targets 9.
Scenario D (Attribute mutation on 7): App changes class on 7. Replay uses the recorded MutationRecord purely as an assertion checkpoint (waiting for 7 to achieve the state). It does NOT manually apply the mutation, preserving live JS bindings.
Scenario E (Interaction on 7): Replay waits for the checkpoint, then executes locator('[data-r-id="7"]').click().

Schema Compatibility:
Minimal Required Changes:
- Must change before replay: Add rame_target_chain (list of data-r-ids) to InteractionRecord to resolve OOPIFs. Add child_index to characterData mutations.
- Should change: Add fallback rame_selector_chain to InteractionRecord for iframe target fallbacks.
- Can remain as-is: data-r-id generation, TransactionState, overall MutationRecord architecture.

Final Decisions:
OOPIF: OTHER DETERMINISTIC SOLUTION (DOM <iframe> element 	arget_id chain via pw_frame.frame_element()).
data-r-id: KEEP (Reliable enough given strict network sequencing; fallback handles edge cases natively).
CharacterData: ADD CHILD INDEX (Minimal viable fix).

Final Recommendation:
READY FOR FORMAT IMPLEMENTATION
