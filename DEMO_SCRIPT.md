# Demo script (≤ 3 minutes)

A click-by-click narration for the demo video. Start with the app running at
`http://localhost:8000` and the six sample PDFs already seeded. Record at 1280×800+
and zoom the browser to ~110% so figures are legible.

---

**0:00 — What this is (15s)**

> "This is a Fact Knowledge Layer. It reads PDFs, turns numbers into _qualified facts_ —
> each tied to its exact spot on the page — and reconciles them across the knowledge base.
> Fully offline; a Groq LLM pass is optional."

Point at the tally: **14 corroborations · 1 contradiction · 25 reconciliations ·
405 grounded facts · 6 documents.**

**0:15 — Case 2, a genuine contradiction (25s)**
Click the **Contradictions** tile.

> "Same IPO offer on page 1 of the prospectus — ₹40,000 million and ₹12,350 million, both
> marked _subject to finalization_. Same metric, period, scope; sixty-nine percent apart;
> nothing explains it — a genuine contradiction."

Point at the two **evidence crops** (the real PDF regions) and read the **Why**.

**0:40 — Case 3, reconciliations by context (35s)**
Click the **Reconciliations** tile.

> "EBITDA ₹127 crore versus ₹76 crore, FY24, both on page 6 — reported versus adjusted.
> Reconciled by **definition**, and the engine names that dimension."

Scroll one card.

> "₹127 crore full-year versus ₹46 crore for Q4 — same metric, different **period**.
> Reconciled, not contradicted. Units work the same way: ₹8,142 crore equals ₹81,415
> million after conversion."

**1:15 — Case 1, corroboration expressed differently (20s)**
Filter to **Corroborated**, find **revenue**.

> "FY24 revenue as `₹8,142 Cr` on page 6, and as a bare `8,142` in the services table on
> page 17 — different representations, same value after normalization. Corroborated."

**1:35 — Annotated documents + download (35s)**
Open the **Annotated documents** tab; pick the prospectus.

> "Here's the source page itself, highlighted — red for contradiction, amber for reconciled,
> green for corroborated. Hover a highlight…"

Hover the red box (tooltip shows the two values + reason).

> "…and it explains the flag: the two conflicting values and why. You can download this as a
> real annotated PDF — the highlights and reasoning travel with the file."

**2:10 — Upload merges incrementally (20s)**
Click **Add a PDF**, choose one.

> "New PDFs merge into the same layer, re-reconciling only the metrics they touch —
> incremental, no full rebuild."

_(Timing tip: if seeding is slow, upload one just before recording and narrate the merge,
or cut straight to the test suite below.)_

**2:30 — Proof it works: the test suite (25s)**
Cut to a terminal showing `python test_system.py` already run (or run it live).

> "And it's verified end-to-end. An offline test suite seeds an isolated database and runs
> thirty-four checks — every API endpoint, the unit-normalization math, all four required
> cases, incremental de-duplication, and that every fact is grounded in its evidence."

Point at the final line: **`RESULT: 34/34 checks passed — ALL PASSED ✅`**

**2:55 — Close (5s)**

> "Everything you saw is produced live by rules from the facts — no hard-coded answers.
> Thanks for watching."

---

### Optional B-roll to have ready

- The **Facts** tab (searchable, 405 grounded facts) — a 2-second pan if you have spare time.
- The console line `LLM extractor: False` at startup — flash it once to prove it's offline.
- `test_system.py` output on screen (see the "Proof it works" beat) — the 34/34 result reads
  strongly on camera.
