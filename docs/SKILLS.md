# Skills

Skills are markdown workflows the model loads on demand. The system prompt lists every installed skill in a one-line index; the model decides which one is relevant for a turn and calls `skill_view` to load the full body. New skills come from external registries via `skill_search` and `skill_install`.

## File layout

```
~/.pepper/skills/<name>/SKILL.md       # user installs (preferred)
~/.pepper/skills/<name>/references/*.md  # optional sibling markdown
./skills/<name>.md                     # repo legacy (flat form, still loaded)
```

Both folder form (`<name>/SKILL.md`) and flat form (`<name>.md`) are recognized. User installs take precedence over repo files on name collision.

Frontmatter:

```yaml
---
name: email_triage
description: Sort an inbox by importance and propose actions
version: 1
references:           # optional; markdown files only
  - references/threading.md
---
```

## Tools

| Tool | Side effects | What it does |
|---|---|---|
| `skill_view(name, ref?)` | no | Returns the SKILL.md body, or a referenced markdown file |
| `skill_search(query, source?)` | no | Substring match over names + descriptions across all registries |
| `skill_install(name, source)` | yes | Copies markdown from a registry into `~/.pepper/skills/<name>/` |
| `skill_registry_update()` | yes | Fetches the anthropics mirror and checks out the pinned SHA |

## Registries

- **anthropics** — `git clone` of `anthropics/skills`, mirrored at `~/.pepper/skill-registry/anthropics/`. Pinned by `PEPPER_ANTHROPIC_SKILLS_SHA` in `.env`; the update tool refuses to run without it. Source: `PEPPER_ANTHROPIC_SKILLS_REPO` (defaults to the public repo).
- **hermes** — read live from `PEPPER_HERMES_SKILLS_PATH` (Docker default: `/data/hermes-skills`). Skipped silently if absent.

Local installs always win on name collision. Registries are read-only.

## Trust model

- **Markdown only.** `skill_install` copies SKILL.md and any `references/*.md` declared in frontmatter; everything else (`scripts/`, `assets/`, executables) is dropped. No code from external sources executes.
- **SHA pinning.** The anthropics mirror is checked out at a specific commit, not `main`. A failed fetch or checkout wipes the mirror so an unpinned tree is never queryable.
- **Path containment.** Skill names that are not a single safe path component (`..`, `/`, `\`, leading `.`) are rejected. Reference paths are bounded to the skill folder on both source and destination. Symlinked SKILL.md or references are refused.
- **Dotdir skip.** Registry walks ignore any path under a dot-prefixed directory (e.g. `.git/`).

Skill bodies are still attacker-influenceable text — they reach the model as prompts. Markdown-only stops code execution; it doesn't sanitize prompt-injection content. Treat the skill author the way you'd treat any other prompt source.

## Lifecycle

Skills are loaded once at startup and re-read by `reload_skills()` after a successful `skill_install`. The reviewer's lookup is swapped atomically so a background review of one turn never sees a half-built skill map.

The post-turn skill reviewer keys off `skill_view` calls observed in the turn's `tool_calls` — it reviews skills the model actually consulted, not skills that matched some heuristic.

## Why no trigger matcher / model tier

Earlier versions of this system kept a substring trigger-phrase matcher and a `model: frontier` upgrade hook. Both are gone:

- The matcher is replaced by the index — the model picks the relevant skill by reading descriptions, which scales further and produces fewer false positives than literal substrings.
- The frontier upgrade is dormant in current Pepper (`DEFAULT_FRONTIER_MODEL` is aliased to the local model). Model selection belongs to `agent/llm.py`, not skill frontmatter; if a real frontier is wired in later, the routing logic lives there.
