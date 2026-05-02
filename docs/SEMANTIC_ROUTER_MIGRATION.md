# Semantic Router Migration

Replace Pepper's hardcoded regex router (`agent/query_router.py`) with a learned, embedding-based intent classifier that grows from real usage, while keeping deterministic slot extraction (entities, time scope, sources) explicit.

This is the migration plan. The execution routine lives in [SEMANTIC_ROUTER_BUILD_PROMPT.md](SEMANTIC_ROUTER_BUILD_PROMPT.md).

---

## Background

### Failure hypothesis

User-reported symptoms: Pepper repeats wrong answers, misroutes simple retrieval questions, and degrades on novel phrasings of known intents.

Architectural audit (2026-04-26) found:

- Routing is 100% deterministic regex/keyword matching across ~870 lines of `query_router.py`. Any phrasing not in the term tables falls through to "general chat" fallback (confidence 0.6) and the LLM is handed the full 50+ tool menu with no narrowing.
- The router uses `qwen3-embedding:0.6b` (1024-dim, local Ollama) for query and exemplar embeddings. The memory subsystem keeps its own pgvector store on `nomic-embed-text` (768-dim) — the two stores are separate; the router does not touch memory vectors.
- No feedback loop. Routing decisions are logged for "eval consumption" but nothing reads them back. Every routing miss is permanent until a human edits the regex tables.
- `temperature` is not set in code — Ollama's default applies. Past references to "temperature 0.7" describe scheduler behavior, not Pepper's runtime.

### Goal

A router that:

1. Generalizes to paraphrased queries without code changes.
2. Improves passively from logged interactions and user feedback.
3. Pre-filters the LLM's tool menu so hermes3 reasons over ≤5 candidate tools, not 50.
4. Has explicit safety rails: backups before promotion, rollback on regression, eval suite that must pass.
5. Discovers new intents autonomously as usage patterns emerge.

### Alignment with INFRA_GUIDELINES §5

§5 says: *"Default to explicit; add semantic where fuzziness is the feature."* Intent classification IS the fuzzy case — paraphrasing is the whole problem. Slot extraction (names, dates, source words) is *explicit* and stays regex-based. Tool execution stays explicit (SQL filters, exact lookups). This migration moves only the *intent classification* layer to semantic; everything downstream stays explicit.

---

## Target Architecture

```
User query
  ↓
┌─ Semantic intent classifier (k-NN over labeled exemplars) ─┐
│   Replaces intent classification job from query_router.py  │
│   ├─ confidence ≥ 0.55       → emit intent + tool subset   │
│   ├─ ambiguous (top conflict) → ASK_CLARIFYING             │
│   └─ OOD (no close match)    → ASK_CLARIFYING              │
└────────────────────────────────────────────────────────────┘
  ↓
┌─ Slot extractor (regex/parse, hardened from query_router.py) ─┐
│   Extracts: entity_targets, time_scope, target_sources,        │
│   action_mode. Explicit features, deterministic.               │
└─────────────────────────────────────────────────────────────────┘
  ↓
RoutingDecision (drop-in compatible with current contract)
  ↓
Tool list assembly (filtered to intent's tool subset, ≤ 5 tools)
  ↓
LLM (hermes3) with curated tool menu + relevant memory context
  ↓
Tool execution + response
  ↓
┌─ Feedback capture ─────────────────────────────────────────┐
│   Explicit: Telegram message reactions (👍 / 👎 / ❤️ / 💩 …)│
│   Implicit: re-ask, abandoned, topic-shift, tool divergence│
│   Spot-check: 5% sampled, weekly Telegram batch            │
└────────────────────────────────────────────────────────────┘
  ↓
Exemplar table → nightly rebuild → next-day learning
```

The classifier is intentionally simple: k-NN over `qwen3-embedding:0.6b` embeddings (1024-dim, local Ollama) of labeled exemplars. **No fine-tuning of the embedding model.** All learning happens by growing and curating the exemplar set.

---

## Phase 0 — Failure diagnosis (validate the hypothesis)

Confirm routing is the dominant failure mode before building anything. We have no existing failure dataset, so Phase 0 must seed it.

