---
name: draft_reply_to_contact
description: Draft a reply to a specific person using recent message history for context
version: 3
---

## Workflow

1. Identify the contact from the user's request. If unclear, ask once: "Who are you replying to?"

2. Call `get_contact_profile` with the contact's name to see their dominant channel and last contact time.

3. Based on dominant channel, fetch recent context (always fetch enough to see *both sides* — your outgoing replies are how Pepper learns your voice with this person):
   - iMessage: `get_imessage_conversation` (limit 15) — already returns both directions.
   - WhatsApp: `get_whatsapp_chat` requires a numeric `chat_id`. First call `get_recent_whatsapp_chats` (limit 30), find the chat whose name matches the contact, then call `get_whatsapp_chat` with that `chat_id` (limit 15). If no matching chat is found, say so and try iMessage instead.
   - Email: call `search_emails` with `from:[name] OR to:[name]` (limit 8) so you see both your replies and theirs.
   - If dominant channel is unclear, try iMessage first, then email.

4. Call `search_memory` with the contact's name to surface relevant history, prior commitments to/from them, and any flagged context (e.g., "Sarah is allergic to scope creep").

5. Check relationship framing — is this a close friend, colleague, boss, family, vendor, or stranger? If you're unsure and the answer would change the tone, call `skill_view("draft_reply_to_contact", ref="references/voice.md")` if available, or quickly grep the life context via `search_memory("relationship with [name]")`.

6. Draft the reply:
   - **Voice**: mirror the user's *outgoing* style from this thread (length, punctuation, formality, emoji use, sign-off). Don't impose a generic helpful-assistant tone.
   - **Specificity**: reference concrete details from the thread — never write something that could be sent to anyone.
   - **Focus**: address the most recent unanswered question or request first. If there are multiple, handle the highest-stakes one and flag the others.
   - **Length**: match the length of *the user's* typical messages with this person, not the contact's.
   - Present the draft clearly labeled: "Draft reply:"

7. Queue the draft using the channel-specific drafter (preferred — they handle the right send tool internally):
   - iMessage → `draft_imessage`
   - WhatsApp → `draft_whatsapp`
   - Email reply → `draft_email_reply`
   - Fallback / unusual case → `queue_outbound_action`

   Nothing is sent until the user approves it from the status panel.

8. After queuing, confirm: "Draft queued for your review — approve, edit, or reject from the status panel. Want me to adjust the tone or content before you approve?"

Never fabricate conversation history. Only draft based on what the tools return. Never bypass the drafter — every outbound message goes through user approval.
