# WorldKG Pipeline: Project Rules

Project guidance for agent sessions in this repository. The documentation
is organized in five places:

- `README.md`: overview and walkthrough: what the app does, one
  example query. The entry point for anything user-facing.
- `docs/SETUP.md`: operator setup: Docker Compose, database extensions,
  migrations, planet init, romanization, workers, fresh-DB reset.
- `docs/` app folders: implementation with file:line refs, organized by
  owning app/service: `core/`, `pipeline/`, `geovectors_encoder/`,
  `semantic_search/`, `igea/`, `worldkg_nca/`, `mcp_agent/`, `platform/`,
  plus `reference/` for lookup docs. Start at `docs/README.md`.
- `docs/research_to_runtime/`: why it was built this way: one doc per
  paper, how each became running code (flat, 13 docs).
- `docs/papers/`: the papers the pipeline implements (WorldKG, GeoVectors,
  IGEA, USLP, MapQA, Spatial-Agent, functional maps), grouped under
  `Maps/`, `WernerKuhn/`, `WorldKG/`.

Conventions and troubleshooting: `AGENTS.md`.

Working rules:
- Match the style and patterns of the file you are editing.
- Run the relevant test suites before declaring a task complete.
- Do not create git commits; leave staging and commits to the maintainer.

Task workflow (branch + ticket first):
- Every planned task, including trivial ones, starts with the maintainer
  creating two things before any code changes:
  1. An issue artifact: `plans/issues/ISSUE-<slug>.md`. Keep it short
     and readable: a `Milestone / Source / Status` header line, then a
     few free-form sections in plain prose - what is being done (or was
     done), why, measured results, caveats, and follow-ups. No mandated
     scaffold; fold anything critical into the body.
     `plans/issues/ISSUE-ann-gv-tags-flip.md` is the exemplar.
  2. A branch named `<issue-number>-<slug>` matching the artifact.
- If a session is asked to implement something and no issue artifact
  exists, ask the maintainer to write one (or draft the artifact for their
  review) instead of implementing directly. The cartographer agent
  (`.devin/agents/cartographer.md`) executes one artifact at a time and
  refuses to work off `main`/`dev`.
- The maintainer owns all mutating git operations (branch, commit, push,
  GitLab issue creation); agent output ends at printed commands.

Label contract (`glab label list`):
- `agent-ready`: applied automatically by the `post-commit` hook when a
  commit on an `<iid>-<slug>` branch adds the matching
  `plans/issues/ISSUE-<slug>.md`. `glab issue list -l
  agent-ready` is the runnable queue. Manual fallback:
  `glab issue update <iid> -l agent-ready`.
- `blocked`: the agent stopped on a blocker and reported it; its report
  includes the `glab issue update <iid> -l blocked` command for the
  maintainer to run.
- `tooling` / `docs`: housekeeping tags for workflow/infra and
  documentation work.
- Scoped `status::` / `type::` labels are optional and only added when a
  board or filter needs them.



