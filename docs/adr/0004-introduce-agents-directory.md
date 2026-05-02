# ADR-0004: Introduce `agents/` directory parallel to `subsystems/`

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Pepper's existing structural rule is that `subsystems/` is the home for *capability boundaries*: People, Calendar, Communications, Knowledge, Health, Finance. Each subsystem is independently replaceable, never imports from another subsystem, and never imports from `agent/core.py`. That rule has held up well in daily code review.

ADR-0001 commits Pepper to a substrate phase whose deliverables include long-running cognitive processes that are not capability subsystems: a reflector that reviews traces nightly, a continuous monitor that compresses memory in the background, and (eventually) on-trigger researchers and similar specialised agents. These cognitive processes have all the properties that motivated the subsystem-isolation rule — they should be independently replaceable, should not entangle with each other, and must not reach into `agent/core.py` — but they are not subsystems. Calling them `subsystems/reflector/` would conflate cognitive specialisation with capability decomposition and erode the meaning of both directories.

Today these cognitive processes have nowhere clean to live. Putting them inside `agent/` blurs them into the orchestrator. Putting them under `subsystems/` overloads that directory's meaning. The OJ-calibration thread surfaced the same tension: a single orchestrator does not scale to inner-life work because reflection, salience scoring, self-modelling, and restraint run on different cadences and need separate prompts and memory windows. Without an explicit home, the substrate phase's deliverables would land scattered.

## Decision

Introduce a new top-level directory `agents/` parallel to `subsystems/`. It is the home for *cognitive functions* — long-running, specialised AI processes that consume traces and produce reflections, compressions, summaries, or research outputs.

Per the Q3 resolution on issue #37, agents run as separate processes — not as in-process daemons inside Pepper Core. The process model decision is recorded there; this ADR ratifies the directory and isolation rule that the processes inhabit.

### Isolation rule

`agents/` carries the same modular discipline as `subsystems/`, with one explicit carve-out for utilities (per the Q7 resolution, 2026-05-02):

1. **No cross-imports between agent modules.** `agents/reflector/` cannot import from `agents/monitor/`. They communicate via the trace store and persisted artefacts (summaries, identity-doc updates), not via shared code.
2. **No imports from `agent/core.py` or any `subsystems/`.** The orchestrator is downstream of cognitive agents; agents must never reach back into it. Subsystem capabilities are reached the same way the orchestrator reaches them — via tool calls / MCP — not via direct Python imports.
3. **Agents MAY import from `agents/_shared/` for utilities only.** Logging setup, config loading, db connection management, pure helpers. `_shared/` is forbidden from holding any state, session info, or shared mutable resource — anything that would create coupling between agents.
4. **Each agent module is independently replaceable.** Swapping the reflector for a different implementation must not require touching any other agent module.

### `agents/_shared/` discipline

The `_shared/` carve-out is a real risk: every project that allows a "shared utilities" directory eventually finds state, session caches, or coordination logic creeping into it. The rule "`_shared/` is utilities only" is therefore enforced by three layered safeguards:

1. **Mandatory docstring rationale.** Every file under `agents/_shared/` must carry a module docstring that names the utility and explains why it is shared rather than living inside a single agent. CI lint rejects PRs that add a `_shared/` file without one. This is the cheapest and most reliable safeguard: every contributor sees the bar.
2. **PR review checklist item.** PR review for any change touching `agents/_shared/` includes the question: *"Does this `_shared/` addition store state, or does it just provide utilities?"* If the answer is "stores state," the change is rejected and the state is moved to its own subsystem with its own MCP contract.
3. **Quarterly audit.** Once per quarter, the `_shared/` directory is reviewed to refactor anything that has drifted toward coupling. Items that have grown state since the last audit are split out as subsystems.

If `_shared/` ever drifts into holding state across agents, the right response is to split that state out as its own subsystem (which gets its own MCP server and explicit contract), not to expand `_shared/` to accommodate it.

### Honest note on the carve-out

During the Q7 deep-walk (2026-05-02), the recommendation was Option A — strict, no `agents/_shared/` directory at all — on the grounds that the discipline cost of keeping `_shared/` clean compounds over years and the safer default is to not have one. Jack picked Option B (looser, with `_shared/`) for ergonomic reasons during the substrate phase. The three safeguards above are what make Option B safe over time. The two-year test is whether `_shared/` ever crosses the state line: if it does, the recovery path is to split state out as a subsystem, not to relax the safeguards.

### Lint enforcement

A lint check enforces both the isolation rule and the `_shared/`-docstring rule. As of this ADR, no such check exists in the repo: the project's lint config is `[tool.ruff]` in `pyproject.toml`, which configures formatting and basic style rules but no import-graph or per-file constraints. The likely home for the new check is one of: a `[tool.ruff.lint.flake8-tidy-imports.banned-api]` block in `pyproject.toml` covering both `subsystems/` and `agents/` boundaries, a small custom checker invoked from CI for the import rules and the `_shared/` docstring rule, or both. This ADR creates the obligation to add the check before the first `agents/` module lands; the choice of mechanism is left to the implementing PR (which lives in #38). The lint check itself is not in this PR — this PR ratifies the rule.

