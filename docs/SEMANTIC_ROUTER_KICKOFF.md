# Semantic Router Build — Kickoff Prompt

Self-contained prompt to feed a local Claude Code invocation. Each run advances the semantic router migration by one chunk of work.

Companion to:
- [SEMANTIC_ROUTER_MIGRATION.md](SEMANTIC_ROUTER_MIGRATION.md) — the plan
- [SEMANTIC_ROUTER_BUILD_PROMPT.md](SEMANTIC_ROUTER_BUILD_PROMPT.md) — the routine spec

---

## The Prompt (copy-paste verbatim into `claude -p`)

```
You are Pepper's semantic router build agent. Your task is to advance the
migration by exactly ONE task end-to-end, then exit.

Read these files in order before doing anything:
  1. /Users/jack/Developer/Pepper/CLAUDE.md
  2. /Users/jack/Developer/Pepper/docs/GUARDRAILS.md
  3. /Users/jack/Developer/Pepper/docs/INFRA_GUIDELINES.md
  4. /Users/jack/Developer/Pepper/docs/SEMANTIC_ROUTER_BUILD_PROMPT.md
  5. /Users/jack/Developer/Pepper/docs/SEMANTIC_ROUTER_MIGRATION.md
  6. /Users/jack/Developer/Pepper/docs/router_build_state.json
     (may not exist yet — if missing, you are starting fresh on Phase 0
     Iteration 1; create it as part of this run)

Then execute "The Loop" section of the build prompt:
  - Step 0: lockfile check — exit immediately if /Users/jack/Developer/
    Pepper/.router_build.lock is held by an active run (mtime <3h).
    Always release the lockfile on exit.
  - Step 2: pick the next incomplete TASK in the current phase, then
    advance it END-TO-END in this run:
      * Implement
      * Review (3 cycles): self-simplify, architectural, test coverage +
        integration
      * Test (3 cycles): unit, replay/stability, end-to-end live
      * Commit locally (no push), with message format from Step 6
      * Update docs/router_build_state.json
  - Soft time budget ~90 min. If a run would clearly exceed 3 hours,
    stop early, commit what works, write notes, release lockfile.
  - Follow Hard Rules 1-11 from the build prompt without exception.
  - Output the 5-line summary the build prompt's Step 7 requires.

Blocked conditions (set blocked: true, Telegram-ping via
agent/telegram_bot.py JARViSTelegramBot.push(), release lockfile, exit):
  - Privacy violation detected in diff
  - 3 fix attempts on the same phase exhausted
  - Eval regression triggered an auto-rollback
  - Phase 0 routing-fixable share <40% (branch decision)
  - Any exit criterion that the spec marks unclear or wrong

Do NOT:
  - Modify the migration plan or build prompt to make exit criteria easier
  - Push commits to remote
  - Skip the privacy boundary grep before committing
  - Continue past blocked: true
  - Leave the lockfile behind on exit

Begin.
```

---

## Recommended Invocation

```bash
cd /Users/jack/Developer/Pepper && \
  claude \
    --model claude-opus-4-7 \
    --dangerously-skip-permissions \
    -p "$(cat docs/SEMANTIC_ROUTER_KICKOFF.md | sed -n '/^```$/,/^```$/p' | sed '1d;$d' | head -n -1)" \
    >> logs/router_build/run_$(date +%Y-%m-%dT%H-%M-%S).log 2>&1
```

Adjust the `sed` extraction to grab the prompt block from this file, OR
save the prompt to its own `.txt` file and `cat` that.

Cleaner: save just the prompt to `bin/router-build-kickoff.txt`, then:

```bash
cd /Users/jack/Developer/Pepper && \
  claude \
    --model claude-opus-4-7 \
    --dangerously-skip-permissions \
    -p "$(cat bin/router-build-kickoff.txt)" \
    >> logs/router_build/run_$(date +%Y-%m-%dT%H-%M-%S).log 2>&1
```

### CLI flags rationale

| Flag | Why |
|---|---|
| `--model claude-opus-4-7` | Opus 4.7 with 1M context for long migration plan + state |
| `--dangerously-skip-permissions` | Bypass-all per the agreed routine config |
| `-p` | Single-shot, non-interactive |
| `>> logs/router_build/...` | Persistent run log per invocation |

Reasoning effort `high` is configured at the model level — set via your
Claude Code config (`~/.claude/settings.json`) or pass via env var if
available in your version.

---

## Sample launchd job (macOS)

Save to `~/Library/LaunchAgents/com.jack.pepper-router-build.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jack.pepper-router-build</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>cd /Users/jack/Developer/Pepper &amp;&amp; claude --model claude-opus-4-7 --dangerously-skip-permissions -p "$(cat bin/router-build-kickoff.txt)" >> logs/router_build/run_$(date +%Y-%m-%dT%H-%M-%S).log 2>&1</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/jack/Developer/Pepper/logs/router_build/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jack/Developer/Pepper/logs/router_build/launchd_stderr.log</string>
</dict>
</plist>
```

Load with:
```bash
launchctl load ~/Library/LaunchAgents/com.jack.pepper-router-build.plist
```

Unload (to pause the routine):
```bash
launchctl unload ~/Library/LaunchAgents/com.jack.pepper-router-build.plist
```

---

## Alternative: crontab

```
10 * * * * cd /Users/jack/Developer/Pepper && /usr/local/bin/claude --model claude-opus-4-7 --dangerously-skip-permissions -p "$(cat /Users/jack/Developer/Pepper/bin/router-build-kickoff.txt)" >> /Users/jack/Developer/Pepper/logs/router_build/run_$(date +\%Y-\%m-\%d_\%H-\%M-\%S).log 2>&1
```

Note: cron requires `%` escaped as `\%` in date format strings. launchd is
generally more reliable on macOS than cron.

---

## Manual one-shot (testing the prompt before scheduling)

```bash
cd /Users/jack/Developer/Pepper
claude --model claude-opus-4-7 --dangerously-skip-permissions \
  -p "$(cat bin/router-build-kickoff.txt)"
```

Run this once to verify the routine works end-to-end before scheduling. The
first manual run will create `docs/router_build_state.json` and start
Phase 0 Task 1.

---

## Pre-flight checklist before first run

- [ ] `bin/router-build-kickoff.txt` exists with the prompt block above
- [ ] `logs/router_build/` directory exists (or invocation will create it)
- [ ] `claude` CLI available in PATH and authenticated
- [ ] Pepper's Telegram bot configured (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` set in `.env`)
- [ ] Pepper's Postgres + pgvector running locally (the agent will need to apply migrations starting in Phase 1)
- [ ] You've done one manual test run and it produced a valid `router_build_state.json` and a 5-line summary

---

## Stopping the routine mid-migration

```bash
# Pause (the next scheduled run won't fire):
launchctl unload ~/Library/LaunchAgents/com.jack.pepper-router-build.plist

# Hard-block (any future run, even manual, will exit at Step 1):
echo '{"blocked": true, "blocked_reason": "user paused"}' > /Users/jack/Developer/Pepper/docs/router_build_state.json
# (merge with existing state — don't blow it away)
```

To resume: clear `blocked: false` in state file, reload launchd job.