### Tasks (in order)

1. **Build synthetic test battery (100 queries):**
   - 10 LIFE_CONTEXT.md categories × 10 queries each (uniform weighting)
   - Categories: Travel, Family, Health, Partner, Calendar, Communications, Finance, Meal, Proactive, Knowledge
   - Output: `tests/failure_seed_battery.jsonl`
   - Format: `{category, query, difficulty, expected_intent, expected_tools}`

2. **Add lightweight chat-turn logger (parallel to battery work):**
   - Patch `agent/core.py` to write every chat turn to `logs/chat_turns/<date>.jsonl`
   - Captures: `timestamp, query, response, tool_calls, latency, model`
   - No analysis. No schema change to DB yet. File-based.
   - This is the seed for organic data going forward.

3. **Run battery against current Pepper:**
   - Loop through battery, send each via chat interface
   - Capture full response + tool_calls
   - Output: `logs/router_audit/battery_run_<timestamp>.jsonl`

4. **LLM-assisted classification (Opus 4.7):**
   - For each query: success/failure judgment vs `data/life_context.md` ground truth
   - For failures: classify per richer taxonomy:
     - `ROUTING_MISS` — wrong tool selected
     - `INTERCEPT_MISS` — deterministic intercept fired but produced wrong output
     - `HALLUCINATION` — model invented facts
     - `SYNTHESIS_MISS` — right tool, right data, wrong summarization
     - `TOOL_MISS` — right tool, bad data returned
     - `CONTEXT_MISS` — right tool, bad context window
     - `OVER_INVOCATION` — too many tools called, blew context
     - `STALE_MEMORY` — outdated facts pulled into context
     - `OTHER` — none of above

5. **Tabulate, write report:**
   - Output: `logs/router_audit/audit_<date>.md`
   - Sections: summary, taxonomy %, top 10 recurring failure patterns, recommendation

6. **Branch decision (autonomous):**
   - Routing-fixable share = `ROUTING_MISS` + `INTERCEPT_MISS`
   - **If ≥40%:** PROCEED to Phase 1, log decision, continue
   - **If <40%:** write recommendation report identifying dominant failure mode, ping user via Pepper's Telegram bot (`telegram_bot.push()` to allowed user_id), set `blocked: true` with `blocked_reason` describing diagnosis. Wait for user to author alternate plan and unblock. Agent reads `docker compose logs pepper` for user's reply to track unblock signal.

### Exit criterion

Report exists in `logs/router_audit/`, decision recorded in `router_build_state.json` (`proceed | blocked-awaiting-branch`).

---

## Phase 1 — Instrumentation

Stand up the queryable, embedded routing-events table. The current router stays in place.

### Schema (single migration)

```sql
CREATE TABLE routing_events (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    query_text TEXT NOT NULL,
    query_embedding vector(1024),
    regex_decision_intent TEXT,
    regex_decision_sources TEXT[],
    regex_decision_confidence FLOAT,
    tools_actually_called JSONB,
    llm_model TEXT,
    latency_ms INTEGER,
    success_signal TEXT,
    success_signal_set_at TIMESTAMPTZ,
    shadow_decision_intent TEXT,
    shadow_decision_confidence FLOAT,
    user_session_id TEXT
);
CREATE INDEX ON routing_events (timestamp DESC);
CREATE INDEX ON routing_events (success_signal);
CREATE INDEX ON routing_events (user_session_id);
CREATE INDEX ON routing_events USING hnsw (query_embedding vector_cosine_ops);
```

`shadow_decision_*` columns added now (forward-compat for Phase 2).

### Tasks

