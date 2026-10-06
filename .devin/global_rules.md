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

Security review models (empirical, 2026-10-05):
- Benchmarked public "offensive-security" LLM fine-tunes — 3B
  `fawazo/qwen2.5-coder-3b-pentest-gguf` and 8B
  `LLM-PBE/Llama3.1-8b-instruct-LLMPC-Red-Team` (mradermacher i1 repack; a
  kali-pentester 8B degenerated on a chat-template mismatch and was
  excluded) — reviewing a self-hosted web deployment's reverse-proxy +
  middleware configs. Observed behavior:
  - 8B red-team: defaulted to a DEFENSIVE posture (mitigation / monitoring
    advice) and misread the host allowlist as "allows any host".
  - The 3B was the most useful (1 real finding, 1 false alarm); the finding
    restated an accepted-risk comment already in nginx.conf.
- None outranked a careful manual review. Treat these models as brainstorming
  assistants; verify every claim against the actual config before acting.
- The 3B's finding (spoofed `Host: localhost` flipping middleware trust via
  the public allowlist) was FIXED 2026-10-05: the public edge allowlist is
  now funnel-hostname-only. Enforced by
  `backend/tests/unit/test_nginx_trust_invariants.py` + live probes in
  `deploy/probes/`.
- Full bake-off writeup: `docs/Schematics/08_Agent_MCP_LLM/07_Security_Review_Models.md`.
