import json

from django.core.management.base import BaseCommand

from geodata.models import DataSource


class Command(BaseCommand):
    help = "Register (or update) a geodata data source."

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True, help='Source name')
        parser.add_argument(
            '--adapter',
            required=True,
            choices=['ckan', 'socrata', 'wfs', 'direct', 'rest'],
            help='Adapter type',
        )
        parser.add_argument('--base-url', required=True, help='Source base URL')
        parser.add_argument('--country-code', default='', help='ISO country code (e.g. CA)')
        parser.add_argument('--api-key', default='', help='Optional API key')
        parser.add_argument(
            '--config',
            default='{}',
            help='Adapter-specific config as JSON (e.g. \'{"target_datasets": ["x"]}\')',
        )

    def handle(self, *args, **options):
        try:
            config = json.loads(options['config'])
        except json.JSONDecodeError as e:
            self.stderr.write(self.style.ERROR(f"Invalid --config JSON: {e}"))
            return

        if not isinstance(config, dict):
            self.stderr.write(self.style.ERROR("--config must be a JSON object"))
            return

        source, created = DataSource.objects.update_or_create(
            name=options['name'],
            defaults={
                'adapter_type': options['adapter'],
                'base_url': options['base_url'],
                'country_code': options['country_code'],
                'api_key': options['api_key'] or None,
                'config': config,
            },
        )
        action = "Registered" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{action} source: {source.name} ({source.adapter_type}, {source.base_url})"
        ))
