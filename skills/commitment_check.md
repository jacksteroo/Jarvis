---
name: commitment_check
description: Surface open commitments and promises — especially those overdue or about to slip
version: 2
---

## Workflow

1. Call `search_memory` with query `"commitment promised will follow up will send will intro will reach out by"` (limit 20). Semantic search will pull the right things — keep the query short and natural, not an OR-soup.

2. Filter the results:
   - Skip anything starting with `[RESOLVED]` — it is done.
   - If a commitment includes an explicit due date ("by Friday", "next week", a date), use that to classify urgency. Otherwise fall back to age:
     - **Overdue**: explicit due date has passed, OR no due date and older than 72 hours
     - **Due soon**: explicit due date within 48 hours
     - **Recent**: made within last 72 hours and no explicit due date

3. For each item, show on one line:
   - What was promised (plain language, not a quote)
   - When made or due (relative: "promised 3 days ago", "due Friday")
   - One concrete next action (e.g., "Send the intro email to X", "Reply to Y's question about pricing")

4. If a commitment is clearly done based on later memory entries (e.g., the intro email was sent), call `mark_commitment_complete` to clean it up before reporting — don't make the user chase ghosts.

5. If there are no pending commitments: say "No open commitments found." and stop.

6. Otherwise close with the count and the most-urgent item:
   "You have N open commitment(s). Most urgent: [description] — [why it's urgent]."

Do not invent commitments. Only surface what appears in memory. If two memory entries describe the same commitment, dedupe — don't double-count.
