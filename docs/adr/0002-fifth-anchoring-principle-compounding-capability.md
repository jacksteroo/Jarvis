# ADR-0002: Add fifth anchoring principle — compounding capability

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Pepper has four anchoring principles: privacy-first, sovereignty, additive memory, pluggable subsystems. Each is a *forbidding* — a constraint on what the system cannot do. That shape is what makes them load-bearing at PR-review time: a reviewer can point at one and reject a change that violates it without re-litigating the philosophy.

ADR-0001 commits Pepper to a substrate-phase plan whose deliverables — trace store, hybrid retrieval, reflection runtime, learned routing — only make sense if the system *learns from its own behaviour*. Workstream E05 (DSPy/GEPA-style prompt and router optimization on local traces) is the concrete implementation of that learning loop. E05's machinery already implies a posture: that self-modification is allowed, but only in artifacts a human can read, version, and revert. That posture is not currently written down anywhere.

The gap is dangerous in both directions:

- Without an explicit anchoring principle, future contributors will second-guess legitimate learning loops within E05's bounds, treating any self-modification as a privacy or sovereignty risk and slowing the substrate phase.
- Without an explicit *forbidding*, the same future contributors will accidentally drift past those bounds — into local fine-tuning of weights, opaque adapter merges, or any "the agent rewrites itself in ways no one can diff" pattern — without realising they have crossed a line.

The OJ-calibration thread surfaces this directly: *compounding context* (additive memory) is in the existing principles, *compounding capability* is not, and the gap shows up everywhere it matters. The principle has to be added now, before substrate-phase PRs land, so that E05 and its successors are reviewable against an explicit anchor instead of being argued from first principles each time.

## Decision

Add a fifth anchoring principle to the project's stated invariants. The wording is deliberately a *forbidding*, to match the shape of the existing four:

> **Compounding capability.** Self-improvement is allowed only through versioned, inspectable, reversible artifacts (prompts, strategies, identity diffs). The system must not modify its own behavior through opaque weight updates or any mechanism that cannot be diffed, inspected, and reverted by hand.

Operationally, this principle means:

- Pepper records structured behavioural traces of her own actions and outcomes.
- Periodic reflection processes consume those traces and surface recurring failure modes, useful patterns, and proposed adjustments.
- Optimization processes (DSPy-style or equivalent) tune routing decisions, prompts, skill selection, and identity-doc deltas from local traces — and only those artifacts.
- Every change produced by self-improvement is committable, diffable, and revertable by the operator. No silent rewrites, no opaque weight updates, no in-place mutation of behaviour that cannot be reconstructed from a versioned artifact.

The principle is concrete and operationally testable: at PR review, the question is *"can a human read, version, and revert this change?"* If yes, the change is within bounds. If no, the change is rejected.

**The bet this principle makes.** That prompt / strategy / identity-diff-level optimization is enough — that we do not need local fine-tuning of small models to hit Pepper's goals. If local fine-tuning of small models becomes a routine, debuggable practice in the next two years, this principle would block it. That is intentional. If the bet looks wrong, the principle gets re-litigated as a future ADR; it does not get silently bent.

This principle is not in conflict with the existing four. Privacy and sovereignty are preserved because optimization runs locally on local traces; only optimized prompts (artifacts, not raw data) ever leave the machine, and only when the operator chooses to ship them. Additivity is preserved because traces are append-only and optimized prompts are versioned. Pluggable subsystems are preserved because trace collection and reflection live in their own modules, not inside `agent/core.py`.

`CLAUDE.md`'s Pepper Core Principles list is updated in this PR to carry the same wording verbatim, as principle #6. The Notion *Agent Pepper* hub's *Anchoring Principles* section is updated alongside, with identical wording, so the two surfaces never drift. `docs/PRINCIPLES.md` is the broader, operational-principles document and is intentionally not modified here — its scope (data sovereignty, durability, versioning, etc.) is wider than the CLAUDE.md anchor list, and folding "compounding capability" into it without restructuring would dilute both.

The bound is enforced by `docs/GUARDRAILS.md`, which takes precedence over any ADR including this one — any self-improvement feature that breaches the bound is rejected at code review per the existing GUARDRAILS precedence rule.

