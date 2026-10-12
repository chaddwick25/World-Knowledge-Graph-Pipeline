***REMOVED*** Recipes

Project memory for load-bearing decisions and invariants, shelved beside
the pipeline's own configuration (`hyperparams.yaml`): one recipe = one
reusable decision unit. Plans cite recipes instead of re-narrating them,
and the checker validates every citation. Same pattern as the factor
tables: computed once, read many times, dedupe-by-construction.

Not the research orchestrator's report recipes (`FOOD_REPORT`): those are
named report plans; these are numbered decision units.

***REMOVED******REMOVED*** Recipe format

One markdown file per recipe, named `<ID>-<slug>.md`, front matter first:

```
---
id: DEC-001
title: one-line title
kind: decision | invariant
status: active | draft | superseded
date: YYYY-MM-DD
trigger: the measured trigger, or "none (design decision)"
source: where it came from (session, paper section, incident)
citations:
  - repo/relative/path.py:123
---

The invariant in one sentence, then the consequence. 2-8 lines.
```

Fields:

| Field | Rule |
|---|---|
| `id` | `DEC-nnn` (decision) or `INV-nnn` (invariant); must match the filename prefix |
| `kind` | `decision` or `invariant` |
| `status` | `active` (in force), `draft` (decided, trigger pending), `superseded` (kept for history; the body names the successor) |
| `date` | date the recipe was written or last re-confirmed |
| `trigger` | the measured condition that gates action; `none (design decision)` when standing |
| `citations` | repo-root-relative paths, optional `:line`; the line must exist. Repo files only; external documents are named in the body |

***REMOVED******REMOVED*** Citing a recipe

Plans and docs cite recipes inline as `[[DEC-001]]` / `[[INV-001]]`. The
checker resolves every reference; a reference to a missing recipe is an
error.

***REMOVED******REMOVED*** Checker

```
python manage.py check_recipes            ***REMOVED*** errors fail, warnings pass
python manage.py check_recipes --strict   ***REMOVED*** warnings fail too
```

Checks: recipe front matter, id/filename match, citation paths and line
ranges, `[[...]]` references across `deploy/plans/**`, `docs/**`, and
`AGENTS.md`, orphan recipes (never cited), and bare `.md` mentions in
`deploy/plans/**` that resolve nowhere (the reference-rot detector; it
found the two dangling companion-doc references in
`AGENT_ORCHESTRATION_TIERED_PLAN.md` on 2026-10-11).

Only files named `DEC-nnn-*` or `INV-nnn-*` are parsed as recipes; other
markdown in this directory (this README) is documentation.

Command: `backend/backend/management/commands/check_recipes.py`.
Tests: `backend/tests/unit/test_check_recipes.py`.

***REMOVED******REMOVED*** Scope

Recipes carry the load-bearing units only. The design prose stays in the
plans; a plan that is all recipes is a checklist, not a design. Write
recipes opportunistically when a plan is touched, not as a bulk migration.
