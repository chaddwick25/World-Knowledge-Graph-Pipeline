import logging
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from worldkg_nca.services.enrichment_service import get_worldkg_enrichment_service
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
from worldkg_nca.services.triples_service import WorldKGTriplesService
from worldkg_nca.services.ontology_loader import WorldKGOntologyLoader

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Enrich OSM entities with WorldKG class assertions'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--region',
            type=str,
            help='Filter entities by region name'
        )
        parser.add_argument(
            '--snapshot-id',
            type=str,
            help='Filter entities by source snapshot UUID'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Number of entities to process per batch (default: 1000)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Limit total number of entities to process (useful for testing)'
        )
        parser.add_argument(
            '--use-sparql',
            action='store_true',
            help='Use WorldKG SPARQL endpoint (slower but more accurate)'
        )
        parser.add_argument(
            '--skip-enriched',
            action='store_true',
            default=True,
            help='Skip entities already enriched (default: True)'
        )
        parser.add_argument(
            '--load-ontology',
            type=str,
            help='Path to WorldKG ontology JSON file to load into Redis'
        )
        parser.add_argument(
            '--clear-cache',
            action='store_true',
            help='Clear WorldKG ontology cache before loading'
        )
        parser.add_argument(
            '--load-from-ttl',
            type=str,
            metavar='TTL_PATH',
            help='Load WorldKG ontology from a .ttl RDF file (e.g., Zenodo download) '
                 'instead of a JSON file. Parses rdfs:subClassOf and owl:equivalentClass.'
        )
        parser.add_argument(
            '--use-create-triples',
            action='store_true',
            help='Use the official WorldKG CreateTriples.py pipeline (requires --pbf-path '
                 'and --worldkg-dir). Produces .ttl triples, then enriches OsmEntity records.'
        )
        parser.add_argument(
            '--pbf-path',
            type=str,
            metavar='PBF_PATH',
            help='Path to .osm.pbf input file for --use-create-triples mode'
        )
        parser.add_argument(
            '--worldkg-dir',
            type=str,
            metavar='DIR',
            help='Path to cloned WorldKG-Knowledge-Graph repository '
                 '(https://github.com/alishiba14/WorldKG-Knowledge-Graph)'
        )
        parser.add_argument(
            '--triples-output',
            type=str,
            default='/tmp/worldkg_triples_output.ttl',
            metavar='TTL_OUTPUT',
            help='Output path for CreateTriples.py TTL file (default: /tmp/worldkg_triples_output.ttl)'
        )
    
    def handle(self, *args, **options):
        ontology_service = get_worldkg_ontology_service()
        enrichment_service = get_worldkg_enrichment_service()

        # Handle cache clearing first (can combine with other flags)
        if options['clear_cache']:
            self.stdout.write("Clearing WorldKG ontology cache...")
            ontology_service.clear_cache()
            self.stdout.write(self.style.SUCCESS("Cache cleared"))
            if not any([options['load_ontology'], options.get('load_from_ttl'),
                        options.get('use_create_triples')]):
                return

        # Handle ontology loading from JSON
        if options['load_ontology']:
            self._load_ontology(ontology_service, options)
            return

        # Handle ontology loading from TTL (Zenodo/official RDF dump)
        if options.get('load_from_ttl'):
            self._load_ontology_from_ttl(ontology_service, options)
            return

        # Handle official CreateTriples.py pipeline
        if options.get('use_create_triples'):
            self._run_create_triples_pipeline(ontology_service, options)
            return

        # Validate ontology is loaded
        all_classes = ontology_service.get_all_classes()
        if not all_classes:
            raise CommandError(
                "WorldKG ontology not loaded in Redis. "
                "Use --load-ontology (JSON), --load-from-ttl (TTL), or "
                "--use-create-triples (PBF pipeline) to load the ontology first."
            )

        self.stdout.write(f"WorldKG ontology loaded: {len(all_classes)} classes")

        # Run batch enrichment
        self.stdout.write("Starting batch enrichment...")
        self.stdout.write(f"  Region: {options['region'] or 'all'}")
        self.stdout.write(f"  Snapshot ID: {options['snapshot_id'] or 'all'}")
        self.stdout.write(f"  Batch size: {options['batch_size']}")
        self.stdout.write(f"  Use SPARQL: {options['use_sparql']}")
        self.stdout.write(f"  Skip enriched: {options['skip_enriched']}")
        self.stdout.write(f"  Limit: {options['limit'] or 'none'}")

        stats = enrichment_service.batch_enrich_region(
            region=options['region'],
            snapshot_id=options['snapshot_id'],
            batch_size=options['batch_size'],
            use_sparql=options['use_sparql'],
            skip_enriched=options['skip_enriched'],
            limit=options['limit']
        )

        # Display results
        self.stdout.write(self.style.SUCCESS("\nEnrichment complete!"))
        self.stdout.write(f"  Enriched:          {stats['enriched']}")
        self.stdout.write(f"  Failed:            {stats['failed']}")
        self.stdout.write(f"  Skipped:           {stats['skipped']}")
        self.stdout.write(f"  SPARQL queries:    {stats['sparql_queries']}")
        self.stdout.write(f"  Local predictions: {stats['local_predictions']}")

        if stats['failed'] > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{stats['failed']} entities could not be enriched. "
                    "They may have tags not covered by the loaded ontology."
                )
            )
    
    def _load_ontology(self, ontology_service, options):
        """Load WorldKG ontology from JSON file into Redis."""
        import json

        ontology_path = options['load_ontology']
        self.stdout.write(f"Loading ontology from JSON: {ontology_path}...")

        try:
            with open(ontology_path, 'r') as f:
                ontology_dict = json.load(f)

            # Strip comment key if present
            ontology_dict.pop('_comment', None)

            ontology_service.load_ontology_from_dict(ontology_dict)
            self._print_ontology_summary(ontology_service)

        except FileNotFoundError:
            raise CommandError(f"Ontology file not found: {ontology_path}")
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON in ontology file: {e}")
        except Exception as e:
            raise CommandError(f"Error loading ontology: {e}")

    def _load_ontology_from_ttl(self, ontology_service, options):
        """Load WorldKG ontology from an RDF Turtle file."""
        ttl_path = options['load_from_ttl']
        self.stdout.write(f"Loading ontology from TTL: {ttl_path}...")
        self.stdout.write(
            "  Parsing rdfs:subClassOf hierarchy and owl:equivalentClass NCA links..."
        )

        try:
            loader = WorldKGOntologyLoader()
            ontology_dict = loader.load_from_ttl(ttl_path)
            ontology_service.load_ontology_from_dict(ontology_dict)
            self._print_ontology_summary(ontology_service)

            # Optionally save as JSON for faster future loads
            json_cache = ttl_path.replace('.ttl', '_cache.json')
            loader.save_to_json(ontology_dict, json_cache)
            self.stdout.write(f"  Cached as JSON: {json_cache}")

        except Exception as e:
            raise CommandError(f"Error loading TTL ontology: {e}")

    def _run_create_triples_pipeline(self, ontology_service, options):
        """Run the official WorldKG CreateTriples.py PBF pipeline."""
        pbf_path = options.get('pbf_path')
        worldkg_dir = options.get('worldkg_dir')
        output_ttl = options.get('triples_output', '/tmp/worldkg_triples_output.ttl')

        if not pbf_path:
            raise CommandError(
                "--pbf-path is required with --use-create-triples. "
                "Provide the path to a .osm.pbf file."
            )
        if not worldkg_dir:
            raise CommandError(
                "--worldkg-dir is required with --use-create-triples. "
                "Clone: git clone https://github.com/alishiba14/WorldKG-Knowledge-Graph"
            )

        all_classes = ontology_service.get_all_classes()
        if not all_classes:
            raise CommandError(
                "WorldKG ontology not loaded. Load it first with --load-ontology or "
                "--load-from-ttl before running --use-create-triples."
            )

        triples_service = WorldKGTriplesService(worldkg_dir=worldkg_dir)

        self.stdout.write(f"Running CreateTriples.py on {pbf_path}...")
        self.stdout.write(f"  WorldKG repo: {worldkg_dir}")
        self.stdout.write(f"  Output TTL:   {output_ttl}")

        ok = triples_service.run_create_triples(pbf_path, output_ttl)
        if not ok:
            raise CommandError(
                "CreateTriples.py failed. Check logs for details. "
                "Ensure osmium Python library is installed in the WorldKG venv."
            )

        self.stdout.write(
            self.style.SUCCESS(f"CreateTriples.py succeeded. Parsing {output_ttl}...")
        )

        enrichments = triples_service.parse_ttl_to_enrichment(
            output_ttl, ontology_service=ontology_service
        )
        self.stdout.write(f"  Parsed {len(enrichments)} entity enrichments")

        self.stdout.write("Applying enrichments to database...")
        stats = triples_service.bulk_apply_enrichment(
            enrichments, batch_size=options['batch_size']
        )

        self.stdout.write(self.style.SUCCESS("\nCreateTriples pipeline complete!"))
        self.stdout.write(f"  Updated:   {stats['updated']}")
        self.stdout.write(f"  Not found: {stats['not_found']}")

    def _print_ontology_summary(self, ontology_service):
        """Print loaded ontology summary to stdout."""
        all_classes = ontology_service.get_all_classes()
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully loaded {len(all_classes)} WorldKG classes into Redis"
            )
        )
        sample_classes = sorted(list(all_classes))[:8]
        self.stdout.write("\nSample classes:")
        for cls in sample_classes:
            depth = ontology_service.get_depth(cls)
            osm_key = ontology_service.get_canonical_osm_key(cls) or ''
            osm_val = ontology_service.get_canonical_osm_value(cls) or ''
            tag_str = f"{osm_key}={osm_val}" if osm_val else (osm_key or 'root')
            self.stdout.write(f"  {cls} (depth={depth}, tag={tag_str})")
