from django.core.management.base import BaseCommand, CommandError

from worldkg_nca.services.projection_weight_service import ProjectionWeightService


class Command(BaseCommand):
    help = "Learn country-specific projection weights for tri-space scores from SpatialTripletScore tables."

    def add_arguments(self, parser):
        parser.add_argument("--country", dest="country", help="Country name or ISO code", required=True)
        parser.add_argument(
            "--iso-code",
            dest="iso_code",
            help="Optional explicit ISO code (overrides auto-resolution)",
        )
        parser.add_argument("--grid-size", dest="grid_size", type=int, default=4)
        parser.add_argument("--min-weight", dest="min_weight", type=float, default=0.5)
        parser.add_argument("--max-weight", dest="max_weight", type=float, default=2.0)
        parser.add_argument("--max-samples", dest="max_samples", type=int, default=50000)
        parser.add_argument(
            "--output-path",
            dest="output_path",
            help="Optional override for projection_weights output directory",
        )

    def handle(self, *args, **options):
        country = options["country"]
        iso_code = options.get("iso_code")
        grid_size = options["grid_size"]
        min_weight = options["min_weight"]
        max_weight = options["max_weight"]
        max_samples = options["max_samples"]
        output_path = options.get("output_path")

        if not country and not iso_code:
            raise CommandError("--country or --iso-code must be provided")

        service = ProjectionWeightService()
        config = {
            "country_name": country,
            "iso_code": iso_code,
            "grid_size": grid_size,
            "min_weight": min_weight,
            "max_weight": max_weight,
            "max_samples": max_samples,
            "output_path": output_path,
        }

        result = service.execute(config)

        if not result.get("weights"):
            self.stdout.write(self.style.WARNING("No projection weights learned (insufficient data)."))
            return

        w = result["weights"]
        self.stdout.write(
            self.style.SUCCESS(
                "Learned weights for {country}: w_geo={w_geo:.3f}, w_name={w_name:.3f}, w_class={w_class:.3f}, "
                "F1={f1:.4f} (samples={n})".format(
                    country=result["country_code"],
                    w_geo=w["w_geo"],
                    w_name=w["w_name"],
                    w_class=w["w_class"],
                    f1=result["validation_accuracy"],
                    n=result["sample_count"],
                )
            )
        )