1. Migration creating `routing_events` table.
2. Patch `agent/core.py` to write per-turn rows with embeddings (`qwen3-embedding:0.6b` local, 1024-dim).
3. Backfill from Phase 0's `logs/chat_turns/<date>.jsonl`.
4. Keep file-based logs writing in parallel (DB is queryable copy, files are durable plaintext source of truth).
5. Implement success-signal heuristic (run async, doesn't block response):
   - `re_asked`: within **30 min**, ≥50% keyword overlap, same session
   - `abandoned`: no follow-up within **60 min** AND response < 50 chars OR contained refusal/error markers
   - `confirmed`: follow-up within **30 min** AND keyword overlap < 30%
   - `unknown`: none of above
6. Build `agent/router_logs.py` CLI:
   - `--histogram-by-intent` — counts per intent over period
   - `--histogram-by-success-signal` — proceed/re-ask/abandon rates
   - `--divergence` — Phase 2+: where regex and shadow disagreed
   - `--query "<text>"` — nearest k embedded queries from past
   - `--since <date>` — time-bound any of the above
7. Add `bin/router-logs` bash wrapper:
   ```bash
   #!/usr/bin/env bash
   exec docker compose exec pepper python -m agent.router_logs "$@"
   ```

### Exit criterion

- ≥227 logged events accumulated through normal usage (retuned 2026-04-28
  from the original 300 with explicit user authorization. Original 300 was
  a placeholder; Phase 1 organic-traffic cadence made it a multi-week wait
  with no information gain. 227 = unique rows at the moment of retune
  (after deduping 8 backfill/inline-writer overlaps); classifier bootstrap
  viability is unchanged at this scale.)
- ≥30% with non-`unknown` `success_signal` (heuristic quality gate)
- No regression in Pepper response latency (>10% slowdown is a fail)

---

## Phase 2 — Shadow semantic classifier

Build the embedding classifier and hardened slot extractor. Run in parallel with regex router. Don't act on its output yet.

### Task 0 — Schema migration + re-embed (prerequisite)

Phase 1 created `routing_events.query_embedding` as `vector(768)` for
`nomic-embed-text`. Phase 2 switches the router to
`qwen3-embedding:0.6b` (1024-dim). Before any classifier work:

1. Snapshot `routing_events` to `backups/router/` (CSV or pg_dump).
2. `ALTER TABLE routing_events ALTER COLUMN query_embedding TYPE vector(1024) USING NULL;` then re-create the HNSW index.
3. Re-embed every existing row's `query_text` with
   `qwen3-embedding:0.6b` and write back to `query_embedding`.
4. Update `agent/llm.py`'s embedding helper (or add a router-specific
   helper) to call `qwen3-embedding:0.6b`. Memory subsystem keeps its
   own `nomic-embed-text` path untouched.
5. Verify no NULL `query_embedding` rows remain on Phase-1 events.

### Task 1 — Telegram reaction capture (✅ landed 2026-04-28)

Pulled forward from Phase 5's "explicit feedback" line in the target
architecture. The success-signal heuristic infers from text follow-ups
only, so most turns sit at `unknown` — starving the signal-coverage
gate and the Phase 2 exemplar-quality bar. Reactions give a one-tap
explicit channel.

What landed:

1. `routing_events`: + `outbound_chat_id BIGINT`, `outbound_message_id
   BIGINT`, with a partial composite index keyed on the (chat, message)
   pair so reaction lookups are O(1). Idempotent `ALTER TABLE ... ADD
   COLUMN IF NOT EXISTS` in `agent/db.py:init_db` keeps existing
   databases upgrade-safe.
2. `PepperCore.record_outbound_message` — called by the Telegram bot
   right after sending its reply; stamps the final paragraph's
   `message_id` onto the most recent `routing_events` row for that
   session. Brief retry loop (~1.5s) bridges the inline-writer's async
   lag.
3. `PepperCore.apply_reaction_signal` + `reaction_to_signal` — pure
   mapping function (covered by `agent/tests/test_reaction_signal.py`).
   Conservative emoji map: 👍/❤️/🔥/🙏/🎉/🤩/👏/💯/🥰/👌 → `confirmed`,
   👎/💩/🤬/😡/🤮/🥱 → `abandoned`. Negative dominates when mixed.
   Ambiguous reactions (🤔, 😱) intentionally unmapped so the heuristic
   sweep keeps its say.
4. `JARViSTelegramBot`: registers `MessageReactionHandler`, opts into
   `Update.ALL_TYPES` (Telegram won't deliver `MessageReactionUpdated`
   without the explicit `allowed_updates` opt-in), and threads the
   final outbound `message_id` from `_stream_response` →
   `_render_response` → `_record_outbound`.
