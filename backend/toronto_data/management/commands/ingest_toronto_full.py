from django.core.management.base import BaseCommand
from django.core.management import call_command


class Command(BaseCommand):
    help = "Full Toronto Open Data ingestion: metadata + quality scores + data download"

    def add_arguments(self, parser):
        parser.add_argument(
            '--datasets',
            nargs='+',
            help='Specific dataset IDs to ingest (default: all TARGET_DATASETS)',
        )
        parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip data download step (metadata only)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-download even if not modified',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(
            "\n" + "="*60 +
            "\nToronto Open Data Full Ingestion Pipeline" +
            "\n" + "="*60
        ))
        
        # Step 1: Ingest metadata
        self.stdout.write("\n" + "─"*60)
        self.stdout.write("STEP 1: Ingesting metadata and quality scores")
        self.stdout.write("─"*60)
        
        metadata_args = []
        if options.get('datasets'):
            metadata_args.extend(['--datasets'] + options['datasets'])
        
        call_command('ingest_toronto_metadata', *metadata_args)
        
        # Step 2: Download data (if not skipped)
        if not options.get('skip_download'):
            self.stdout.write("\n" + "─"*60)
            self.stdout.write("STEP 2: Downloading and parsing data files")
            self.stdout.write("─"*60)
            
            download_args = []
            if options.get('force'):
                download_args.append('--force')
            
            # If specific datasets requested, download each one
            if options.get('datasets'):
                for dataset_id in options['datasets']:
                    call_command('download_toronto_data', '--dataset', dataset_id, *download_args)
            else:
                call_command('download_toronto_data', *download_args)
        
        # Final summary
        self.stdout.write("\n" + "="*60)
        self.stdout.write(self.style.SUCCESS("✓ Full ingestion pipeline complete!"))
        self.stdout.write("="*60)
