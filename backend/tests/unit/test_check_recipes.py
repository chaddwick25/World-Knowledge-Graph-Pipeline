"""
Tests for the decision-recipe checker (check_recipes).

The recipes (backend/pipeline/recipes/) are project memory: plans cite
them as [[DEC-001]] and the checker validates front matter, citations,
and references. These tests cover the pure core on tmp_path fixtures
plus one regression pass over the real recipes directory, so a broken
recipe or a dangling reference fails here. No DB / network required.
"""

from pathlib import Path

import pytest

from backend.management.commands.check_recipes import (
    check_recipe,
    parse_front_matter,
    run_check,
    scan_recipe_refs,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID_RECIPE = """---
id: DEC-100
title: test recipe
kind: decision
status: active
date: 2026-10-11
trigger: none (test)
source: test
citations:
  - plans/PLAN.md:2
---

Body line.
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture_root(tmp_path, recipe_text=VALID_RECIPE, plan_text="***REMOVED*** Plan\n[[DEC-100]]\n"):
    root = tmp_path / "repo"
    _write(root / "recipes" / "DEC-100-test.md", recipe_text)
    _write(root / "plans" / "PLAN.md", plan_text)
    return root


def test_parse_front_matter_extracts_fields_and_citations():
    fields = parse_front_matter(VALID_RECIPE)
    assert fields["id"] == "DEC-100"
    assert fields["kind"] == "decision"
    assert fields["citations"] == ["plans/PLAN.md:2"]
    assert fields["trigger"] == "none (test)"


def test_parse_front_matter_returns_none_without_block():
    assert parse_front_matter("***REMOVED*** just prose\n") is None


def test_recipe_missing_field_is_an_error(tmp_path):
    root = tmp_path
    recipe = _write(root / "DEC-100-test.md", VALID_RECIPE.replace("title: test recipe\n", ""))
    _, errors = check_recipe(recipe, root)
    assert any("missing field 'title'" in e for e in errors)


def test_recipe_id_filename_mismatch_is_an_error(tmp_path):
    root = tmp_path
    recipe = _write(root / "DEC-200-test.md", VALID_RECIPE)
    _, errors = check_recipe(recipe, root)
    assert any("does not match filename" in e for e in errors)


def test_recipe_citation_line_out_of_range(tmp_path):
    root = tmp_path
    _write(root / "plans" / "PLAN.md", "***REMOVED*** one line\n")
    recipe = _write(
        root / "DEC-100-test.md",
        VALID_RECIPE.replace("plans/PLAN.md:2", "plans/PLAN.md:99"),
    )
    _, errors = check_recipe(recipe, root)
    assert any("line out of range" in e for e in errors)


def test_recipe_citation_missing_path(tmp_path):
    root = tmp_path
    recipe = _write(
        root / "DEC-100-test.md",
        VALID_RECIPE.replace("plans/PLAN.md:2", "plans/MISSING.md"),
    )
    _, errors = check_recipe(recipe, root)
    assert any("does not resolve" in e for e in errors)


def test_unresolved_recipe_ref_is_an_error(tmp_path):
    root = _fixture_root(tmp_path, plan_text="***REMOVED*** Plan\n[[DEC-999]]\n")
    errors, _, _ = run_check(root, recipes_dir="recipes", plans_glob="plans/*.md")
    assert any("[[DEC-999]]" in e for e in errors)


def test_orphan_recipe_is_a_warning_not_an_error(tmp_path):
    root = _fixture_root(
        tmp_path,
        recipe_text=VALID_RECIPE.replace("citations:\n  - plans/PLAN.md:2\n", "citations:\n"),
        plan_text="***REMOVED*** Plan, no citations\n",
    )
    errors, warnings, _ = run_check(root, recipes_dir="recipes", plans_glob="plans/*.md")
    assert errors == []
    assert any("DEC-100 is never cited" in w for w in warnings)


def test_bare_md_mention_warns_and_strict_fails(tmp_path):
    root = _fixture_root(tmp_path, plan_text="***REMOVED*** Plan\nSee MISSING.md.\n")
    errors, warnings, _ = run_check(root, recipes_dir="recipes", plans_glob="plans/*.md")
    assert errors == []
    assert any("MISSING.md" in w for w in warnings)
    strict_errors, _, _ = run_check(
        root, recipes_dir="recipes", plans_glob="plans/*.md", strict=True
    )
    assert any("MISSING.md" in e for e in strict_errors)


def test_non_recipe_md_in_recipes_dir_is_not_parsed_as_recipe(tmp_path):
    root = _fixture_root(tmp_path)
    _write(root / "recipes" / "README.md", "***REMOVED*** Recipes docs, no front matter\n")
    errors, _, _ = run_check(root, recipes_dir="recipes", plans_glob="plans/*.md")
    assert not any("README.md" in e for e in errors)


def test_fenced_code_blocks_are_skipped(tmp_path):
    root = _fixture_root(tmp_path, plan_text="***REMOVED*** Plan\n```\n[[DEC-999]]\n```\n")
    plan = root / "plans" / "PLAN.md"
    assert scan_recipe_refs([plan], {"DEC-100"}) == []


def test_real_recipes_and_plan_references_pass():
    ***REMOVED*** Host-checkout regression: the checker's paths are repo-root-relative
    ***REMOVED*** (deploy/, AGENTS.md); the backend container mounts only backend/ at
    ***REMOVED*** /app, so the plan-reference scan has nothing to see there.
    if not (REPO_ROOT / "deploy" / "plans").is_dir():
        pytest.skip("host repo layout (deploy/) not present")
    errors, _, stats = run_check(REPO_ROOT)
    assert errors == []
    assert stats["recipes"] >= 6