5. Auth: only allowed-user reactions count.

Out of scope here (deferred):

- Reaction capture on the HTTP-API channel (no message ids exist there
  to react to — would require a separate /feedback endpoint).
- Inline-keyboard 👍/👎 buttons on each Pepper reply. Reactions are
  lower-friction and don't clutter the UI; revisit only if Telegram
  delivery proves unreliable.
- Backfilling reactions onto historical rows that predate the
  outbound-id capture — there's no message-id to anchor them to.

### Architecture

- `SemanticRouter` replaces intent classification **only** (job 1)
- Slot extraction (entities, time scope, sources, action_mode) ported from `query_router.py` and **hardened for production**:
  - Strict typing on all extractor functions
  - Explicit error contracts (return `None` for "not found", raise only on malformed input)
  - Comprehensive test coverage per extractor
  - Documented supported patterns per extractor
  - Edge case handling: empty queries, unicode, queries >2000 chars
- Lives in `agent/semantic_router.py` and `agent/slot_extractors.py`

### Bootstrap exemplars (tiered)

- **Platinum** (~80 exemplars): Phase 0's classified failures — LLM-judged labels, including "what intent should this have been"
- **Gold** (~150 exemplars): Phase 0's battery successes — query + regex-router intent + verified correct
- **Silver** (~200-1000 exemplars): Phase 1 organic logs where `success_signal == confirmed` — trust regex labels
- **Manual top-up**: 5-10 hand-labeled exemplars per "Top 10 Pattern" from Phase 0

### k-NN classifier

```
k = 7
distance = cosine via pgvector
weighting = 1 / (epsilon + distance)        # epsilon = 0.05
confidence = sum(winning_weights) / sum(all_weights)
out_of_distribution: top-1 distance > 0.40 → ASK_CLARIFYING
ambiguity: winner conf < 0.55 AND runner_up > 0.30 → ASK_CLARIFYING
           with both candidates surfaced
```

Thresholds tune via shadow data — these are starting values.

**Kernel calibration (2026-04-29):** the original `1 / (1 + distance)` kernel
was empirically too flat. A self-match at `distance=0` got weight 1.0; six
mismatched neighbours at `distance≈0.30` each got weight 0.77, summing to 4.6
and outvoting the exact match. Changed to `1 / (epsilon + distance)` with
`epsilon=0.05`: a self-match weighs 20.0, swamping any moderately-distant
field. Eval-set accuracy moved 75% → 98% with this change alone. OOD eval
and multi-intent eval remain green.

### Embedding cache

`qwen3-embedding:0.6b` inference is ~80-150ms locally (639MB model, 1024-dim). Cache by `sha256(query) → embedding`. Repeat queries free. Expected p95 < 200ms.

### Multi-intent handling

Regex fragment splitter ported, **extended with more variations**:
- Existing: `" and "`, `" also "`, `"; "`, `"?"`
- Added: `" plus "`, `" then "`, `" & "`, line breaks, `"what about"` prefixes, `" as well as "`, em-dash variants, hyphen between clauses
- Negative split guards: don't split inside quotes, possessives, etc.

Each fragment classified independently.

### Shadow mode

- `SemanticRouter` runs in parallel with regex router on every query
- Regex output drives behavior; semantic output written to `routing_events.shadow_decision_*`
- No user-visible change

### Exit criteria for Phase 3 promotion (all must hold)

1. Agreement ≥ 90% on queries where regex confidence ≥ 0.9
2. Divergence sample (50 cases): user adjudicates via **Telegram batch** (~40 min); semantic correct ≥ 65%, regex correct ≤ 35%
3. p95 embedding+search latency < 200ms (retuned for `qwen3-embedding:0.6b`; original 150ms target assumed `nomic-embed-text`)
4. OOD detection fires on ≥ 80% of 20-query nonsense test set
5. Multi-intent split accuracy ≥ 90% on 30-query test set
6. Phase 0's 100-query battery: ≥ 85% correct classification (becomes ongoing regression gate as `tests/router_eval_set.jsonl`)

---

