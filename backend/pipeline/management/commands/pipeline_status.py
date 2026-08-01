"""
Management command to check pipeline status.

Usage:
    python manage.py pipeline_status <pipeline_run_id>
    python manage.py pipeline_status --country MZ
    python manage.py pipeline_status --list
"""

import json
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Check WorldKG Pipeline v2 status'

    def add_arguments(self, parser):
        parser.add_argument(
            'pipeline_run_id',
            nargs='?',
            type=str,
            default=None,
            help='UUID of the PipelineRun to check',
        )
        parser.add_argument(
            '--country',
            type=str,
            default=None,
            help='Show latest run for this ISO code',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            default=False,
            help='List all recent pipeline runs',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            default=False,
            help='Output as JSON',
        )

    def handle(self, *args, **options):
        from orchestration.models import PipelineRun

        run_id = options.get('pipeline_run_id')
        country = options.get('country')
        as_json = options.get('json', False)
        list_all = options.get('list', False)

        if list_all:
            runs = PipelineRun.objects.order_by('-created_at')[:20]
            if as_json:
                data = []
                for run in runs:
                    data.append({
                        'id': str(run.id),
                        'country_code': run.country_code,
                        'country_name': run.country_name,
                        'pipeline_type': run.pipeline_type,
                        'status': run.status,
                        'current_stage': run.current_stage,
                        'completed_stages': run.completed_stages,
                        'created_at': run.created_at.isoformat() if run.created_at else None,
                        'completed_at': run.completed_at.isoformat() if run.completed_at else None,
                        'error_message': run.error_message,
                    })
                self.stdout.write(json.dumps(data, indent=2))
                return

            self.stdout.write(
                self.style.SUCCESS('\nRecent Pipeline Runs:\n')
            )
            self.stdout.write(f'{"ID":<40} {"Country":<15} {"Status":<12} {"Stage":<20} {"Created":<25}')
            self.stdout.write('-' * 112)
            for run in runs:
                self.stdout.write(
                    f'{str(run.id):<40} '
                    f'{run.country_code:<15} '
                    f'{run.status:<12} '
                    f'{run.current_stage or "—":<20} '
                    f'{run.created_at.strftime("%Y-%m-%d %H:%M"):<25}'
                )
            return

        if country:
            run = (
                PipelineRun.objects
                .filter(country_code=country.upper())
                .order_by('-created_at')
                .first()
            )
            if not run:
                self.stdout.write(
                    self.style.WARNING(f'No runs found for {country}')
                )
                return
            run_id = str(run.id)

        if not run_id:
            self.stdout.write(
                self.style.ERROR('Provide a pipeline_run_id or --country or --list')
            )
            return

        try:
            from orchestration.models import PipelineRun
            run = PipelineRun.objects.get(id=run_id)
        except PipelineRun.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f'PipelineRun {run_id} not found')
            )
            return

        if as_json:
            data = {
                'id': str(run.id),
                'country_code': run.country_code,
                'country_name': run.country_name,
                'pipeline_type': run.pipeline_type,
                'status': run.status,
                'current_stage': run.current_stage,
                'completed_stages': run.completed_stages,
                'stage_metrics': run.stage_metrics,
                'total_entities_processed': run.total_entities_processed,
                'total_entities_aligned': run.total_entities_aligned,
                'total_spatial_links': run.total_spatial_links,
                'created_at': run.created_at.isoformat() if run.created_at else None,
                'started_at': run.started_at.isoformat() if run.started_at else None,
                'completed_at': run.completed_at.isoformat() if run.completed_at else None,
                'error_message': run.error_message,
            }
            self.stdout.write(json.dumps(data, indent=2))
            return

        # Pretty-print
        self.stdout.write('')
        self.stdout.write('=' * 60)
        self.stdout.write(f'  Pipeline Run Status')
        self.stdout.write('=' * 60)
        self.stdout.write(f'  ID:             {run.id}')
        self.stdout.write(f'  Country:        {run.country_name} ({run.country_code})')
        self.stdout.write(f'  Type:           {run.pipeline_type}')
        self.stdout.write(f'  Status:         {self._style_status(run.status, run.status)}')
        self.stdout.write(f'  Current stage:  {run.current_stage or "—"}')
        self.stdout.write(f'  Stages done:    {", ".join(run.completed_stages) or "—"}')
        self.stdout.write(f'  Created:        {run.created_at}')
        self.stdout.write(f'  Started:        {run.started_at or "—"}')
        self.stdout.write(f'  Completed:      {run.completed_at or "—"}')

        if run.error_message:
            self.stdout.write(self.style.ERROR(f'  Error:          {run.error_message}'))

        if run.stage_metrics:
            self.stdout.write('')
            self.stdout.write('  Stage Metrics:')
            for stage, metrics in run.stage_metrics.items():
                self.stdout.write(f'    {stage}: {json.dumps(metrics, default=str)}')

        self.stdout.write('=' * 60)
        self.stdout.write('')

    def _style_status(self, status, text):
        if status == 'COMPLETED':
            return self.style.SUCCESS(text)
        elif status == 'FAILED':
            return self.style.ERROR(text)
        elif status == 'RUNNING':
            return self.style.WARNING(text)
        return text
