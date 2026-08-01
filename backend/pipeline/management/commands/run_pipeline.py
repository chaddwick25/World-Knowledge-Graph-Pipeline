"""
Management command to trigger the WorldKG Pipeline v2.

Usage:
    # Full pipeline (Steps 1–5)
    python manage.py run_pipeline MZ

    # Full pipeline + pre-processing (phases 1–3)
    python manage.py run_pipeline MC --preprocess

    # Full pipeline + pre-processing + GeoVectors pickle (phases 1–4)
    python manage.py run_pipeline MC --preprocess --preprocess-phases 1,2,3,4

    # Full pipeline with entropy gate bypass
    python manage.py run_pipeline GB --skip-entropy-gate

    # Single stage (for debugging / resume)
    python manage.py run_pipeline MZ --stage 3

    # With custom snapshot date
    python manage.py run_pipeline MZ --snapshot-date 2024_06_30
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Execute the WorldKG Pipeline v2 (Steps 1–5) via Celery Canvas'

    def add_arguments(self, parser):
        parser.add_argument(
            'iso',
            type=str,
            help='ISO 3166-1 alpha-2 country code (e.g., MZ, GB, CA)',
        )
        parser.add_argument(
            '--stage',
            type=int,
            default=None,
            choices=[1, 2, 3, 4, 5],
            help='Run a single stage instead of the full pipeline (for debugging)',
        )
        parser.add_argument(
            '--skip-entropy-gate',
            action='store_true',
            default=False,
            help='Bypass the entropy gate (force processing)',
        )
        parser.add_argument(
            '--snapshot-date',
            type=str,
            default=None,
            help='Override snapshot date (e.g., 2024_06_30)',
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            default=False,
            help='Wait for the pipeline to complete before returning',
        )
        parser.add_argument(
            '--preprocess',
            action='store_true',
            default=False,
            help='Run temporal pre-processing (phases 1–3) before the pipeline',
        )
        parser.add_argument(
            '--preprocess-phases',
            type=str,
            default='1,2,3',
            help='Which pre-processing phases to run (default: 1,2,3)',
        )

    def handle(self, *args, **options):
        iso = options['iso'].upper()
        stage = options.get('stage')
        skip_entropy = options.get('skip_entropy_gate', False)
        snapshot_date = options.get('snapshot_date')
        wait = options.get('wait', False)
        preprocess = options.get('preprocess', False)
        preprocess_phases_str = options.get('preprocess_phases', '1,2,3')

        # In --wait mode, use eager execution so no broker/worker is needed
        if wait:
            from pipeline.celery_app import celery_app
            celery_app.conf.update(
                task_always_eager=True,
                task_eager_propagates=True,
            )

        from pipeline.canvas import run_worldkg_pipeline, run_pipeline_stage
        from celery.result import AsyncResult

        # ── Optional: Temporal Pre-processing (Phases 1–3 / 1–4) ────────
        if preprocess:
            self.stdout.write(
                self.style.WARNING(
                f'\n{"="*60}\n'
                    f'  Pre-processing {iso} (phases {preprocess_phases_str})\n'
                    f'{"="*60}\n'
                )
            )
            from django.core.management import call_command
            call_command(
                'preprocess_country',
                iso,
                phases=preprocess_phases_str,
                wait=False,
        )

        if stage:
            # Single stage mode
            self.stdout.write(
                self.style.WARNING(
                    f'Running Stage {stage} for {iso}...'
            )
        )
            task_id = run_pipeline_stage(
                iso=iso,
                stage=stage,
                snapshot_date=snapshot_date,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f'Stage {stage} dispatched — task_id={task_id}'
                )
            )
            return

        # Full pipeline mode
        self.stdout.write(
            self.style.SUCCESS(
                f'\n{"="*60}\n'
                f'  WorldKG Pipeline v2 — {iso}\n'
                f'  Steps 1–5\n'
                f'{"="*60}\n'
            )
        )
        self.stdout.write(f'  Country:        {iso}')
        self.stdout.write(f'  Snapshot:       {snapshot_date or "default"}')
        self.stdout.write(f'  Skip entropy:   {skip_entropy}')
        self.stdout.write(f'  Wait for result: {wait}')
        self.stdout.write(f'{"="*60}\n')

        pipeline_run_id = run_worldkg_pipeline(
            iso=iso,
            snapshot_date=snapshot_date,
            skip_entropy_gate=skip_entropy,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f'Pipeline dispatched — pipeline_run_id={pipeline_run_id}\n'
                f'Track progress via:\n'
                f'  celery -A pipeline.celery_app status\n'
                f'  python manage.py pipeline_status {pipeline_run_id}\n'
            )
        )

        if wait:
            self.stdout.write('Waiting for pipeline to complete...')
            result = AsyncResult(pipeline_run_id)
            result.wait(timeout=14400)  # 4 hours max
            if result.successful():
                self.stdout.write(
                    self.style.SUCCESS('Pipeline completed successfully!')
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f'Pipeline failed: {result.result}')
                )

