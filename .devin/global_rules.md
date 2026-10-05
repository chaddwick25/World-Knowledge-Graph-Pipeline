# WorldKG Pipeline — Project Rules

Project guidance for agent sessions in this repository. The documentation
is organized in five places:

- `README.md` — layman overview and walkthrough: what the app does, one
  example query. The entry point for anything user-facing.
- `docs/SETUP.md` — operator setup: Docker Compose, database extensions,
  migrations, planet init, romanization, workers, fresh-DB reset.
- `docs/Schematics/` — implementation with file:line refs, organized in 8
  categories: `01_Encoder`, `02_Celery`, `03_Postgres`,
  `04_ETL_Django_Vue_Primitives`, `05_Learned_Layer`, `06_IGEA_USLP`,
  `07_Romanizer`, `08_Agent_MCP_LLM`. Start at `docs/Schematics/README.md`.
- `docs/preserving_the_process/` — why it was built this way: process
  narrative and design decisions (13 docs).
- `docs/papers/` — the papers the pipeline implements (WorldKG, GeoVectors,
  IGEA, USLP, MapQA, Spatial-Agent, functional maps), grouped under
  `Maps/`, `WernerKuhn/`, `WorldKG/`.

Conventions and troubleshooting: `AGENTS.md`.

Working rules:
- Match the style and patterns of the file you are editing.
- Run the relevant test suites before declaring a task complete.
- Do not create git commits; leave staging and commits to the maintainer.
