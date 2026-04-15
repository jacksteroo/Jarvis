# Pepper — Agent Framework Evaluation

The agent runtime is the most volatile architectural decision. It will be replaced at least once as the ecosystem matures. Design accordingly.

---

## The Landscape (as of 2026)

### Hermes (Nous Research)

**Status**: Strong candidate for Pepper Core  
**Repo**: <https://github.com/NousResearch/hermes-agent>  

**Strengths**:

- Open source, MIT license, local-first
- Multi-platform messaging out of the box (Telegram, Discord, Slack, WhatsApp, Signal, Email, CLI)
- Persistent learning — generates skills from observed patterns
- Natural language scheduling ("remind me to follow up with X on Thursday")
- Sub-agent delegation — can spawn parallel agents
- Sandbox flexibility: local, Docker, SSH backends

**Weaknesses**:

- Early-stage — documentation and stability are still maturing
- No native MCP support (as of evaluation)
- Skill ecosystem is nascent

**Integration path**: Write Hermes skills (Python functions) that call Pepper subsystem REST APIs. Hermes handles the orchestration, scheduling, and messaging layers; subsystems handle data.

---

### Letta / MemGPT

**Status**: Strong candidate for persistent memory layer  
**Repo**: <https://github.com/cpacker/MemGPT>  

**Strengths**:

- Specifically designed for long-running agents with persistent memory
- Tiered memory architecture (working, recall, archival) — exactly what Pepper needs
- Self-editing memory: agent manages its own memory, compresses old data
- Local deployment supported
- Active development, stable API

**Weaknesses**:

- Primarily focused on memory, not full agent orchestration
- Less opinionated about multi-platform messaging
- Heavier setup than simpler frameworks

**Integration path**: Use Letta as Pepper Core's memory backend; wrap with custom orchestration for scheduling and proactivity. Hermes handles the messaging interfaces.

---

### AutoGen (Microsoft)

**Status**: Watch — not recommended for Phase 1  
**Repo**: <https://github.com/microsoft/autogen>  

**Strengths**:

- Multi-agent conversation framework
- Well-documented, large community
- Good for complex multi-agent workflows

**Weaknesses**:

- Microsoft-oriented; less local-first focus
- Heavier framework for personal assistant use case
- Not optimized for long-running persistent agents

---

### LangGraph

**Status**: Watch — not recommended for Phase 1  

**Strengths**:

- Graph-based agent workflows, very flexible
- Good for complex multi-step reasoning chains

**Weaknesses**:

- Verbose configuration
- Overkill for early phases
- LangChain ecosystem has had abstraction problems

---

### OpenHands (formerly OpenDevin)

**Status**: Useful for maintenance/code agents, not Pepper Core  
**Repo**: <https://github.com/All-Hands-AI/OpenHands>  

**Strengths**:

- Excellent at software engineering tasks (reading code, writing code, running tests)
- Computer use capabilities — can operate any GUI application
- This is the "OpenClaw" capability: agentic computer use that bypasses API restrictions

**Role in Pepper**: maintenance agents that read and update the codebase; integration agents that read data from apps without official APIs (reading iMessage by looking at the screen, etc.)

**Not suitable for**: Pepper Core orchestration, personal assistant tasks

---

## Recommended Phase 1 Architecture

```text
Hermes                    ← Orchestration, scheduling, messaging interfaces
  └── calls via REST ──→  Pepper Core Service (custom Flask/FastAPI)
                            ├── Life context document
                            ├── Tool router → subsystems
                            └── Memory backend (Letta)

OpenHands                 ← Maintenance agents, code updates, integration agents
Claude Code               ← Development, scheduled maintenance tasks
```

This keeps Hermes as the user-facing agent (because its multi-platform messaging is immediately valuable) while custom Pepper Core service handles the personal intelligence layer.

---

## Migration Path

The agent framework will change. Design for it:

1. **Pepper Core exposes a stable REST API** — regardless of what agent framework calls it
2. **Agent framework is the client** — Hermes today, something else tomorrow
3. **Life context and memory live in Pepper Core's database** — not inside the agent framework's storage
4. **Subsystems are oblivious** to which agent framework is orchestrating them

When a better agent framework arrives (and it will), the migration is:

- Swap the orchestration client (Hermes → Pepper v2)
- Pepper Core service stays unchanged
- All subsystems stay unchanged
- Life context and memory survive intact

This is the zero-downtime upgrade principle applied to the agent layer itself.

---

## The Pepper Vision

The current open-source agent ecosystem is at the Hermes/Letta/AutoGen stage. The trajectory points toward something much more integrated:

- **Ambient awareness**: always listening (local Whisper), acts on voice commands without wake word
- **Multimodal**: reads screens, understands images, processes video
- **Predictive**: acts before you ask based on learned patterns
- **Embodied**: potentially integrated with home automation, calendar hardware, etc.

This doesn't require AGI — it requires sufficient context richness + reliable tool execution + low latency local inference. All three are arriving on the current trajectory.

Pepper is built to be the persistent identity that survives agent framework replacements. The intelligence is the context, not the model or the orchestrator.