First inhabitants once substrate work begins:

- `agents/reflector/` — nightly trace review, summarisation of failure modes and recurring patterns.
- `agents/monitor/` — long-running memory-compression and salience scoring.
- `agents/researcher/` — on-trigger multi-hop investigation; lower priority, may not land in the substrate phase.

`agent/` (singular, no trailing `s`) is unchanged: it remains the orchestrator and its supporting code. `subsystems/` is unchanged: it remains capability boundaries (Calendar, Communications, Knowledge, Health, Finance, eventually People). `agents/` (plural) is new: cognitive specialisation.

## Consequences

**Positive.**

- Substrate-phase deliverables (reflector, monitor) have a structural home that keeps the orchestrator from accreting cognitive functions.
- The isolation rule that has worked for `subsystems/` is reused, not reinvented. Reviewers already understand the shape of the boundary.
- The directory naming (`agent/` for the orchestrator, `agents/` for cognitive specialists, `subsystems/` for capabilities) creates three coexisting concepts that are visibly distinct.
- ADR-0002's "compounding capability" principle gets a concrete structural anchor: self-improvement processes live somewhere, in their own modules, with their own boundaries.

**Negative.**

- A new top-level directory expands the project's surface and adds a concept new contributors must learn. The mitigation is keeping the rule mostly identical to `subsystems/` so the cognitive load is "another boundary you've seen before, with one explicit utilities carve-out."
- Lint enforcement is now an obligation. The first PR that adds an `agents/` module (#38) must also bring the lint check covering import boundaries *and* the `_shared/` docstring requirement, or the boundary erodes immediately.
- The `_shared/` carve-out is a discipline cost, not a free lunch. It has to be re-defended on every PR that touches it, plus a quarterly audit. The cost is real; the safeguards are designed to make it sustainable rather than to eliminate it.

**Neutral.**

- Existing code under `agent/` and `subsystems/` is untouched. This ADR creates a directory rule, not a migration.
- The process model (separate processes per agent) is not chosen here — it is recorded in the Q3 resolution on issue #37. This ADR ratifies the directory and isolation rule that those processes inhabit.

## Alternatives considered

- **(A) Strict — `agents/` with no `_shared/` directory at all.** Rejected (see *Honest note on the carve-out*, above). Reason: this was the lower-risk option over a multi-year horizon — every project that allows a `_shared/` directory eventually finds state creeping into it, and starting without one would have eliminated that failure mode at zero discipline cost. It was rejected in favour of Option B for ergonomics during the substrate phase: requiring every agent to redo logging setup, config loading, and db connection plumbing inside its own module would slow the first three or four agents, with that cost falling almost entirely on a small number of contributors. The three safeguards (mandatory docstring, PR-review checklist, quarterly audit) are explicitly the price of choosing B. If a future audit finds `_shared/` has crossed the state line and the safeguards are not holding it back, the right escalation is to revisit this ADR and adopt A — not to relax the safeguards.
- **Status quo: keep cognitive processes inside `agent/`.** Rejected — that is the path that produces a single orchestrator stuffed with reflection, monitoring, salience scoring, and restraint, all sharing one prompt and one memory window. The OJ-calibration thread documents why that pattern produces incoherent agents. Keeping the directory clean before the work starts is far cheaper than untangling it later.
- **Place cognitive processes under `subsystems/` (e.g., `subsystems/reflector/`).** Rejected — `subsystems/` means *capability boundaries*. Adding cognitive functions there overloads the meaning of the directory and weakens the existing isolation rule for both. Two clear concepts beats one ambiguous one.
- **Use a `runtime/` or `cognition/` directory instead of `agents/`.** Rejected — `agents/` matches the language used internally and in the calibration thread, and matches industry usage (OpenJarvis archetypes, Generative Agents). New names would need new explanations.
- **Defer the directory decision until the first cognitive process is built.** Rejected — the structural decision drives the implementation. Building the reflector first and *then* deciding where it lives produces churn (rename, re-import, re-lint) and risks the first implementation accreting cross-cuts that the isolation rule would have prevented.

## References

- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §4 "Single orchestrator may not scale to inner life"; cognitive-specialization rule open question
- [Agent Pepper hub](https://www.notion.so/jacksteroo/Agent-Pepper-353fb7367390806a88addf0430118d34)
- [ADR-0001](0001-resequence-around-oj-calibration.md) — substrate phase deliverables that will populate `agents/`
- [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) — compounding-capability principle that `agents/` modules instantiate
- [docs/GUARDRAILS.md](../GUARDRAILS.md) — existing subsystem boundary rules that the `agents/` rule mirrors
- [ADR-0000 template](0000-template.md)
- Source PR: [#14](https://github.com/jacksteroo/Pepper/issues/14)
