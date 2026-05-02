# People-subsystem un-defer criteria

The People subsystem is intentionally deferred. `subsystems/people/` is reserved as a future capability boundary, but no implementation lives there today. CLAUDE.md, ROADMAP.md, and the architecture doc all repeat this position.

The risk is that "should we build People?" gets re-litigated every quarter from scratch. This document captures the criteria once, in the repository, so future-Jack and future-collaborators have a stable, version-controlled answer. It is referenced from the deferred-work section of `docs/ROADMAP.md`.

> **Privacy classification.** When the People subsystem lands, it will inherit raw-personal-data classification. Names, contact handles, relationship metadata, and per-person memory are all raw personal data. The privacy invariant from `CLAUDE.md` and `docs/GUARDRAILS.md` applies: raw content stays local; only summaries reach Claude. Any implementation PR for People must demonstrate this before it merges.

## Concrete usage signals that would un-defer

Each signal below is observable from telemetry that already exists or from daily use of Pepper. None requires new instrumentation. If two or more of these are sustained for at least three consecutive weeks, treat the subsystem as un-deferred and start a design ADR.

- **Repeated person-fact memory writes.** The operator has asked Pepper to remember three or more distinct facts about the same person, three weeks running. Observable from existing memory write logs in PostgreSQL: `SELECT subject, count(*) FROM memory_writes WHERE entity_type = 'person' GROUP BY subject` is a single query against today's schema.
- **Wrong-person reply shipped.** Pepper has produced at least one reply, draft, or summary in production that referred to the wrong person — confused two contacts with the same first name, attributed a message to the wrong sender, or suggested action toward the wrong relationship. A single occurrence is enough; this is a correctness failure, not a frequency one.
- **Same-person, multi-channel comprehension failure.** The operator has asked Pepper a question about a single person ("what did Sarah say about the move?") and gotten an answer that drew from one channel while another channel held the relevant content. Observable from the operator's own day-to-day usage and from semantic-router clarification logs that surface unresolved disambiguation.
- **Contact-name ambiguity is a recurring clarification request.** Pepper repeatedly asks the operator to disambiguate first-name collisions ("which Sarah?") despite memory containing enough context to resolve it. Observable from the existing clarification-request logs in the agent runtime; no new instrumentation needed.
- **Repeated manual cross-channel stitching.** The operator finds themselves hand-summarising context from iMessage *and* email *and* Slack about the same person before asking Pepper a question, because Pepper cannot stitch them. Observable as a daily-use pattern, not a metric, and worth recording in `data/life_context.md` when it happens so the count is real.

These signals exist because the underlying capability gap is real. They do not exist because new logging was added to detect them.

## Out-of-scope signals (do **not** un-defer for these)

The signals below look like People-subsystem demand but are actually demand for a different product. If they appear, the right response is to push back, not to start building People.

- **CRM-shaped requests.** "I want sales pipelines / lead tracking / deal status / outreach cadences." That is a CRM. Pepper is not a CRM, and building People to satisfy CRM workflows would distort the subsystem's purpose.
- **Bulk-contact operations.** "I want Pepper to message every person in cohort X." Pepper sends individual replies in service of a single operator's relationships. Bulk contact features are a different threat model and a different product.
- **Public-people knowledge.** "Tell me about [public figure]." That is web search plus retrieval, which Pepper already has. The People subsystem is for the operator's personal relationships, not for arbitrary humans.
- **Recruiting / vendor / customer pipelines.** Same shape as the CRM signal: a different product with different invariants. Defer to CRM tooling.
- **One-off curiosity.** "I'd like to track when I last spoke with each friend." Nice to have; not a structural gap. The wish belongs in `WISHLIST.md`, not in an un-defer signal.

If the operator finds themselves wanting two or more of these things, it is more likely that they want a CRM (or a separate side project) than that they want Pepper's People subsystem. Document the wish in `WISHLIST.md` and move on.

## Initial shape if un-deferred

The v0 implementation is deliberately small. It is a contact-identity module under `subsystems/people/`, not a full relationship intelligence system. It unifies the same person across iMessage / WhatsApp / Slack / email by reconciling phone numbers, email addresses, and platform handles into a single canonical identity, with manual override available via a gitignored config file. It exposes one tool to the agent — `lookup_person(handle_or_name) → canonical_identity_with_known_channels` — and one write path: when the operator asks Pepper to remember a fact about a person, that fact is associated with the canonical identity, not with a single channel-specific handle. No graph, no relationship inference, no proactive outreach scoring. The subsystem-isolation rule from `docs/GUARDRAILS.md` applies: no imports from other subsystems, no imports from `agent/core.py`.

Beyond v0, the next increments are surfaced by usage: per-person summaries from communication history, last-spoke timestamps, and (only if the multi-channel-comprehension signal stays hot) cross-channel stitching for relationship questions. Each increment should land behind its own ADR so that the subsystem grows by recorded decisions rather than by accretion. None of these increments are committed to in this document — they are listed only so that "v0 is deliberately small" is interpretable, not as a roadmap. The roadmap commitment continues to be: stay deferred until the un-defer signals fire.

## References

- [`docs/ROADMAP.md`](ROADMAP.md) — deferred-work section links to this file
- [`CLAUDE.md`](../CLAUDE.md) — *Relationship to the People Subsystem*
- [`docs/GUARDRAILS.md`](GUARDRAILS.md) — privacy and subsystem-isolation rules the People subsystem will inherit
- [`docs/WISHLIST.md`](WISHLIST.md) — where requests that look like the out-of-scope signals above belong
- Source PR: [#16](https://github.com/jacksteroo/Pepper/issues/16)
