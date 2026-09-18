# Sample Research Prompts (Research tab)

How to use: open the app, pick a country + snapshot, switch to the Research
tab (last tab). Type a prompt in the chat. The KE (interviewer) asks
clarifying questions one at a time; answer them. After each reply the
system builds a research brief from the interview (structured extraction
via `POST /api/nca/research/finalize/`); the blue brief box appears when
it is ready and **Run research** activates. The loop (decompose →
deterministic answers → grounded summary) streams below. After the
summary, keep chatting to revise the brief and re-run (second pass).

Place names below are the verified anchors from `sample_questions.md`
(2026-09-13). The interview runs on the 4070 (fast); the run on the 2070
(slower, 1-2 min per run).

## Worked example (Belize, snapshot 2025_12_31, 2026-09-18)

The full interview transcript, exactly as the KE produced it:

```
prompt: plan a 2-day trip to Belize City
KE:     What is your budget range for the trip?
user:   $5000
KE:     How many people are traveling with you?
user:   2
KE:     How would you like to spend your time in Belize City—relaxing, exploring, or a mix?
user:   Exploring
```

The KE never emits a brief itself; the system builds it. After the
interview (or a full prompt typed in one message), the brief box shows the
structured extraction, e.g.:

```
Plan a 2-day trip to Belize City for 2 adults with a $5000 budget, focused
on exploring.
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
| Skip-the-interview | type the full spec in one message (dates, party, interests, constraints) | KE says it is ready; brief box fills in |
| Second pass | after the summary, type feedback (e.g. "drop the museums, only cafes") → Send → Run again | revised brief, re-run works |
| Brief never appears | keep answering; brief box stays empty | console `[Research] finalize error` — extraction failed |
| Chat fails | Send → nothing streams | console `[Research] chat error`; `chat status` never leaves ready |
| Run fails | Run → red alert | console `[Research] question:` lines show the failing index and error |
| GPU split | `nvidia-smi` during the run | interview on GPU 1 (4070), run loads GPU 0 (2070) |
| Summary honesty | Run with a sparse country | summary says "no X found" instead of inventing |

## Console diagnostics

All logs carry the `[Research]` prefix (`@ <ms>` timestamps). The useful
ones during a test: `chat status` (state transitions), `chat error`
(fetch/parse failures), `chat message` (each turn, first 200 chars),
`finalize: structured|fallback <brief>` (brief build), `brief ready` /
`brief cleared`, `run start, source: brief|lastUserPrompt` (which prompt
the run used), `plan: N questions`, `question: <i> <template> count: <n>
<error>`, `done, summary len: N elapsed: Xs errors: M`.

## Countries (anchors verified in sample_questions.md)

| Code | Country | Anchors | Starter prompt |
|---|---|---|---|
| BZ | Belize | Belmopan, Belize City, Caye Caulker | plan a 2-day trip to Belize City |
| CU | Cuba | Havana, Varadero | plan a 3-day trip to Havana |
| CV | Cape Verde | Praia, Mindelo | plan a 2-day trip to Praia |
| CY | Cyprus | Nicosia, Limassol, Larnaca, Paphos | plan a 2-day trip to Limassol |
| GT | Guatemala | Guatemala City, Antigua, Lake Atitlan | plan a 3-day trip to Antigua |
| IE | Ireland | Limerick, Cliffs of Moher, Dublin, Cork, Galway | plan a 2-day trip to Dublin |
| IS | Iceland | Reykjavik, Akureyri, Blue Lagoon | plan a 2-day trip to Reykjavik |
| IT | Italy | Rome, Venice, Milan, Florence, Naples, Colosseum | plan a 3-day trip to Florence |
| JM | Jamaica | Kingston, Montego Bay, Ocho Rios, Negril | plan a 2-day trip to Montego Bay |
| KR | South Korea | Seoul, Busan, Incheon | plan a 2-day trip to Seoul |
| LK | Sri Lanka | Colombo, Kandy, Galle, Jaffna | plan a 2-day trip to Kandy |
| MC | Monaco | Monte Carlo (small, 10,620 entities) | plan a 1-day trip to Monte Carlo |
| MX | Mexico | Mexico City, Cancun, Guadalajara, Tulum | plan a 3-day trip to Cancun |
| NI | Nicaragua | Managua, Granada, Leon, Masaya | plan a 2-day trip to Granada |
| NL | Netherlands | Amsterdam, Rotterdam, The Hague, Utrecht | plan a 2-day trip to Amsterdam |

Useful second-pass feedback phrases: "we have no car, keep everything
within 1km", "drop the museums, only cafes", "budget is actually $2000",
"add a beach day".
