---
name: decision_log
description: Capture a decision with its rationale, alternatives, and date — so it can be retrieved later
version: 1
---

## Workflow

1. **Identify the decision.** From the user's message, extract:
   - **What**: the choice being made (e.g., "going with Postgres over SQLite for Pepper's storage")
   - **Why**: the reasoning, in the user's own words
   - **Alternatives considered**: what was rejected and why (if mentioned)
   - **Reversibility**: is this a one-way door or a two-way door?
   - **Stakeholders / scope**: who or what does this affect?
   - **Date**: today, unless the user says otherwise.

   If any of these are missing AND meaningfully ambiguous, ask one consolidated question. Don't interrogate — only ask if you can't write a useful record without it.

2. **Format the entry.** Use a consistent structure so future retrieval is reliable:

   ```
   DECISION (YYYY-MM-DD): [one-sentence what]

   Why: [the actual reasoning, not platitudes]
   Alternatives considered: [what was rejected and why, or "none discussed"]
   Reversibility: [one-way / two-way / unclear]
   Scope: [what this affects]
   ```

3. **Save it.** Call `save_memory` with the formatted entry. The `DECISION (date):` prefix is the retrieval anchor — keep it consistent across all decisions so `search_memory("DECISION")` always pulls the full log.

4. **Cross-reference open commitments.** If this decision closes or supersedes an existing commitment in memory, call `mark_commitment_complete` on that one. If it creates a new follow-up ("we'll revisit in Q3"), surface that to the user and offer to file it as a separate commitment entry.

5. **Confirm back.** Show the user exactly what was saved — verbatim. They should be able to spot-check the wording before it lives in memory forever. "Filed. Want me to adjust the wording?"

6. **Optionally update life context.** If this decision represents a lasting shift in how the user operates, thinks, or works (not a one-off project call), call `update_life_context` with the relevant section. Be conservative — most decisions are project-level, not life-level.

Never paraphrase the *why* into something the user didn't say. The whole point of a decision log is that future-you can trust past-you's actual reasoning, not Pepper's gloss on it.
