---
name: write-issue
description: Draft a plans/issues/ISSUE-<slug>.md artifact from a roadmap row, then print the issue-create command for the maintainer
argument-hint: "<slug> [milestone]"
allowed-tools:
  - read
  - grep
  - glob
  - write
triggers:
  - user
---

Draft one issue artifact for the milestone workflow.

## Inputs

`<slug>` is the issue short name (kebab-case). `[milestone]` is one of
`beta.1`, `beta.2`, `beta.3`, `retrospective`; if omitted, infer it from
the issue seeds table in `plans/BETA_ROADMAP_PLAN.md` §5.

## Steps

1. Read `plans/BETA_ROADMAP_PLAN.md` §5 to find the matching seed
   row and its milestone. Read the milestone section in §2 for context.
2. If the seed references an existing plan doc (`plans/*.md`,
   `plans/next-stage/*.md`) or a Schematics doc, read it and pull
   the concrete file touchpoints.
3. Write `plans/issues/ISSUE-<slug>.md` in this shape:

```markdown
# ISSUE-<slug>: <title>

> **Milestone:** <milestone> | **Source:** BETA_ROADMAP_PLAN.md row <N>
> **Intent:** one line, what changes and why

## Scope
- <what this issue does>

## Out of scope
- <adjacent work explicitly not done here>

## Touchpoints
- `path/to/file.py` - <what changes, cite Schematics file:line if known>

## Test plan
- <which suites, which new tests>

## Handoff
Branch: `<issue-number>-<slug>` (fill in after issue creation)
Commit trailer: `Changelog: <added|fixed|changed>` (or none)
```

4. Print the issue-create command for the maintainer to run:

```
glab issue create --title "<title>" \
  --description "$(cat plans/issues/ISSUE-<slug>.md)" \
  --label "type::feature" --milestone "<milestone>"
```

No `glab` installed? Print the `curl` equivalent against
`https://gitlab.com/api/v4/projects/84407733/issues` instead.

## Rules

- One artifact per invocation. Do not batch.
- Voice spec: no em-dashes, no ASCII box art.
- Never run the issue-create command. You have no exec access; the
  maintainer runs it and fills in the issue number on the branch line.
