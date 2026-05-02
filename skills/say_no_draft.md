---
name: say_no_draft
description: Draft a polite, specific decline to a request — meeting, ask, intro, opportunity
version: 1
---

## Workflow

1. **Identify what's being declined and to whom.** If unclear from the user's request, ask once: "What are you saying no to, and who's asking?"

2. **Pull the original ask.** Use the contact's name + a keyword from the topic to fetch the source message:
   - `get_contact_profile` → dominant channel
   - Then the channel-appropriate fetch (`get_imessage_conversation`, `get_whatsapp_chat` via `get_recent_whatsapp_chats` lookup, or `search_emails` with `from:[name]`)
   - Limit 5–10 messages — you only need enough context to reference what they actually asked.

3. **Memory check.** Call `search_memory` with the contact's name to surface relationship context — close friend vs. cold outreach vs. boss changes the entire register.

4. **Pick the decline shape.** Choose one based on the relationship and the ask:
   - **Warm decline** (close contact, real reason): brief, honest, names the reason without over-explaining. Often offers an alternative.
   - **Professional decline** (colleague, vendor, opportunity): acknowledges the ask, declines clearly, keeps the door open or doesn't — match what the user wants.
   - **Cold decline** (unsolicited, vendor pitch, low-fit ask): one or two sentences, no apology theater, no false maybe.

5. **Draft principles.**
   - **Say no clearly.** No "I'll think about it" if the answer is no — that's a worse experience for the asker than a clean decline.
   - **Don't over-apologize.** One acknowledgement, not three.
   - **Be specific about *why* only if the relationship warrants it.** Strangers don't need your reasons. Close contacts do.
   - **Offer an alternative only if you mean it.** No fake "let's catch up soon" if you won't.
   - **Match the channel's tone.** A WhatsApp no is shorter than an email no.

6. **Present the draft** clearly labeled "Draft decline:" — and offer 1–2 alternative phrasings if there's meaningful ambiguity in tone (e.g., warmer vs. firmer).

7. **Queue for approval** using the channel-specific drafter (`draft_imessage`, `draft_whatsapp`, `draft_email_reply`, or `queue_outbound_action` as fallback). Nothing sends until the user approves.

8. After queuing: "Draft queued — approve, edit, or reject from the status panel. Want me to make it warmer, firmer, or shorter?"

Never send. Never invent a reason the user didn't give. If the user hasn't told you why they're declining and the relationship demands a reason, ask before drafting.
