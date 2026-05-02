# Router label conflict — manual exemplars vs eval set

Generated 2026-04-28 to support Phase 2 iter 15 adjudication.

## Headline

The manual file has **zero** `general_chat` rows. The eval set has **30** (out of 100).
That's the conflict in one number — the manual labels treat almost every "soft" query
as actionable; the eval set treats them as chitchat.

| intent              | manual (51) | eval (100) |
|---------------------|-------------|------------|
| action_items        |          11 |         12 |
| capability_check    |           3 |          2 |
| conversation_lookup |          10 |         17 |
| cross_source_triage |          11 |          5 |
| general_chat        |           0 |         30 |
| inbox_summary       |           3 |          4 |
| person_lookup       |           8 |         17 |
| schedule_lookup     |           5 |         13 |

The categories where the regression hit (Health 9→6, Finance 6→4, Proactive 5→3) are
the ones where this gap is loudest.

## Side-by-side, by conflict cluster

### 1. Fitness / self-tracking — manual=`action_items`, eval Health=`general_chat`

| MANUAL says     | query                                                  |
|-----------------|--------------------------------------------------------|
| action_items    | Track my running progress over the past two weeks     |
| action_items    | How many pickleball sessions did I log this month?    |
| action_items    | Show me what workouts I completed last week           |

| EVAL says      | query                                                  |
|----------------|--------------------------------------------------------|
| general_chat   | What's my resting heart rate this week?               |
| general_chat   | Did I work out yesterday?                             |
| general_chat   | How many hours did I sleep last night?                |
| general_chat   | Am I on track with my fitness goals?                  |
| general_chat   | What was my Oura readiness score yesterday?           |
| general_chat   | Should I rest today based on my stats?                |
| action_items   | Track my pickleball sessions this month               |
| general_chat   | How is my golf handicap trending?                     |
| general_chat   | What workouts have I logged this week?                |
| general_chat   | Remind me what supplements I take                     |

Note row 7: **the eval set itself is internally inconsistent** — "Track my pickleball
sessions" → action_items, but "What workouts have I logged this week?" → general_chat.
That's an eval-set bug, not just a labeling-philosophy difference.

### 2. Meal log — manual=`action_items`, eval Meal=`general_chat`

| MANUAL says   | query                                  |
|---------------|----------------------------------------|
| action_items  | List the meals I logged yesterday      |

| EVAL says           | query                                              |
|---------------------|----------------------------------------------------|
| general_chat        | What should I cook for dinner tonight?            |
| conversation_lookup | What did Susan say about dinner plans?            |
| general_chat        | Suggest a healthy lunch given my schedule today   |
| general_chat        | Find a sushi spot in Palo Alto for tonight        |
| general_chat        | What's a good restaurant for a date with Susan?   |
| schedule_lookup     | Do we have a dinner reservation Saturday?         |
| general_chat        | What did the kids eat for school lunch this week? |
| action_items        | Plan Sunday family dinner                          |
| general_chat        | Find vegan recipes my mom would like              |
| conversation_lookup | When is the last time we ordered DoorDash?        |

The manual row "List the meals I logged yesterday" is closest to the eval row "What
did the kids eat for school lunch this week?" — different subject, but same shape
(retrieve a meal log). Manual=action_items, eval=general_chat.

### 3. Knowledge / decision recall — manual=`conversation_lookup`, eval Knowledge=`general_chat`

| MANUAL says         | query                                                  |
|---------------------|--------------------------------------------------------|
| conversation_lookup | What did I decide about the Q2 product launch?       |
| conversation_lookup | Remind me what we landed on for the pricing model    |
| conversation_lookup | What were the open questions from last sprint review?|
| conversation_lookup | What was the conclusion on the hiring pipeline plan? |
| conversation_lookup | What feedback did I get on the architecture proposal?|

| EVAL says           | query                                                |
|---------------------|------------------------------------------------------|
| general_chat        | What is Pip Labs?                                    |
| general_chat        | Remind me what Poseidon does                         |
| general_chat        | What's my MBTI type?                                 |
| general_chat        | What's my home address?                              |
| general_chat        | How old is my mom?                                   |
| general_chat        | When did my dad pass away?                           |
| conversation_lookup | What was the decision I made about the Q2 launch?    |
| conversation_lookup | What did I write in memory about Connor's volleyball?|
| conversation_lookup | Search my notes for 'hypertrader'                    |
| capability_check    | What do you know about me, Pepper?                   |

This one looks more **compatible than it appears**: the eval set's `general_chat`
Knowledge rows are factual identity recall ("home address", "MBTI"), while its
`conversation_lookup` rows are decision/notes recall — exactly what the manual
exemplars target. The clash is partial; the manual rows would not directly contradict
these specific eval rows. (But the embedding nearest-neighbor effect can still pull
queries the wrong way.)

