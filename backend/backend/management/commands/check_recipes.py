"""Validate decision recipes and the references to them (backend/pipeline/recipes/).

Recipes are project memory: one recipe = one load-bearing decision or
invariant; plans cite recipes as [[DEC-001]] instead of re-narrating them.
This command is the citation checker: recipe front matter, id/filename
match, citation paths + line ranges, [[...]] references across
docs/** and AGENTS.md (plus any --plans-glob location), orphan
recipes, and bare .md mentions in the scanned plans that resolve
nowhere (the reference-rot detector; it found two dangling
companion-doc references on 2026-10-11).

Not the research orchestrator's report recipes (FOOD_REPORT in
research_service.py): those are named report plans, these are numbered
decision units.

No DB, no network. The core is pure filesystem logic, testable directly
(backend/tests/unit/test_check_recipes.py).
"""

import re
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

RECIPE_REF_RE = re.compile(r"\[\[([A-Z]+-\d+)\]\]")
RECIPE_FILE_RE = re.compile(r"^(?:DEC|INV)-\d+-")
MD_MENTION_RE = re.compile(r"[\w./-]+\.md\b")
REQUIRED_FIELDS = ("id", "title", "kind", "status", "date", "trigger", "source")
KNOWN_KINDS = ("decision", "invariant")
KNOWN_STATUSES = ("active", "draft", "superseded")


