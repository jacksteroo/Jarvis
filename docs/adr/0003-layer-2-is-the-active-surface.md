# ADR-0003: Layer 2 is the active surface

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Pepper's architecture document describes the system as three layers:

- **Layer 1 — Data.** Subsystem ingestors: Gmail, Calendar, iMessage, WhatsApp, Slack, Yahoo, Telegram, plus persistent memory in PostgreSQL + pgvector.
- **Layer 2 — Intelligence.** The agent runtime: orchestration, prompt assembly, retrieval, semantic router, skill system, MCP client.
- **Layer 3 — Presentation.** Telegram, web UI, future macOS shell.

Past planning sequenced "Layer 2 design — pending Layer 1 lock-in," which reads as *finish ingesting all sources before tightening the brain*. That ordering was defensible if the bottleneck on Pepper's daily usefulness were data coverage. It isn't.

In daily use the recurring complaint is *"Pepper doesn't comprehend very well to my asks"* — wrong intent classification, wrong source picked, retrieved memory not surfaced, multi-step tasks falling apart. Each of those failures lives at Layer 2. Adding more sources at Layer 1 cannot fix any of them; it produces a still-confused agent with more data to be confused about. The OJ-calibration thread surfaced this clearly: comprehension is a Layer 2 problem, not a Layer 1 gap.

Layer 1 is not finished, but it is *adequate*. Gmail, Calendar, iMessage, WhatsApp, Slack, Yahoo, Telegram, the memory store and the life context document are all present and queryable today. Stage 3 of ADR-0001 will resume Layer 1 expansion (Knowledge / Health / Finance) once Layer 2 is smarter; until then, Layer 1 churn is at best neutral and at worst a distraction.

## Decision

For the next two sprints, treat **Layer 2 (Intelligence + agent runtime) as the active surface**. Layer 1 is in maintenance: bugs get fixed, sources do not get added.

Active surface means:

- New investments — trace store, hybrid retrieval, reflection runtime, learned routing, context-assembly subsystem — land at Layer 2.
- Comprehension regressions are diagnosed as Layer 2 problems first (router, retrieval, prompt assembly, skill selection) before Layer 1 is suspected.
- Roadmap items that propose new Layer 1 sources during this window are deferred to `WISHLIST.md` until ADR-0001 stage 3.

This decision is bounded: two sprints, then re-evaluated. If during the window Layer 2 work surfaces a concrete Layer 1 gap (e.g., reflection cannot work because trace storage *is* a Layer 1 concern), that gap is unblocked, but only by exception.

## Consequences

**Positive.**

- Investment is concentrated where the observed bottleneck is. Comprehension lift becomes the unit of progress for two sprints.
- Architectural diagrams stop suggesting that Layer 2 work is "pending" — it is the active surface, and the docs should reflect that.
- Layer 1 stability for two sprints means subsystem owners (Calendar, Communications, etc.) can focus on hardening rather than expansion.

**Negative.**

- The macOS desktop direction and any source-coverage gaps surfaced by daily use during this window will not be addressed until stage 3.
- The principle of "build-ahead-of-need is forbidden" (existing roadmap principle) creates tension: if a new Layer 1 source surfaces a real need mid-window, the bound on this ADR may force a hard call.

**Neutral.**

- The three-layer framing is preserved. This ADR ranks layers by activity, not by importance.
- Coupled rewrite of the architecture doc itself is tracked separately by [#15 — Layer-model vs subsystem-horizontal: pick canonical framing for `docs/ARCHITECTURE.md`](https://github.com/jacksteroo/Pepper/issues/15). That issue captures the choice between layered framing and subsystem-horizontal framing as the canonical organising principle. This ADR records the activity decision for Layer 2; #15 records the framing decision and is expected to lead with the layered framing as a consequence of accepting this ADR.

## Alternatives considered

- **Status quo: continue treating "Layer 1 lock-in" as the gating concern.** Rejected — there is no observable bottleneck at Layer 1 today. Daily comprehension failures all live at Layer 2. The status-quo ordering directs investment away from where the problem is.
- **Treat all three layers as equally active.** Rejected — single-operator throughput. Spreading attention across layers in two sprints means none of them get a meaningful upgrade. The point of choosing an active surface is to concentrate investment.
- **Make Layer 3 (Presentation) the active surface — push the macOS shell first.** Rejected — a better presentation layer over a confused agent is a demo, not a tool. The macOS direction is queued for later, not now.
- **Skip the layer-activity framing entirely; just list features.** Rejected — without an explicit active-surface decision, every PR re-litigates the layer ordering. The cost of stating it once is far below the cost of repeating the debate.

## References

- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §1 "The three-layer model under-weights the Intelligence Layer"
- [Agent Pepper hub](https://www.notion.so/jacksteroo/Agent-Pepper-353fb7367390806a88addf0430118d34)
- [ADR-0001](0001-resequence-around-oj-calibration.md) — substrate → inner life → subsystem expansion sequencing
- [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) — anchors substrate-phase deliverables
- [#15 — pick canonical framing for `docs/ARCHITECTURE.md`](https://github.com/jacksteroo/Pepper/issues/15) — coupled framing decision; captures the choice between layered and subsystem-horizontal as the canonical organising principle
- [ADR-0000 template](0000-template.md)
- Source PR: [#13](https://github.com/jacksteroo/Pepper/issues/13)
