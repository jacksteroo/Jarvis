# ADR-0001: Re-sequence around OJ-calibration third option

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

The original ROADMAP frames the next strategic move as a binary choice between two options: pause Phases 4–6 (Knowledge / Health / Finance subsystems) and pivot to inner-life work, or push on with subsystem expansion as drafted. The active gating thread treats this as the live debate.

Calibrating Pepper's architecture against OpenJarvis (Stanford Hazy Research's local-first personal-agent stack) made it clear that both branches of that debate quietly assume an adequate substrate that does not exist today:

- There is no behavioural trace store. Logs exist, but agent self-consumption needs structured traces.
- There is no reflection runtime — the inner-life threads (reflection loop, identity doc, strategy hub, wait-action feedback) all assume Pepper can review what she did, and she structurally cannot.
- Retrieval is pure pgvector HNSW. Comprehension failures observed in daily use look like memory-query failures, not source-coverage gaps.
- The router and prompts are statically tuned. There is no mechanism for the system to improve between commits.

Adding more subsystems on top of this substrate compounds the comprehension problem. Building the inner-life moves on top of this substrate produces vibes-based reflection with no ground truth. The binary is false: both paths get more expensive without the substrate, and both get cheaper with it.

## Decision

Pause original ROADMAP Phases 4–6 (Knowledge / Health / Finance subsystems). Adopt a three-stage sequence:

1. **Substrate** — trace store, hybrid retrieval, reflection runtime v0, learned routing/prompt optimization. Targeted at one quarter of single-operator work.
2. **Inner life** — reflection loop, identity doc, Strategy Hub, wait-action with trace-grounded feedback. Tractable only after substrate lands.
3. **Subsystem expansion** — Knowledge / Health / Finance, on top of a fundamentally smarter Pepper rather than a comprehension-bottlenecked one.

Phases 4–6 of the original roadmap are not cancelled, only re-sequenced. They return as part of stage 3. Capability work moved to `WISHLIST.md` stays there until pulled forward by usage signals, per the existing roadmap principle.

## Consequences

**Positive.**

- Both originally-debated paths become cheaper. Inner-life moves stop being aspirational once a trace substrate exists. Subsystem expansion lands on a smarter foundation.
- The active gating thread is resolved by an explicit decision rather than left open across quarters.
- Comprehension regressions in daily use get a mechanical path to root-cause via traces, instead of relying on operator intuition.
- Subsequent ADRs (`ADR-0003` Layer 2 reprioritization, `ADR-0004` `agents/` directory) sit cleanly under this sequencing decision.

**Negative.**

- Knowledge / Health / Finance subsystems do not ship in the next two quarters. If a high-value capability gap surfaces in those domains during that window, it will be felt.
- Substrate work is plumbing. There is no user-visible feature for several weeks. This is expected but real.
- The wishlist gets longer before it gets shorter. Items deferred from the original Phase 4–6 may need re-justification when stage 3 begins.

**Neutral.**

- Subsystem boundaries (`subsystems/people/`, `subsystems/calendar/`, etc.) are unaffected. New work happens in `agents/` and a new trace-store module; existing modules are untouched.
- The four anchoring privacy / sovereignty / additive-memory / pluggable-subsystem principles are unaffected; this ADR is about ordering, not boundaries.

## Alternatives considered

- **Status quo: keep the original Phases 4–6 sequence.** Rejected — both that path and the inner-life pivot assume substrate Pepper does not have. Building Knowledge / Health / Finance now means feeding more sources into an agent whose comprehension bottleneck is at Layer 2, not Layer 1.
- **Pivot directly to the four inner-life moves (reflection, identity, strategy, wait-action) without substrate work first.** Rejected — without traces, "reflection" reduces to fuzzy LLM self-prompting with no ground truth. Wait-action feedback explicitly requires a trace store to compare wait outcomes against act outcomes. Skipping substrate makes inner-life work hallucinate confidently and unverifiably.
- **Switch foundations to OpenJarvis itself.** Rejected — OJ is a framework optimized for many users; Pepper is a single-operator product with hard privacy invariants. Adopting OJ as kernel dilutes the operator-legibility property that is already load-bearing. Vendor patterns, not the framework.
- **Do substrate and Phases 4–6 in parallel.** Rejected — single-operator throughput. Doing both halves both. The substrate sequencing is the load-bearing decision and deserves the focus.

## References

- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §6 "The active gating thread is a false binary"
- [Agent Pepper hub](https://www.notion.so/jacksteroo/Agent-Pepper-353fb7367390806a88addf0430118d34)
- [ADR-0000 template](0000-template.md)
- Related: ADR-0002 (compounding capability principle), ADR-0003 (Layer 2 active surface), ADR-0004 (`agents/` directory)
- Source PR: [#11](https://github.com/jacksteroo/Pepper/issues/11)