def parse_front_matter(text: str) -> Optional[dict]:
    """Return the front-matter dict of a recipe file, or None when absent.

    Tolerant `key: value` parsing; a `citations:` key collects the
    `  - ` list lines that follow it. No YAML dependency.
    """
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fields = {}
    current_key = None
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current_key:
            existing = fields.get(current_key)
            if isinstance(existing, list):
                existing.append(stripped[2:].strip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            current_key = key
            fields[key] = value if value else []
    return fields


def iter_content_lines(path: Path) -> Iterable[Tuple[int, str]]:
    """Yield (lineno, line) skipping fenced code blocks (``` toggles)."""
    fenced = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield lineno, line


def _check_citation(citation: str, root: Path) -> Optional[str]:
    """Validate one `path[:line]` citation against the repo root."""
    path_part, _, line_part = citation.partition(":")
    target = root / path_part
    if not target.exists():
        return "citation '%s' does not resolve" % citation
    if line_part:
        if not line_part.isdigit():
            return "citation '%s' has a non-numeric line" % citation
        line_count = len(target.read_text(encoding="utf-8").splitlines())
        if not 1 <= int(line_part) <= line_count:
            return "citation '%s' line out of range (file has %d lines)" % (
                citation, line_count)
    return None


def check_recipe(path: Path, root: Path) -> Tuple[Optional[str], List[str]]:
    """Return (recipe_id, errors) for one recipe file."""
    errors: List[str] = []
    fields = parse_front_matter(path.read_text(encoding="utf-8"))
    if fields is None:
        return None, ["%s: no front matter block" % path.name]
    name = path.name
    for field in REQUIRED_FIELDS:
        if not fields.get(field):
            errors.append("%s: missing field '%s'" % (name, field))
    recipe_id = fields.get("id")
    if recipe_id and not name.startswith(recipe_id + "-"):
        errors.append("%s: id '%s' does not match filename" % (name, recipe_id))
    if fields.get("kind") and fields["kind"] not in KNOWN_KINDS:
        errors.append("%s: unknown kind '%s'" % (name, fields["kind"]))
    if fields.get("status") and fields["status"] not in KNOWN_STATUSES:
        errors.append("%s: unknown status '%s'" % (name, fields["status"]))
    for citation in fields.get("citations") or []:
        problem = _check_citation(citation, root)
        if problem:
            errors.append("%s: %s" % (name, problem))
    return recipe_id, errors


def scan_recipe_refs(md_files: List[Path], recipe_ids: Set[str]) -> List[Tuple[Path, int, str]]:
    """Return (path, lineno, ref) for [[...]] refs resolving to no recipe."""
    unresolved = []
    for path in md_files:
        for lineno, line in iter_content_lines(path):
            for match in RECIPE_REF_RE.finditer(line):
                if match.group(1) not in recipe_ids:
                    unresolved.append((path, lineno, match.group(1)))
    return unresolved


def _mention_resolves(mention: str, referring_file: Path, root: Path) -> bool:
    candidates = [root / mention, referring_file.parent / mention]
    return any(candidate.exists() for candidate in candidates)


def scan_md_mentions(md_files: List[Path], root: Path) -> List[Tuple[Path, int, str]]:
    """Return (path, lineno, mention) for bare .md mentions resolving nowhere."""
    unresolved = []
    for path in md_files:
        for lineno, line in iter_content_lines(path):
            for match in MD_MENTION_RE.finditer(line):
                mention = match.group(0)
                if not _mention_resolves(mention, path, root):
                    unresolved.append((path, lineno, mention))
    return unresolved


def run_check(
    root,
    recipes_dir: str = "backend/pipeline/recipes",
    plans_glob: str = "docs/",
    ref_globs: Tuple[str, ...] = ("docs/**/*.md", "AGENTS.md"),
    strict: bool = False,
) -> Tuple[List[str], List[str], dict]:
    """Run the full check. Returns (errors, warnings, stats)."""
    root = Path(root)
    recipes_path = root / recipes_dir
    errors: List[str] = []
    warnings: List[str] = []

    md_in_dir = sorted(recipes_path.glob("*.md")) if recipes_path.is_dir() else []
    recipes = [p for p in md_in_dir if RECIPE_FILE_RE.match(p.name)]
    recipe_docs = [p for p in md_in_dir if not RECIPE_FILE_RE.match(p.name)]
    if not recipes:
        errors.append("no recipes found in %s" % recipes_dir)
    recipe_ids: Set[str] = set()
    for recipe in recipes:
        recipe_id, recipe_errors = check_recipe(recipe, root)
        if recipe_id:
            recipe_ids.add(recipe_id)
        errors.extend(recipe_errors)

    plan_files = [
        p for p in root.glob(plans_glob)
        if p.is_file() and recipes_path not in p.parents
    ]
    ref_files = set(plan_files) | set(recipe_docs)
    for pattern in ref_globs:
        for p in root.glob(pattern):
            if p.is_file() and recipes_path not in p.parents:
                ref_files.add(p)
    ref_files = sorted(ref_files)

    scanned = ref_files + recipes
    for path, lineno, ref in scan_recipe_refs(scanned, recipe_ids):
        errors.append("%s:%d: recipe reference [[%s]] resolves to no recipe" % (
            path.relative_to(root), lineno, ref))

    cited: Set[str] = set()
    for path in scanned:
        for match in RECIPE_REF_RE.finditer(path.read_text(encoding="utf-8")):
            if match.group(1) in recipe_ids:
                cited.add(match.group(1))
    for recipe_id in sorted(recipe_ids - cited):
        warnings.append("recipe %s is never cited (orphan)" % recipe_id)

    for path, lineno, mention in scan_md_mentions(plan_files, root):
        warnings.append("%s:%d: .md mention '%s' resolves nowhere" % (
            path.relative_to(root), lineno, mention))

    if strict:
        errors.extend(warnings)
        warnings = []
    stats = {"recipes": len(recipes), "plans": len(plan_files)}
    return errors, warnings, stats


class Command(BaseCommand):
    help = (
        "Validate decision recipes (backend/pipeline/recipes/) and the "
        "[[...]] references to them across docs/**, AGENTS.md (plus any "
        "--plans-glob location)"
    )

    def add_arguments(self, parser):
        parser.add_argument("--recipes-dir", default="backend/pipeline/recipes")
        parser.add_argument(
            "--plans-glob", default="docs"
        )
        parser.add_argument(
            "--strict", action="store_true",
            help="Warnings fail the check too",
        )

    def handle(self, *args, **options):
        root = Path(settings.PROJECT_ROOT)
        errors, warnings, stats = run_check(
            root,
            recipes_dir=options["recipes_dir"],
            plans_glob=options["plans_glob"],
            strict=options["strict"],
        )
        for warning in warnings:
            self.stdout.write(self.style.WARNING("WARN   " + warning))
        for error in errors:
            self.stdout.write(self.style.ERROR("ERROR  " + error))
        if errors:
            raise CommandError(
                "check_recipes: %d error(s), %d recipe(s), %d plan file(s)"
                % (len(errors), stats["recipes"], stats["plans"])
            )
        self.stdout.write(self.style.SUCCESS(
            "check_recipes: OK (%d recipe(s), %d plan file(s), %d warning(s))"
            % (stats["recipes"], stats["plans"], len(warnings))
        ))