## Phase 3 — Atomic cutover

Promote the semantic router to primary. Delete the regex router (archived, recoverable).

### Pre-cutover (in order)

1. Snapshot exemplar table + index → `backups/router/<timestamp>/`
2. ~~Move `agent/query_router.py` → `archive/query_router_<date>.py.bak`~~ **DEFERRED to Phase 5 cleanup** — see "Architectural amendment" below.
3. Confirm `tests/router_eval_set.jsonl` exists with 100 queries
4. Confirm all Phase 2 exit criteria still hold (re-run shadow comparison)

### Architectural amendment (2026-04-29, iter 122)

The original spec listed two coupled changes for the cutover commit: (a) swap
`QueryRouter` → `SemanticRouter` as primary in `core.py`, AND (b) physically
move `agent/query_router.py` to `archive/`. Empirical evidence from iters 120–
121 shows these two changes cannot land in the same commit without breaking
shadow mode:

- `agent/semantic_router.py` imports `IntentType`, `ActionMode`, and
  `RoutingDecision` directly from `agent.query_router` (the legacy module owns
  those types — see [agent/semantic_router.py:64](../agent/semantic_router.py)).
- The cutover commit explicitly preserves `ROUTER_SHADOW=1` behavior (line
  341): when set, `QueryRouter` runs in shadow alongside `SemanticRouter` for
  comparison logging. That requires `QueryRouter` to remain importable from
  `agent/`.
- Moving the file to `archive/` immediately would either break those imports
  outright, or force an importlib-shim hack just to load a module that's about
  to be deleted 30 days later anyway.

**Resolution:** The file move is deferred to **Phase 5 cleanup**, where it is
performed atomically with the rest of the legacy retirement (delete
`ROUTER_SHADOW` codepath, remove the file). The migration plan's Phase 5
"After 30 days clean operation" subsection already groups these changes — this
amendment simply removes the redundant pre-cutover file move. The cutover
commit retains all other steps unchanged: primary-router swap, pre-commit
eval gate, doc updates, soak window.

This change does NOT weaken any phase exit criterion (Hard Rule 3); it
clarifies how to satisfy the `ROUTER_SHADOW=1` requirement that already lives
in the spec.

### Cutover commit

