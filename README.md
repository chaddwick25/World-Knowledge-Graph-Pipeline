# World KG & Geo Spatial Reasoning

Search places worldwide and get AI-powered answers. The app turns raw
OpenStreetMap data into a searchable knowledge base, then lets you ask
questions about any country it has processed and get grounded answers
on an interactive map.

Pick a country, run the pipeline, and ask a question like "Which cafes
are within 2km of a school?" The app parses the question, finds the
matching places, shows them on the map, and writes a short AI summary
that explains what it found and why.


## What it does

Three things, each building on the last:

1. **Processes a country.** The pipeline reads OpenStreetMap data for
   a country, builds vector embeddings of every place, aligns places
   with Wikidata, and predicts spatial relationships between them. One
   run takes a few minutes on a GPU; the results are stored and ready
   to query.

2. **Searches the results three ways.** Search by OSM tag, by name in
   any language or script, or by a full natural-language question. The
   first two are fast lookups; the third is the AI-powered mode that
   parses your question, runs it against the stored data, and
   summarizes the answer.

3. **Answers questions with grounded AI.** The AI summary cites the
   actual places, their tags, and the spatial relationships the
   pipeline predicted. It is not a generic chatbot answer. It is
   grounded in the data the pipeline produced for the country you
   selected.


## Try it

```bash
docker compose -f docker-compose.yml -f compose.override.yml up -d
cd frontend-v3 && npm install && npm run dev
```

Open the app, pick a country that is already processed (green badge),
and try one of the three search modes below.


## Three ways to search

### 1. Search by tag

Enter OSM tags as JSON (e.g. `{"amenity": "cafe"}`) or a name in any
language (e.g. `{"name": "파리바게뜨"}`). The romanizer auto-detects the
script for cross-script name matching. FastText provides semantic type
matching.

![Query by OSM Tag](frontend-v3/src/assets/Query_By_OSM_Tag.png)

### 2. Search by name

Type a name in any language or script (e.g. "paris bagueete",
"파리바게뜨", "café", "원탕"). The romanizer handles cross-script matching;
FastText adds semantic type matching.

![Query by Natural Language](frontend-v3/src/assets/Query_By_Natural_Language.png)

### 3. Ask a question (AI summary)

Type a full geospatial question (e.g. "Which bars are within 50m of
Hollywood Blvd?"). The app shows you what it understood, you approve
or edit, and it runs.

![AI summary](frontend-v3/src/assets/Kuhns_Template.png)

Here is what the experience looks like end to end:

1. **Type a question.** Anything from "Which cafes are within 2km of a
   school?" to "How far is Betelnut Cafe from Limerick?"
2. **Review what the app understood.** A confirmation modal shows the
   extracted pieces of your question: the amenity, the location, the
   radius. You can approve, edit a slot, or reject and rephrase.
3. **Watch it run.** Results stream back in stages: the parsed query,
   then the matching places, then the entity context the AI will use.
4. **Read the answer.** A short AI summary appears token by token,
   grounded in the actual places, their tags, and the spatial
   relationships the pipeline predicted. It cites what it found, not
   what it guessed.
5. **Explore the map.** Every matching place renders on the map with a
   popup showing its metadata. The answer and the map stay in sync.


*How a question becomes an answer: the app extracts the concepts in
your question, composes them into a structured plan, and maps that
plan to real data lookups. Source: [Spatial-Agent
paper](docs/papers/WernerKuhn/Spatial-Agent:%20Agentic%20Geo-spatial%20Reasoning%20with%20Scientific%20Core%20Concepts.pdf)*


## What the AI handles well

Questions that map to the data the pipeline produces: place types,
spatial relationships, semantic similarity, and cross-script names.

| Question type | Example |
|---|---|
| Find things near a place | "Which cafes are within 2km of a school?" |
| Find the nearest | "What is the nearest cafe to Shandon Bells?" |
| Compare distances | "Which is closer to Moher Cottage: Cliff Coast Coffee or the Cliffs of Moher?" |
| What is around here | "What amenities are around Tully Mill?" |
| Direction from a place | "What is west of Tullygally Tavern?" |
| How far is X from Y | "How far is Betelnut Cafe from Limerick?" |

Routing and navigation questions ("What turns do I take to get to the
pub?") are future work; there is no training data for those yet.
