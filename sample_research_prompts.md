# Sample Research Prompts (general researcher, inside the OSM RAG tab)

How to use: open the app, pick a country + snapshot, switch to the OSM RAG
tab, then select the **Researcher** sub-mode (third radio — Question |
OSM Tag Query | Researcher; the standalone Research tab was removed
2026-10-01). Type a prompt in the chat. The KE (interviewer) asks
clarifying questions one at a time; answer them. After each reply the
system builds a research brief from the interview (structured extraction
via `POST /api/nca/research/finalize/`); the blue brief box appears when
it is ready and **Run research** activates. The loop (decompose →
deterministic answers → grounded summary) streams below. After the
summary, keep chatting to revise the brief and re-run (second pass).

The loop is recipe-driven (`research_recipes.py`). The only recipe is the
**place report** — a domain-neutral report over the classes OSM maps most
completely (restaurants and food, shops, services, public transit,
infrastructure). The trip-planning recipe was removed 2026-10-01 (tourism
strained the data; the general researcher leads with data-supported
domains), so every prompt lands on the place report. The run's `done`
payload carries `recipe: place_report` (console: `done, ...`).

Place names below are the verified anchors from `sample_questions.md`
(2026-09-13). The interview runs on the 4070 (fast); the run on the 2070
(slower, 1-2 min per run).

## Worked example (Belize, snapshot 2025_12_31, 2026-09-18)

The full interview transcript, exactly as the KE produced it:

```
prompt: what restaurants and shops are around Belize City?
KE:     How far out from Belize City should the search go?
user:   within 3km
KE:     Any type of restaurant or shop in particular, or the full mix?
user:   the full mix
```

The KE never emits a brief itself; the system builds it. After the
interview (or a full prompt typed in one message), the brief box shows the
structured extraction, e.g.:

```
Research Belize City (within 3km) focusing on restaurants and shops.
```

## Resolved issue (2026-09-18): runaway BRIEF list

Observed: the KE replied with a hallucinated place/distance list instead
of a brief ("...25 km from Placencia, 35 km from San Pedro, 120 km from
Ambergris Caye..." escalating by 50 km to 1850 km). Fixed by removing
free-form brief emission entirely: the KE only interviews, and the brief
comes from `chat_json` field extraction (`format: "json"`) plus a
deterministic render, so the runaway list cannot recur. Fail-soft:
extraction failure or LLM down falls back to the first user message
(`source: fallback`, shown in the brief box).

Console check: `[Research] finalize: structured <brief>` is the passing
signal; `[Research] finalize error` or `finalize: fallback` means the
extraction path failed.

## Test scenarios

| Scenario | Steps | Pass signal |
|---|---|---|
| Happy path | prompt → answer 2-3 questions → brief box appears → Run | per-question cards with template badges, grounded summary |
| Agent-decided radius | a decomposed question carries a small radius and returns nothing | the loop does NOT silently widen it (escalation removed 2026-10-01) — a visible follow-up (probe / class-swap / replan) re-asks instead; questions carry the planner's `radius_m` |
| Skip-the-interview | type the full spec in one message (area, focus, scope) | KE says it is ready; brief box fills in |
| Second pass | after the summary, type feedback (e.g. "drop the shops, only cafes") → Send → Run again | revised brief, re-run works |
| Brief never appears | keep answering; brief box stays empty | console `[Research] finalize error` — extraction failed |
| Chat fails | Send → nothing streams | console `[Research] chat error`; `chat status` never leaves ready |
| Run fails | Run → red alert | console `[Research] question:` lines show the failing index and error |
| GPU split | `nvidia-smi` during the run | interview on GPU 1 (4070), run loads GPU 0 (2070) |
| Summary honesty | Run with a sparse country | summary says "no X found" instead of inventing |
| Recipe | any prompt → Run | done payload `recipe: place_report` (trip_plan removed 2026-10-01); a failed "getting around" question probes with bus stops kept as evidence (transit is the signal there) |

## Console diagnostics

All logs carry the `[Research]` prefix (`@ <ms>` timestamps). The useful
ones during a test: `chat status` (state transitions), `chat error`
(fetch/parse failures), `chat message` (each turn, first 200 chars),
`finalize: structured|fallback <brief>` (brief build), `brief ready` /
`brief cleared`, `run start, source: brief|lastUserPrompt` (which prompt
the run used), `plan: N questions`, `question: <i> <template> count: <n>
<error>`, `radius escalation: <q> -> <wider>` (small-radius empty
widen, 2026-09-19), `done, summary len: N elapsed: Xs errors: M`.
Every run also has a `trace_id` (in the SSE `done` payload and the
`X-Trace-Id` header) — set `TRACE_SINK=langfuse` to push the run's LLM
spans to the self-hosted Langfuse
(`docs/plans/completed/UNIFIED_LLM_TRACE_PLAN.md`).

## Countries (anchors verified in sample_questions.md)

| Code | Country | Anchors | Starter prompt (place report) |
|---|---|---|---|
| BZ | Belize | Belmopan, Belize City, Caye Caulker | How well is Belize City served by public transit? |
| CU | Cuba | Havana, Varadero | Overview the restaurants and shops in Havana |
| CV | Cape Verde | Praia, Mindelo | What services are near Praia? |
| CY | Cyprus | Nicosia, Limassol, Larnaca, Paphos | Where are the fuel stations around Nicosia? |
| GT | Guatemala | Guatemala City, Antigua, Lake Atitlan | How well is Guatemala City served by public transit? |
| IE | Ireland | Limerick, Cliffs of Moher, Dublin, Cork, Galway | Overview the restaurants and shops in Dublin |
| IS | Iceland | Reykjavik, Akureyri, Blue Lagoon | What services are near Reykjavik? |
| IT | Italy | Rome, Venice, Milan, Florence, Naples, Colosseum | Where are the fuel stations around Rome? |
| JM | Jamaica | Kingston, Montego Bay, Ocho Rios, Negril | How well is Kingston served by public transit? |
| KR | South Korea | Seoul, Busan, Incheon | Overview the restaurants and shops in Seoul |
| LK | Sri Lanka | Colombo, Kandy, Galle, Jaffna | What services are near Colombo? |
| MC | Monaco | Monte Carlo (small, 10,620 entities) | Where are the fuel stations around Monte Carlo? |
| MX | Mexico | Mexico City, Cancun, Guadalajara, Tulum | How well is Mexico City served by public transit? |
| NI | Nicaragua | Managua, Granada, Leon, Masaya | Overview the restaurants and shops in Managua |
| NL | Netherlands | Amsterdam, Rotterdam, The Hague, Utrecht | How well is Amsterdam served by public transit? |

Useful second-pass feedback phrases: "zoom out to 5km", "drop the shops,
only cafes", "add public transit", "only fuel stations and parking".