### 4. Planning synthesis — manual=`cross_source_triage`, eval Proactive split between `action_items`/`cross_source_triage`

| MANUAL says         | query                                                  |
|---------------------|--------------------------------------------------------|
| cross_source_triage | Am I on track for my fitness goals this quarter?     |
| cross_source_triage | Should I be worried about anything happening this week?|
| cross_source_triage | Give me a sense of how the week is shaping up         |
| cross_source_triage | Where am I behind right now and what should I focus on?|
| cross_source_triage | Anything I'm forgetting that I should follow up on?  |

| EVAL says           | query                                                |
|---------------------|------------------------------------------------------|
| action_items        | What should I focus on this morning?                 |
| action_items        | What am I forgetting to do today?                    |
| cross_source_triage | Anything urgent I should know about?                 |
| action_items        | Brief me on my day                                   |
| person_lookup       | Who should I check in with this week?                |
| action_items        | What deadlines are coming up at work?                |
| action_items        | Are there any college deadlines I'm missing for Matthew?|
| cross_source_triage | What's slipping with the kids' summer plans?         |
| cross_source_triage | Has anything from Susan's family needed attention?   |
| action_items        | Give me a Monday morning game plan                   |

This is the cleanest *philosophical* disagreement: "what should I focus on?" /
"what am I forgetting?" — manual treats them as cross-source triage; eval treats
them as action_items.

### 5. Daily prep — manual=`cross_source_triage`, eval Proactive (same as #4)

| MANUAL says         | query                                                  |
|---------------------|--------------------------------------------------------|
| cross_source_triage | Anything brewing that I should pack for next week?    |
| cross_source_triage | Pull together my prep for the upcoming Boston trip    |
| cross_source_triage | What should I be aware of before this week's family weekend?|
| cross_source_triage | Help me prep for tomorrow's day                       |
| cross_source_triage | Give me a heads-up on anything I'd want to know for tonight|

Mostly compatible with eval (both use `cross_source_triage` for travel-prep
synthesis), but evicted by manual rows added under pattern 8 (#4 above) bleeding
embedding-space into "what should I focus on" → action_items eval rows.

### 6. Travel + person — manual=`person_lookup`, eval Travel split

| MANUAL says   | query                                                  |
|---------------|--------------------------------------------------------|
| person_lookup | Where is Susan staying during the Orlando trip?       |
| person_lookup | Did Matthew confirm the East Coast meet-up with Senky?|
| person_lookup | What hotel did Anna book for the volleyball tournament?|
| person_lookup | Where is Jessie meeting up with the family on the East Coast?|
| person_lookup | What is Susan's flight number for the Orlando departure?|

| EVAL says           | query                                                |
|---------------------|------------------------------------------------------|
| schedule_lookup     | When does Matthew fly to Boston for the Harvard program?|
| person_lookup       | What hotel is Susan staying at for the Orlando tournament?|
| schedule_lookup     | Remind me what flights I have booked next month      |
| general_chat        | Should I plan a Hawaii trip with the family this summer?|
| general_chat        | How long will it take to drive from Cupertino to LAX?|
| schedule_lookup     | Do I have any travel coming up that I should pack for?|
| person_lookup       | Where am I supposed to meet Matthew on the East Coast?|
| cross_source_triage | What's the latest on the AAU volleyball tournament?  |
| conversation_lookup | Did Susan confirm the Four Points Sheraton check-in? |
| schedule_lookup     | Show me the itinerary for our July trip              |

Compatible-ish — eval already routes "What hotel is Susan staying at" → person_lookup,
matching the manual rule. The drift comes from queries like "When does Matthew fly to
Boston" (eval=schedule_lookup) being pulled toward person_lookup by the new exemplars.

## What the operator actually has to decide

Three philosophical questions, in priority order:

1. **Self-tracking queries** ("how many workouts this week", "what did I sleep")
   → `action_items` (a tool can answer it from health logs) **or** `general_chat`
   (it's a casual question, conversational answer is fine)?

2. **"What should I focus on?" / "What am I forgetting?"**
   → `action_items` (a triage of open commitments) **or** `cross_source_triage`
   (multi-source synthesis)?

3. **Decision/notes recall** ("what did I decide about Q2 launch")
   → `conversation_lookup` is consistent across both files; no real conflict.

The first two are the load-bearing ones. The eval set's own internal inconsistency
on (1) — "Track my pickleball sessions" = action_items but "What workouts have I
logged" = general_chat — suggests the eval set itself needs a pass once you pick a
direction.