## Consequences

**Positive.**

- Substrate work (trace store, reflection runtime, learned routing, E05) is now anchored to a stated principle, not argued ad hoc.
- The principle is operationally testable at PR review: *can a human read, version, and revert this change?* That is a sharper bar than "is this human-reviewable?" and harder to game.
- The forbidding shape matches the existing four principles, so reviewers already know how to apply it. It plugs into the existing review reflex rather than asking reviewers to learn a new pattern.
- Future ADRs that propose feedback loops, learning components, or DSPy/GEPA-style optimization can cite this principle directly instead of re-justifying the entire posture.

**Negative.**

- The "five anchoring principles" framing must now be reflected in onboarding docs, `CLAUDE.md`, the Notion hub, and any future architecture write-ups. Doc churn is a one-time cost.
- The principle creates an obligation: any self-improvement feature must demonstrate inspectability and reversibility, which is a real engineering requirement (versioning, diff surfaces, rollback).
- The bet on artifact-level optimization is real. If small-model fine-tuning matures into a routine, debuggable practice within Pepper's lifetime, this principle would block adoption until a successor ADR re-litigates the posture. That is the cost of forbidding a technique now to keep the substrate clean.

**Neutral.**

- The four existing principles are unchanged in scope and meaning.
- The principle does not pick a specific implementation (DSPy, GEPA, lighter-weight prompt evolution, learned routers). It commits only to the *shape* of the invariant.

## Alternatives considered

- **(A) Original enabling wording — *"the system improves itself in response to its own behaviour, within human-reviewable bounds"*.** Rejected. Two reasons. First, it does not match the *shape* of the existing four anchoring principles, which are forbiddings, so it would not plug into the same review reflex. Second, "within human-reviewable bounds" is softer than the operationally-testable bar in the ratified wording: it authorises self-modification in general and asks reviewers to draw the line case-by-case, where the ratified wording forecloses opaque weight updates outright. The enabling wording would not have ruled out the failure mode it intended to.
- **(B) Status quo: keep four anchoring principles, treat self-improvement as a feature category.** Rejected — every substrate-phase deliverable in ADR-0001 (and E05 in particular) ends up arguing the same posture from first principles. Without an explicit principle, a single reviewer's intuition becomes the bar, and the substrate phase ships unevenly.
- **(C) Reject the principle entirely; do not add a fifth.** Rejected. This was the most credible alternative. The argument: four crisp principles are easier to remember than five, and the substrate phase can be reviewed against privacy-first and sovereignty alone. The argument fails because privacy-first only forbids data exfiltration, and sovereignty only forbids cloud dependence — neither speaks to the *shape* of behaviour-mutation, which is the actual concern. Without compounding capability, the substrate phase has to invent a new principle in its first PR review and re-justify it forever after. The cost of stating it once now is much smaller.
- **(D) State the principle in `docs/ROADMAP.md` rather than as an anchoring principle.** Rejected — roadmap items rotate; anchoring principles do not. Compounding capability is an invariant about *how the system works over time*, not a phase deliverable.
- **(E) Adopt OpenJarvis-style autonomous learning loops without an anchoring principle.** Rejected — that posture (sensible defaults, opt-out) is a framework's posture. Pepper is a single-operator product and has chosen "you cannot opt out without breaking a test" as its enforcement model. Importing the loop without importing the posture would create a hidden divergence from the existing principles.

## References

- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §3 "No learning loop — Pepper is static between commits"
- [Agent Pepper hub](https://www.notion.so/jacksteroo/Agent-Pepper-353fb7367390806a88addf0430118d34) — *Anchoring Principles* section is updated alongside this ADR
- [docs/PRINCIPLES.md](../PRINCIPLES.md) — canonical list of operational principles (intentionally not modified here)
- [ADR-0001](0001-resequence-around-oj-calibration.md) — substrate phase that this principle anchors
- [ADR-0000 template](0000-template.md)
- Q8 resolution (forbidding-shape wording), 2026-05-02
- Source PR: [#12](https://github.com/jacksteroo/Pepper/issues/12)
