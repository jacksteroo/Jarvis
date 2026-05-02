# System Prompt Audit — Runtime-Generation Candidates

**Issue:** [#35](https://github.com/jacksteroo/Pepper/issues/35)  ·  **Parent epic:** [#31](https://github.com/jacksteroo/Pepper/issues/31) (Context Assembly Subsystem)
**Date:** 2026-05-02

## Purpose

Phase 6.7 introduced the **capability-block pattern** — instead of hand-writing the "Your available capabilities" section into `SOUL.md`, the prompt now derives that block at turn time from the live `CapabilityRegistry` (`build_capability_block` in `agent/life_context.py`). The model sees only what is genuinely live, with status notes ("not configured", "permission required: …") attached to each source. That swap materially improved comprehension and removed a class of "claim-vs-reality" drift between the prompt and the actual tool surface.

This audit walks the rest of the system prompt with the same lens. Every distinct chunk that ends up in the `system` field of the LLM call (`agent/core.py:3163`) is enumerated and classified.

## Methodology

The system prompt is assembled in two stages:

1. **Cold-start (once per process)** — `build_system_prompt` (`agent/life_context.py:246-297`) concatenates SOUL, schedule, capability block, life context, and a couple of static guardrails. The result is cached on `self._system_prompt` (`agent/core.py:1930`, also rebuilt after `update_life_context`).
2. **Per-turn** — `agent/core.py:2980-3154` prepends a time header and an interface tag, appends seven runtime context blocks (memory/web/routing/calendar/email/iMessage/WhatsApp/Slack), conditionally appends the GROUNDING RULES block on heavy turns, and finally appends the skills index.

Each chunk below is classified as one of:

- **Static** — hand-written prose that doesn't depend on per-process or per-turn state. Keep as-is.
- **Runtime-generatable** — could be derived from registries, config, schedulers, life-context structure, or trace data at build time. Candidate for refactor.
- **Optimizer-tunable** — wording is load-bearing for behavior and should evolve via E5 (DSPy/GEPA) rather than hand edits.

A chunk can be primarily one classification while having secondary candidates noted in "Follow-up".

## Inventory

| # | Chunk | Source (file:line) | Classification | Rationale | Follow-up |
|---|-------|---------------------|----------------|-----------|-----------|
| 1 | `SOUL.md` Identity / Voice / Priorities / How You Think | `docs/SOUL.md:1-60`, loaded by `life_context.py:17-28`, embedded at `life_context.py:283` | **Static** | This is the persona definition — Pepper's identity, voice, owner relationship. Should not float per turn. Text refinements belong in version control with PR review. | None. Possibly **Optimizer-tunable** in the long run for sub-sections like "How You Think", but only after we have rubric-graded eval data. |
| 2 | `SOUL.md` Memory & Context Rules | `docs/SOUL.md:64-72` | **Static** | Tool-usage policy for `search_memory` / `save_memory` / `update_life_context`. Tied to actual tool semantics. | None. |
| 3 | `SOUL.md` Response Format Rules — FORBIDDEN closing phrases | `docs/SOUL.md:78-92` | **Optimizer-tunable** | Pure behavior-shaping list, grown reactively from observed failures. Exactly the kind of artifact GEPA should be evolving against a "no sycophancy / no filler" rubric. | Linked to E5 epic #43 (DSPy/GEPA Optimization Loop). Issue #46 already covers context-assembly prompts; format rules are a sibling target. |
| 4 | `SOUL.md` FORBIDDEN meta-commentary phrases | `docs/SOUL.md:94-117` | **Optimizer-tunable** | Same shape as #3 — a denylist of leak phrases ("based on the provided information", "the life context says", …) accreted from real failures. Should be grown automatically from trace-store evidence rather than hand-edited. | Same as #3 — feed into E5. Cross-reference: [#41 Recurring failure-mode detection](https://github.com/jacksteroo/Pepper/issues/41) is the natural producer of new entries. |
| 5 | `SOUL.md` Second-person / abbreviation / travel attribution rules | `docs/SOUL.md:119-125` | **Static** | Style invariants tied to Jack's identity and family naming, not behavior we want to evolve via optimization. | None. |
| 6 | `SOUL.md` "What You Never Do" / "What You Always Do" | `docs/SOUL.md:129-156` | **Static** | Values + safety. Hand-curated by intent. | None. |
| 7 | `SOUL.md` Domain Rules — Family Logistics, Trip & Travel, Pre-College, Open-Loop Status | `docs/SOUL.md:160-201` | **Runtime-generatable** (partially) | Several sub-rules name *specific entities* — "Matthew is confirmed for Harvard pre-college Quantum Computing (starts June 22)", "Four Points Sheraton, dates July 7–10", "POA/Taiwan-Malaysia insurance" — that are facts about the current life state, not timeless persona rules. They will go stale and currently require hand edits to SOUL.md every time the underlying open loop closes. The **rule** is static; the **examples and named entities** should be drawn from `data/life_context.md` at build time. | **Issue created.** Refactor to template these examples from `life_context.py` section parsing (Open Loops, Active Challenges, Kids — Activities) so the rule text auto-refreshes when life context changes. |
| 8 | Schedule block (morning brief / commitment check / weekly review / memory compression times) | `agent/life_context.py:269-279` | **Runtime-generatable** | Already partially runtime-generated from `config.MORNING_BRIEF_HOUR/MINUTE`, `WEEKLY_REVIEW_DAY/HOUR`. But the *list of scheduled jobs* is hard-coded in the f-string while the actual schedule is registered in `agent/scheduler.py`. New jobs added to the scheduler don't appear in the prompt unless someone hand-edits this string — exactly the drift the capability-block pattern is meant to prevent. | **Issue created.** Refactor to enumerate from a single scheduler registry analogous to `CapabilityRegistry`. |
| 9 | Capability block (Calendar / Email / iMessage / WhatsApp / Slack / Memory / Local files / Contacts / Comms Health / Images / Health) | `agent/life_context.py:160-230` | **Runtime-generatable (already done — Phase 6.7)** | This is the reference pattern. Status notes are pulled from `CapabilityRegistry`. | None. Tool names are validated by `validate_prompt_tool_references` against the live registry — keep this guarantee. The *Health* line is still a hand-written "NOT CONNECTED" stanza that should automatically flip when health is wired up; convert to a registry lookup with `CapabilityStatus.NOT_CONFIGURED` like the others. |
| 10 | "When asked if you can read iMessages…" hard-error guardrail | `agent/life_context.py:288` | **Static** | Reinforces capability claims; tied to capability-block structure. | None. |
| 11 | "Health and biometric data … NOT connected and has NEVER been connected" | `agent/life_context.py:290` | **Runtime-generatable** | Mirrors the Health line in the capability block. Once health is registered as a capability source (even with `NOT_CONFIGURED`), this duplicate stanza should be auto-derived from the registry rather than hand-asserted. Hard-coding "NEVER been connected" guarantees prompt drift the moment health *is* connected. | **Issue created.** Same target as #9 Health-line refactor — register health sources in `CapabilityRegistry` and remove the hand-written stanza. |
| 12 | "Your owner's life context: ---\n{context}\n---" | `agent/life_context.py:292-295` (full file from `data/life_context.md`) | **Runtime-generatable (already done)** | Already loaded from disk at process start; rebuilt when `update_life_context` writes back. Whole-file injection is the simplest version of runtime generation. | Possible follow-up: section-level selection rather than whole-file dump (per-turn relevance), but that overlaps with epic #31 context-assembly extraction (#32) and shouldn't be done here. |
| 13 | Stale-deadline regex sanitization | `agent/life_context.py:254-259` | **Runtime-generatable (currently hand-coded patch)** | A regex hard-codes "January/February/March/April 20XX" to rewrite past-deadline phrases. This is a band-aid that only catches one specific phrasing and silently goes stale every January. The right fix is a structured "deadline state" computed from dates in the life context, not regex patching of prose. | Defer — flagged in the audit summary. Scope creep for this issue; revisit when life context gains structured deadline metadata. |
| 14 | Final "Answer questions … directly from the life context above. Only call search_memory when …" instruction | `agent/life_context.py:297` | **Static** | Tool-routing policy. | None. |
| 15 | Per-turn time header — `[Current time: …]` | `agent/core.py:2980` | **Runtime-generatable (already done)** | Pure runtime. Reference pattern. | None. |
| 16 | Per-turn interface tag — `[Interface: You are responding via {channel}.]` | `agent/core.py:2982` | **Runtime-generatable (already done)** | Pure runtime, threaded from chat-turn metadata. | None. |
| 17 | Memory context block — `{memory_context}` | `agent/core.py:2984` | **Runtime-generatable (already done)** | Built per-turn from `search_memory` results. | None — but the *labels* and *ordering* of all eight context blocks (memory/web/routing/calendar/email/iMessage/WhatsApp/Slack) are currently ad-hoc concatenation. Epic #31 calls for extracting this into a named module (#32). |
| 18 | Web context block — `{web_context}` | `agent/core.py:2986` | **Runtime-generatable (already done)** | Per-turn from `search_web`. | See #17. |
| 19 | Routing context block — `{routing_context}` | `agent/core.py:2988` | **Runtime-generatable (already done)** | Per-turn from semantic router decisions. | See #17. |
| 20 | Calendar / Email / iMessage / WhatsApp / Slack context blocks | `agent/core.py:2990-2998` | **Runtime-generatable (already done)** | Per-turn from each respective tool. | See #17. |
| 21 | GROUNDING RULES block (heavy turns only) — owner identity rule, calendar-ground rule, web-ground rule, no-placeholder rule, scoping rules, certainty-preservation rule, second-person rule, Susan career rule | `agent/core.py:3023-3144` | **Optimizer-tunable** with **Runtime-generatable** owner-identity prefix | This 14-rule block is the densest piece of behavior-shaping prose in the prompt. Rules 0, 7, and 13 reference `owner_name`/`owner_first` — those parameterizations are already runtime. The *rule list* itself has the same shape as #3/#4: hand-grown reactively from observed failures. Rule 14 ("Susan's career") is even tighter to current life state and would belong with the life-context-templated rules in #7. The whole block is a prime target for E5 — these are exactly the "context-assembly prompts" issue #46 is about. | **Issue created.** Refactor (a) extract block to a named module under context-assembly (#32), (b) feed it into E5/GEPA optimization (#46). Rule 14 can be removed once #7 lands. |
| 22 | Skills index — `Available skills:\n- name — desc\n…` | `agent/skills.py:288-309`, injected at `agent/core.py:3154` | **Runtime-generatable (already done)** | Built from the live `Skill` list at turn time. Reference pattern. | None. |

## Summary

**Counts by classification (primary class only):**

- **Static:** 6 (chunks 1, 2, 5, 6, 10, 14)
- **Runtime-generatable (already done):** 8 (chunks 9, 12, 15, 16, 17, 18, 19, 20, 22 — counted as one cluster of 8)
- **Runtime-generatable (refactor needed):** 4 (chunks 7, 8, 11, 13)
- **Optimizer-tunable:** 3 (chunks 3, 4, 21)

**Top 3 priorities (queued as follow-up issues):**

1. **Schedule block from scheduler registry** (chunk 8) — direct application of the Phase 6.7 pattern. Smallest blast radius, biggest "this is the same shape" win.
2. **Domain-rule examples templated from life context** (chunk 7) — biggest staleness risk; SOUL.md currently hard-codes named entities ("Four Points Sheraton July 7–10", "Matthew … June 22") that drift the day a trip closes.
3. **GROUNDING RULES extraction + E5 wiring** (chunk 21) — densest behavior-shaping prose; correct home is epic #31 (#32 extraction) feeding epic #43 (#46 optimization). Lowest velocity but highest long-term leverage.

**Items deferred:**

- **Stale-deadline regex (#13)** — flagged but not refactored. Real fix requires structured deadline metadata in the life context, which doesn't exist yet. Revisit when life-context schema gains date fields.
- **SOUL.md sub-section optimization (#1)** — deferred until E5 has rubric-graded eval data; persona text is too load-bearing to optimize blindly.
- **Health stanza dedupe (#11)** — small; rolls into the Phase 6.7 pattern when health is registered as a capability source. Tracked alongside #9.

**Cross-references:**

- Epic [#31](https://github.com/jacksteroo/Pepper/issues/31) — Context Assembly Subsystem (parent of this audit)
- Issue [#32](https://github.com/jacksteroo/Pepper/issues/32) — Extract context-assembly logic into named module (natural home for refactor of chunks 17–21)
- Issue [#33](https://github.com/jacksteroo/Pepper/issues/33) — Make assembly decisions traceable (consumes the named module)
- Epic [#43](https://github.com/jacksteroo/Pepper/issues/43) — DSPy/GEPA Optimization Loop (E5)
- Issue [#46](https://github.com/jacksteroo/Pepper/issues/46) — Apply optimization to context-assembly prompts (consumer of chunk 21 once extracted)
- Issue [#41](https://github.com/jacksteroo/Pepper/issues/41) — Recurring failure-mode detection (natural producer of entries for chunks 3, 4)