- Single commit changing `core.py`: `QueryRouter` → `SemanticRouter` (primary).
  When `ROUTER_SHADOW=1`, `QueryRouter` runs in shadow alongside the new
  primary (the inverse of Phase 2's shadow direction).
- `ROUTER_SHADOW=1` env var still works (runs both for comparison)
- Pre-commit hook: `tests/router_eval_set.jsonl` must pass ≥85%

### Post-cutover monitoring (3-day soak window)

Per-hour automated checks:
- Eval set pass rate ≥ 85%
- `re_asked` rate within 1.5× pre-cutover baseline
- `abandoned` rate within 1.5× pre-cutover baseline
- p95 routing latency < 200ms
- Router exception rate = 0

Failure handling:
- Telegram ping on any check fail
- Auto-rollback (docker rebuild from archive) if eval drops below 80%
- Otherwise wait for user call

### Documentation updates in cutover commit

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/INFRA_GUIDELINES.md` (clarify §5 boundary: intent now semantic, slots stay explicit)
- `docs/SEMANTIC_ROUTER.md` (new operating doc)

### After 30 days clean operation

- Move `agent/query_router.py` → `archive/query_router_<date>.py.bak` (the
  pre-cutover file move deferred per the architectural amendment above)
- Update `agent/semantic_router.py` to import `IntentType`, `ActionMode`,
  `RoutingDecision` from a shared types module (or extract them at this
  point); ditto for any test importers
- Remove `ROUTER_SHADOW` codepath from `agent/core.py`
- Action triggered in Phase 5 cleanup, not auto-scheduled here

### Exit criterion

3-day soak passes all checks, kill-switch (docker rebuild) verified, documentation merged, Telegram notification sent.

---

## Phase 4 — Feedback loop

Close the loop. Confirmed exemplars promote in real-time; demotions and evictions batch nightly.

### Feedback sources (all three active)

**A. Explicit (Telegram inline keyboard buttons under each Pepper response):**
- `[👍]` → confirmed positive exemplar (real-time insertion)
- `[👎]` → query queued for adjudication batch (weekly)
- `[✏️]` → opens "what should this have done?" prompt → label captured

**B. Implicit (already partial in Phase 1; extended):**
- Re-ask within 30min → previous routing wrong
- Abandoned (no follow-up + short response) → previous routing failed
- Topic-shift follow-up → previous routing succeeded
- LLM tool divergence: router suggested `[A,B]`, LLM called `[C]` → flag routing's tool subset wrong

**C. Spot-check sampling:**
- 5% of routed queries per week queued for manual review
- Telegram weekly batch every Sunday: "Here are N routings to spot-check"

### Negative signal handling (delete-and-forget)

- Negative signals trigger eviction from exemplar set at nightly rebuild
- No polarity scores, no negative voting
- Simpler, fewer edge cases

### Feedback latency (hybrid)

- **Additions real-time**: confirmed exemplars insert immediately, available on next query
- **Evictions/demotions**: nightly batch only (atomic + rollback-friendly)

### Per-intent cap and eviction

- Cap: **1000 per intent** (perf is not the bottleneck on pgvector + HNSW)
- Eviction priority (highest evicted first):
  1. Negative-signaled exemplars (immediate at nightly rebuild)
  2. `confirmation_count = 0` AND age > 60 days
  3. Age > 180 days AND no confirmations in last 30 days
- **Never evict:**
  - Manually-labeled (manual tier from Phase 2 seeding)
  - `confirmation_count >= 5`
  - Most recently confirmed exemplars within last 14 days

### Nightly rebuild (2:30am local)

1. Snapshot exemplar table + HNSW index → `backups/router/<date>/`
2. Apply nightly changes (evictions, demotions; additions already real-time)
3. Rebuild HNSW index (in-place; pgvector supports this)
4. Run eval set (`tests/router_eval_set.jsonl`)
5. **If eval drops below 96%**: rollback to snapshot, Telegram alert
6. **If eval passes**: write daily digest to `logs/router_audit/daily_<date>.md`
7. Telegram daily digest pushed every day (no noteworthy-only filter)

### Stagnation defense

- Implicit signals work without explicit feedback
- Sunday spot-check batch keeps user in loop
- Eval set is the floor

### Exit criterion

- Nightly rebuild runs cleanly for 7 consecutive days
- Exemplar count grows
- Eval set pass rate stays ≥ 98% across the 7-day window
- All three feedback sources fire at least once with verified effect

---

## Phase 5 — Autonomous evolution + safety

Pattern detection, autonomous promotion, drift detection, auto-rollback. The router invents new intents on its own.

### Pattern detection (daily, after nightly rebuild)

1. Pull queries from last 30 days where:
   - Confidence < 0.55 (ambiguous), OR
   - Routed to `GENERAL_CHAT`
2. Embed (cached from `routing_events`)
3. **HDBSCAN clustering** (handles unknown # clusters, noise-robust)
4. For each cluster of size ≥ 10:
   - **Tool consistency**: % cluster queries with shared tool calls
   - **Stability**: cluster present in last-7-day AND last-30-day windows
   - **User satisfaction**: re-ask rate on cluster < 25%
   - **Intent name**: LLM (Opus 4.7) summarizes cluster → snake_case name
5. If all conditions met → promote (fully autonomous, mode A)

### Auto-promotion (mode A, no grace period)

**Pre-promotion (always):**
1. Snapshot exemplar table → `backups/router/<timestamp>_pre_promotion_<intent>/`
2. Snapshot intent registry (intents + tool mappings)
3. Write promotion log entry to `logs/router_audit/promotions_<date>.md`:
   - Cluster stats, 5 sample queries, tool subset, snapshot path
4. Telegram notification with promotion ID

**Promotion:**
1. Register new intent (auto-named, snake_case from LLM suggestion)
2. **Compute tool subset:**
   - Top 5 tools by frequency in cluster
   - Each must be called in ≥ 30% of cluster queries (quality floor)
   - If no tool meets floor → `ANSWER_FROM_CONTEXT`
3. Cluster exemplars added to exemplar table with new intent label
4. Live on next query

### Drift detection (daily, after promotion job)

1. Take last 100 queries from `routing_events`
2. Re-route each through current router; compare to original routing
3. **If routing changed for ≥ 5%** → Telegram alert (heads-up only)
4. **If routing changed for ≥ 20%** → auto-rollback last nightly rebuild, Telegram alert at higher severity

### Operational cadence

**Daily Telegram:**
- Daily digest (Phase 4)
- Drift alert if any
- Promotion notification if any

**Weekly Telegram (Sundays):**
- Promotion summary for the week
- Eval delta over the week
- Top 5 exemplar additions
- Spot-check batch (Phase 4)

**Monthly:** none.

### Exit criterion (Phase 5 = first proof, then steady-state)

1. First auto-promotion completes cleanly (snapshot + register + no eval regression + Telegram notification)
2. Drift detector runs 7 consecutive days without false-positive alert
3. Auto-rollback verified working via deliberate-degradation test
4. `docs/SEMANTIC_ROUTER.md` updated with operating procedures (inspect, override, delete intents)

When Phase 5 exits:
- Migration complete
- Build routine sets `phase: 6`, `phase_status: "all_phases_complete"` in `router_build_state.json`
- Routine self-recommends pause/deletion in final commit

---

## Per-Task Quality Protocol

Each **task** within a phase runs implement → review → test → commit end-to-end in a single run. Each task must complete **3 review cycles** and **3 test cycles** before being marked done.

### 3 review cycles

1. **Self-simplification** — invoke `/simplify` skill on the diff. Remove premature abstractions.
2. **Architectural review** — verify against CLAUDE.md (privacy, additive, subsystem boundaries) and INFRA_GUIDELINES.md (especially §1, §3, §5).
3. **Test coverage + integration review** — every new code path covered? Run end-to-end with real Pepper instance (rebuild via `docker compose up -d --build` if needed); nothing regressed.

### 3 test cycles

1. **Unit tests** on new code.
2. **Replay or stability test** — Phase 1+: re-run against logged historical queries. Phase 0: re-run the deterministic generator/loader to confirm output stable.
3. **End-to-end live test** — ask Pepper 5 representative queries via the chat interface; verify responses. For phases that change tooling/routing, also run the canonical eval set (`tests/router_eval_set.jsonl`) when it exists.

If any test cycle fails, fix in-run if possible (within the run's time budget). If not, increment `fix_attempts_this_phase` and the next run resumes. **Stop after 3 fix attempts per phase** — escalate to user via Telegram.

### Phase exit criterion check

After all tasks in a phase complete (3+3 cycles each), the next run runs the phase-level exit criterion check from the phase spec. Advance only when met.

---

## What This Migration Does NOT Do

- Does **not** fine-tune the embedding model. `qwen3-embedding:0.6b` stays as-is.
- Does **not** change the LLM (hermes3 stays primary).
- Does **not** touch memory retrieval. Memory pipeline is untouched.
- Does **not** change tool implementations. Tools stay as-is.
- Does **not** introduce cloud dependencies. All k-NN is local pgvector.

The blast radius is exactly: `query_router.py` is replaced; `core.py` instantiates a different router class; one new table is added; new modules are added (`semantic_router.py`, `slot_extractors.py`, `router_logs.py`).

---

## Rollback Plan

If at any point the new router is worse than the regex router:

1. Restore `archive/query_router_<date>.py.bak` to `agent/query_router.py`.
2. Revert the `core.py` instantiation change (single line).
3. Optionally drop `routing_events` and `router_exemplars` tables (data preserved in `backups/router/`).
4. `docker compose up -d --build` — Pepper is back to current state.

Phase-specific rollback paths:
- **Phase 1-2**: revert commit, no behavior was changed
- **Phase 3**: docker rebuild with archive restoration (above)
- **Phase 4**: nightly rebuild auto-rollbacks on eval drop <96%
- **Phase 5**: drift detector auto-rollbacks on ≥20% routing change

The migration is fully reversible at every phase.

---

## Decisions Log (frozen 2026-04-27)

| Phase | Decision | Choice | Rationale |
|---|---|---|---|
| 0 | Failure data sources | Hybrid: synthetic battery + organic logging | No existing dataset; battery bootstraps Phase 0, logging accumulates for Phase 1+ |
| 0 | Classification approach | LLM-assisted (Opus 4.7) with user spot-check | 200 cases too many for manual; user spot-checks adjudicate calibration |
| 0 | Failure taxonomy | Richer 9-category | Coarse buckets miss `INTERCEPT_MISS`, `HALLUCINATION` patterns we know exist |
| 0 | Branch behavior | Hard-block + Telegram ping for alternate plan | Agent shouldn't invent strategy autonomously; user authorizes branch |
| 0 | User adjudication timing | Skip pre-classification, spot-check after | User trusts LLM classifier; spot-check catches systematic bias |
| 1 | Shadow columns | Added now (forward-compat) | Cheap; saves migration in Phase 2 |
| 1 | File logs after DB | Keep both | DB queryable, files unix-tool-friendly + durable |
| 1 | Success signal windows | 30min re-ask, 60min abandoned | Larger windows than original 5/10min — more realistic for daily use |
| 1 | Inspector tooling | CLI + bash wrapper | Both Python interface and ergonomic shell access |
| 1 | Exit threshold | 300 events, 30% non-unknown signal | Lower bar; classifier doesn't need 500 to bootstrap |
| 2 | Scope | Replace intent classification only | Slot extraction stays explicit per INFRA_GUIDELINES §5 |
| 2 | Slot extractor treatment | Port + harden for production | Production-grade typing, error contracts, test coverage |
| 2 | Exemplar tiers | Silver tier accepted | Inherits some regex bias; Platinum/Gold contradict where wrong |
| 2 | k value | k=7 | Smooths noise without blurring distinctions |
| 2 | Performance | Accept ~120ms + cache | qwen3-embedding:0.6b dominates; cache covers repeat queries |
| 2 | Multi-intent splitter | Keep regex, extend variations | More conjunctions/punctuation patterns |
| 2 | Adjudication batch | User does ~40min Telegram batch before cutover | Only way to verify "actually better" vs "differently wrong" |
| 3 | Cutover style | Atomic flip | Single user, regression visible immediately, no need for ramp |
| 3 | Archive period | 30 days | Fast non-git rollback path |
| 3 | Soak window | 3 days | Daily user with many use cases; weekend coverage |
| 3 | Kill switch | None (trust docker rebuild) | Avoid extra code complexity |
| 4 | Feedback UX | Inline keyboard buttons (#1) | Universal, unambiguous, no library version risk |
| 4 | All three feedback sources | A + B + C | Belt + suspenders + sampling |
| 4 | Negative signal handling | Delete-and-forget | Simpler, fewer edge cases |
| 4 | Feedback latency | Hybrid (additions real-time, evictions nightly) | Fast learning + safe quality control |
| 4 | Per-intent cap | 1000 | Perf isn't bottleneck; cap prevents bias and unbounded growth |
| 4 | Daily digest | Push every day | Always-on visibility |
| 5 | Clustering algo | HDBSCAN | Handles unknown # clusters, noise-robust |
| 5 | Promotion mode | A: fully autonomous, immediate | User wants autonomy with backup safety |
| 5 | Tool subset selection | Top 5 + 30% floor | Bounded menu with quality floor; defensible |
| 5 | Drift thresholds | 5% alert, 20% auto-rollback | Tighter than original 10%/25% |
| 5 | Intent naming | Auto-name from LLM | Per-promotion confirm is overkill if autonomous |
| 5 | Cadence | Daily + Weekly | Skip monthly; weekly already aggregates |

---

## Communication Channel

The build agent communicates with the user via **Pepper's Telegram bot** (`agent/telegram_bot.py`). Outbound: `JARViSTelegramBot.push()` to allowed user_id. Inbound: agent reads `docker compose logs pepper` to capture user replies. Pepper itself may attempt to respond to incoming messages — that's expected and fine; the build agent reads the raw log, not Pepper's response.
