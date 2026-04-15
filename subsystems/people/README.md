# People Subsystem — Stub

**Status**: Future integration  
**Source project**: `~/Developer/corela`

This subsystem will eventually expose Corela's relationship intelligence (61 tools, pgvector semantic search, relationship scoring) as a standard Pepper subsystem interface.

## Integration is NOT Phase 1

Corela exists and is functional, but:

1. The relationship data hasn't been fully curated
2. Pepper needs to be operational first to know what relationship data it actually needs
3. Pepper will drive Corela's evolution based on observed gaps — not the other way around

## What Pepper Will Drive in Corela Over Time

- Populate contacts from iMessage, email, and calendar attendees Pepper encounters
- Flag relationship health issues based on Pepper's broader life context (not just message frequency)
- Recommend Corela feature improvements based on questions Pepper can't answer
- Provide richer context for people who appear across multiple life domains

## When Integration Happens

The trigger is: Pepper repeatedly fails to answer a people/relationship question and the answer exists in Corela's data. That failure surfaces the integration as a real need, not a hypothetical one.

## Interface Contract (to be implemented)

```text
GET  /health
GET  /tools           # Subset of Corela's 61 tools relevant to Pepper
POST /tools/{name}    # Execute tool
GET  /status
```

Port: 8001 (Corela's existing port)
