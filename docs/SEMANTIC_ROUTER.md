# Semantic Router — Operating Doc

This is the operator/maintainer reference for Pepper's semantic intent router.
For the build history and migration plan, see
[SEMANTIC_ROUTER_MIGRATION.md](SEMANTIC_ROUTER_MIGRATION.md). For the
architectural reasoning behind why intent classification is semantic but slot
extraction stays explicit, see [INFRA_GUIDELINES.md](INFRA_GUIDELINES.md) §5.

## What it is

The semantic router classifies each user message into a `RoutingDecision` —
intent + target sources + slots — before prompt assembly. It replaced the
regex-based `QueryRouter` as Pepper's primary router during the Phase 3
atomic cutover.

The router is a fusion of three deterministic pieces:

- **`split_multi_intent`** — fragments compound utterances ("any emails AND
  what's on my calendar?") into independent intent-bearing clauses.
- **`SemanticIntentClassifier`** — k-NN intent label per fragment using
  pgvector similarity over a manually-seeded + auto-promoted exemplar table
  (`router_exemplars`). Embeddings are 1024-dim from the local Ollama model
  `qwen3-embedding:0.6b`.
- **Explicit slot extractors** — time scope, entity targets (person names),
  target sources (email/imessage/calendar/...), filesystem path. These are
  regex/keyword-based by design; semantics handles "what?", slots handle
  "which?".

The classifier never makes external API calls. Embeddings come from local
Ollama; the lookup is a single pgvector query against `router_exemplars`.

## Where it lives

| Concern | File |
| --- | --- |
| Primary router class | [agent/semantic_router.py](../agent/semantic_router.py) (`SemanticRouter`, `SemanticIntentClassifier`) |
| Shared types | [agent/query_router.py](../agent/query_router.py) (`IntentType`, `ActionMode`, `RoutingDecision`) — types stay here through the soak window; legacy `QueryRouter` class also lives here for shadow mode |
| Exemplar table | `router_exemplars` (DDL: [agent/db.py](../agent/db.py) `init_db`; ORM: [agent/models.py](../agent/models.py) `RouterExemplar`) |
| Routing event log | `routing_events` (ORM: [agent/models.py](../agent/models.py) `RoutingEvent`) |
| Eval set (regression gate) | [tests/router_eval_set.jsonl](../tests/router_eval_set.jsonl) — 100 queries |
| Latency bench | [agent/router_latency_bench.py](../agent/router_latency_bench.py) |
| Backfill / replay tools | [agent/router_backfill.py](../agent/router_backfill.py), [agent/router_shadow_replay.py](../agent/router_shadow_replay.py) |
| Adjudication tooling | [agent/router_adjudication.py](../agent/router_adjudication.py) |

## The intent set

Defined in [agent/query_router.py](../agent/query_router.py)
`IntentType`. Eleven labels, each with a fixed action mode:

| Intent | Action mode | Notes |
| --- | --- | --- |
| `capability_check` | `answer_from_context` | "can you read my email?" — answered from `CapabilityRegistry` |
| `inbox_summary` | `call_tools` | "what emails came in today?" |
| `action_items` | `call_tools` | "what do I owe replies on?" |
| `person_lookup` | `call_tools` | "what did Susan say last week?" |
| `conversation_lookup` | `call_tools` | "find the thread about the contract" |
| `schedule_lookup` | `call_tools` | "what's on my calendar tomorrow?" |
| `cross_source_triage` | `call_tools` | "anything important overnight?" |
| `general_chat` | `answer_from_context` | open-ended, no tool needed |
| `unsupported_capability` | `answer_from_context` | health/meal/finance — refuse politely (subsystems not yet integrated) |
| `web_lookup` | `call_tools` | explicit "google X / search the web" |
| `unknown` | `ask_clarifying_question` | classifier can't decide; defer to user |

## Shadow mode

When `ROUTER_SHADOW=1` is set in the environment, both routers run on every
turn for comparison. As of the Phase 3 cutover the polarity is inverted from
Phase 2:

- **Primary**: `SemanticRouter` — its decision drives behavior
- **Shadow**: `QueryRouter` (regex) — its top decision is logged onto
  `routing_events.shadow_decision_*` for offline comparison

Shadow mode is best-effort: if either router raises, the row still lands with
the other side's decision and a `routing_event_shadow_failed` warning. Shadow
mode runs off the critical path — the user-visible response is decided by the
primary router alone.

Shadow mode + the `QueryRouter` codepath are scheduled for removal **30 days
post-cutover** as part of Phase 5 cleanup (see migration plan
[Phase 5](SEMANTIC_ROUTER_MIGRATION.md#after-30-days-clean-operation)).

## The eval gate

`tests/router_eval_set.jsonl` is the canonical 100-query battery. Pass rate
must hold ≥ 85% as a pre-commit gate on any change that touches the router.
At cutover time the live pass rate was 98%.

To run manually:

```bash
.venv/bin/pytest agent/tests/test_router_eval.py -q
```

A live-eval CLI is wired into `agent.router_eval`:

```bash
.venv/bin/python -m agent.router_eval --threshold 0.85
```

The pre-commit hook lives at
[scripts/git-hooks/pre-commit-router-eval](../scripts/git-hooks/pre-commit-router-eval)
and runs that CLI automatically, but only when staged files touch router
code or the eval set itself (full path list inside the script). Install:

```bash
ln -sf ../../scripts/git-hooks/pre-commit-router-eval .git/hooks/pre-commit
```

Bypass for a single commit (use sparingly — emergencies, doc-only diffs the
pattern list missed, etc.):

```bash
SKIP_ROUTER_EVAL=1 git commit ...
```

The same JSONL is the post-cutover soak-window check (per-hour automated
review during the 3-day window; auto-rollback at < 80%).

## Post-cutover soak monitor

[agent/router_soak_monitor.py](../agent/router_soak_monitor.py) implements
the Phase 3 3-day soak window. Five checks per hour:

| # | Check | Source | Threshold |
|---|---|---|---|
| 1 | Eval pass rate | `agent.router_eval` (canonical 100-query JSONL) | ≥ 0.85 (rollback < 0.80) |
| 2 | Router-only p95 latency | optional probe via `agent.router_latency_bench` | < 200 ms |
| 3 | Primary-router exception rate | `routing_events.regex_decision_intent IS NULL` (post-cutover this column stores SemanticRouter's primary decision) | = 0 |
| 4 | `re_asked` rate vs baseline | `routing_events.success_signal` last hour vs frozen baseline | within 1.5× |
| 5 | `abandoned` rate vs baseline | same | within 1.5× |

The frozen baseline lives at `logs/router_audit/soak_baseline.json` (.gitignored
— the file is per-machine; recompute on every cutover). Computed once at the
start of the soak by sweeping the 48h pre-cutover window.

```bash
# Once at soak start (computes & writes baseline):
.venv/bin/python -m agent.router_soak_monitor compute-baseline --lookback-hours 48

# Hourly during soak (exits 0 PASS / 1 FAIL / 4 ROLLBACK):
.venv/bin/python -m agent.router_soak_monitor check --window-hours 1
```

Each `check` run writes `logs/router_audit/soak_<utc-ts>.json` for audit. The
ratio checks have a noise floor (baseline < 1% absolute and soak < 5%
absolute → PASS) so a near-zero baseline can't trip a false-positive failure
on natural variance. The router-only p95 gate runs by default — the CLI
builds a probe via `build_default_router_p95_runner` that benches
`SemanticRouter.route_first` against a stratified subset of
`tests/router_eval_set.jsonl` (default 30 queries, 3 warmup) and reports
`p95_ms` to the soak evaluator. Pass `--no-router-p95` to skip the probe
(the gate degrades to SKIP); `--p95-sample-n` / `--p95-warmup` /
`--p95-queries-file` tune the probe. The probe is necessary because
`routing_events.latency_ms` measures full chat-turn wall time (LLM + tools),
not the router alone.

## Latency

Gate 3 of the migration plan: p95 end-to-end routing latency < 200ms. As
measured at Phase 2 close on a warm container against local Ollama
`qwen3-embedding:0.6b`: p95 = 90.45 / 94.20 / 91.52 ms across three full
100-query runs (~50% headroom). Bench reproducibly:

```bash
docker compose exec pepper-agent python -m agent.router_latency_bench \
  --eval-set tests/router_eval_set.jsonl --warmup 5
```

## When the router gets a query wrong

Three feedback channels, all built into Phase 4:

- **Explicit (Telegram inline buttons)** — 👍 promotes the query as a
  confirmed exemplar (real-time insertion); 👎 queues for the weekly
  adjudication batch; ✏️ opens a "what should this have done?" prompt and
  captures a corrective label.
- **Implicit signals** — re-ask within 30 min, abandoned (no follow-up + short
  response), topic-shift follow-up, LLM-tool divergence (router suggested
  `[A, B]`, LLM called `[C]`).
- **Spot-check sampling** — 5% of routed queries per week are queued for a
  Sunday Telegram batch.

Negative signals trigger eviction at the nightly rebuild (delete-and-forget;
no polarity scores). Positive signals add real-time. Adjudication batches
are operator-driven via `agent.router_adjudication --send` /
`--ingest-replies`.

## Auto-rollback (kill-switch)

If the post-cutover soak window or any subsequent nightly rebuild detects an
eval drop below 80%, the kill-switch
([agent/router_kill_switch.py](../agent/router_kill_switch.py)) Telegram-pings
the operator and writes a runnable rollback plan to
`logs/router_audit/rollback_<utc-ts>/` (`plan.json` + `rollback.sh`). The
plan checks out the pre-cutover commit (`af86069`) onto a new
`router-rollback-<ts>` branch and runs `docker compose up -d --build` to
rebuild the container against the legacy `QueryRouter`-primary code.
Snapshots are taken pre-cutover (`backups/router/phase_3_pre_cutover_*/`,
gitignored — contains raw `query_text`) and at every nightly rebuild
(`agent/router_nightly_rebuild.py`); the snapshot path is recorded in
`plan.json` for hand-recovery if the auto-plan is insufficient.

```bash
# Read a soak result (FAIL → alert only; ROLLBACK → alert + plan).
.venv/bin/python -m agent.router_kill_switch \
  --soak-result logs/router_audit/soak_<ts>.json
```

By default the executor is **plan-only**: the plan is written to disk and
the alert points at it, but no command runs. To arm auto-execution:

```bash
ROUTER_KILL_SWITCH_CONFIRM=I-UNDERSTAND-DOCKER-REBUILD \
.venv/bin/python -m agent.router_kill_switch \
  --soak-result logs/router_audit/soak_<ts>.json \
  --auto-rollback
```

To restore exemplar table state from a pre-cutover snapshot:

```bash
docker compose exec -T pepper-postgres psql -U pepper -d pepper \
  < backups/router/phase_3_pre_cutover_<ts>/router_exemplars_full.sql
```

### Hourly soak check (production wiring)

[scripts/router-soak-hourly.sh](../scripts/router-soak-hourly.sh) chains the
soak monitor and the kill-switch in one call so the operator's scheduled-tasks
runtime can fire it on a single cron entry:

```bash
# What the scheduled task runs every hour:
scripts/router-soak-hourly.sh
# Or, after operator review of an alert, armed for auto-rollback:
ROUTER_KILL_SWITCH_CONFIRM=I-UNDERSTAND-DOCKER-REBUILD \
  scripts/router-soak-hourly.sh --auto-rollback
```

It runs `python -m agent.router_soak_monitor check`, picks the most recently
written `logs/router_audit/soak_*.json`, forwards it (plus any extra flags)
to `python -m agent.router_kill_switch`, and finally runs the one-shot
`python -m agent.router_soak_completion_notifier` so the "PHASE 3 SOAK
COMPLETE" Telegram fires automatically the first hourly tick the rolling
72h window comes up clean. Exit codes mirror the kill-switch (the
authoritative soak verdict): `0` PASS, `1` FAIL (alert sent), `4`
ROLLBACK (plan + alert), `5` setup error (e.g. missing `.venv` or no
result file produced). The completion notifier is best-effort — its rc is
informational and never overrides the kill-switch's. Set
`ROUTER_SOAK_SKIP=1` to no-op the whole wrapper (maintenance windows);
set `ROUTER_SOAK_NOTIFY_SKIP=1` to skip only the completion notifier
(e.g. during kill-switch drills, so the drill's transient FAIL doesn't
re-arm the flag file).

**Operator-side registration (one-time):** the cron entry lives in the
Claude scheduled-tasks runtime, which a scheduled session itself cannot
register (the runtime refuses `create_scheduled_task` from inside a
scheduled run). From any *interactive* Claude session, run
`mcp__scheduled-tasks__create_scheduled_task` with:

- `taskId`: `pepper-router-soak-hourly`
- `cronExpression`: `0 * * * *` (top of every hour, local time)
- `notifyOnCompletion`: `false`
- `description`: `Hourly Phase 3 router soak check + kill-switch dispatch (read-only by default; fires Telegram alert on FAIL/ROLLBACK).`
- `prompt`: see [scripts/router-soak-hourly.SKILL.md](../scripts/router-soak-hourly.SKILL.md) — the canonical SKILL prompt body, kept under version control so the build agent can re-source it on demand.

Once registered, FAIL/ROLLBACK alerts surface via Telegram (the kill-switch
sends them); the scheduler's own notification channel stays silent on PASS.
Disable / inspect via `mcp__scheduled-tasks__list_scheduled_tasks` from any
Claude session. Disable when Phase 3 closes.

## Common operations

### Add a manual exemplar

```bash
.venv/bin/python -m agent.router_exemplar_seeds --manual \
  --jsonl tests/router_<topic>_seeds.jsonl
```

The JSONL keys must mirror the DB column names exactly: `query_text`,
`intent_label`, `tier`, `source_note`. (`tier` for manual seeds is `manual`;
auto-promoted exemplars get `auto`.)

### Re-run shadow replay

After adding exemplars, replay them against the historical
`routing_events` rows to update `shadow_decision_*`:

```bash
.venv/bin/python -m agent.router_shadow_replay
```

### Inspect divergences

```bash
docker compose exec pepper-postgres psql -U pepper -d pepper -c "
  SELECT regex_decision_intent, shadow_decision_intent, COUNT(*)
  FROM routing_events
  WHERE shadow_decision_intent IS NOT NULL
    AND regex_decision_intent != shadow_decision_intent
  GROUP BY 1, 2 ORDER BY 3 DESC;
"
```

### Sample + send a Gate-2-style adjudication batch

```bash
.venv/bin/python -m agent.router_adjudication --send -n 50
# operator replies via Telegram with `<id> A|B|N` lines
.venv/bin/python -m agent.router_adjudication \
  --ingest-replies <replies.txt> \
  --sample logs/router_audit/adjudication_sample_<ts>.jsonl
```

## Privacy boundary

The router runs entirely locally. No personal data leaves the machine:

- Query text → local Ollama for embedding (no cloud call)
- Embedding → local pgvector (no cloud call)
- `router_exemplars` and `routing_events` carry real `query_text` — both
  tables stay in the local Postgres only. Snapshots under `backups/router/`
  are gitignored for the same reason.
- Adjudication artifacts (`logs/router_audit/`) are gitignored; they include
  numeric stats + ID lists, never raw response bodies.

Per [GUARDRAILS.md](GUARDRAILS.md) §1, raw query text is local-only — never
sent to the Claude API or any other cloud service.
