"""Celery task: Step 5.8 — Generate subdivision sample questions.

Generates subdivision-scoped (and cross-subdivision) sample questions for
every subdivision of the country that has a populated bbox, using the
snapshot the pipeline just built. Each question is verified
(execute-and-keep): only questions the real parser+executor returns
results for are stored — the "every question has results" criterion.

This step is **non-fatal**: generation failures are logged and the
pipeline continues to Step 6. Sample questions are a search-layer aid;
the API falls back to the country's curated bank when a subdivision has
none. Deterministic + idempotent (regeneration replaces prior generated
rows).
"""

import logging

from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.tasks.helper import _log
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger(__name__)


@pipeline_task(
    bind=True, base=PipelineTask, name="step_5_8_generate_sample_questions",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("generate_sample_questions", CountryEnvelope, 5.8)
def step_5_8_generate_sample_questions(
    self, env: CountryEnvelope,
) -> CountryEnvelope:
    """Step 5.8: generate + verify subdivision sample questions.

    Pipeline:
      5.8.1  For each subdivision with a populated bbox, generate
             geometry-scoped questions (SampleQuestionService.
             generate_subdivision, verify=True).
      5.8.2  Generate country-wide cross-subdivision questions
             (generate_cross_subdivision, verify=True).

    Non-fatal: failures are logged and the pipeline continues.
    """
    from django.db.models import Q as _Q

    from core.models import SubgraphProfile
    from semantic_search.services.sample_question_service import (
        SampleQuestionService,
    )

    subs = list(SubgraphProfile.objects.filter(
        country_profile__iso2__iexact=env.iso,
        wikidata_id__isnull=False,
    ).filter(
        _Q(bbox_min_lon__isnull=False) & _Q(bbox_max_lat__isnull=False),
    ).order_by("name"))
    _log(
        logger, "info",
        f"Step 5.8: {len(subs)} subdivisions with bboxes",
        country=env.iso, pipeline_run_id=env.pipeline_run_id,
    )

    generated = skipped = dropped = errors = 0
    for sg in subs:
        try:
            result = SampleQuestionService.generate_subdivision(
                env.iso, sg.wikidata_id, snapshot_date=env.snapshot_date,
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal step
            _log(
                logger, "warning",
                f"Step 5.8: generation failed for {sg.name}",
                country=env.iso, error=str(exc),
                pipeline_run_id=env.pipeline_run_id,
            )
            errors += 1
            continue
        if result.get("error"):
            errors += 1
            continue
        if result.get("skipped"):
            skipped += 1
            continue
        generated += result.get("generated", 0)
        dropped += result.get("dropped", 0)

    try:
        cross = SampleQuestionService.generate_cross_subdivision(
            env.iso, snapshot_date=env.snapshot_date,
        )
        cross_generated = cross.get("generated", 0)
        cross_dropped = cross.get("dropped", 0)
    except Exception as exc:  # noqa: BLE001 — non-fatal step
        _log(
            logger, "warning",
            "Step 5.8: cross-subdivision generation failed",
            country=env.iso, error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )
        cross_generated = cross_dropped = 0

    _log(
        logger, "info",
        f"Step 5.8: generated {generated} subdivision + {cross_generated} "
        f"cross-subdivision questions (dropped {dropped + cross_dropped} "
        f"unverified, skipped {skipped} sparse, {errors} errors)",
        country=env.iso, pipeline_run_id=env.pipeline_run_id,
    )
    return env
