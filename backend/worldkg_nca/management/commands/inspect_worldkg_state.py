from django.core.management.base import BaseCommand
from django.db.models import Count
from django.db.utils import OperationalError
from django.contrib.gis.geos import Polygon

from worldkg_nca.models import OsmEntity
from igea.models import SpatialTripletScore


class Command(BaseCommand):
    help = "Inspect WorldKG enrichment, Wikidata alignment, and USLP spatial links."

    def handle(self, *args, **options):
        self.stdout.write("\n=== OsmEntity (vectors DB) — WorldKG / Wikidata / GV-NLE ===\n")

        # WorldKG enrichment stats
        enriched_worldkg = (
            OsmEntity.objects.using("vectors")
            .exclude(wkg_class__isnull=True)
            .count()
        )
        self.stdout.write(f"WorldKG-enriched entities (wkg_class IS NOT NULL): {enriched_worldkg:,}")

        # Wikidata alignment stats (native + IGEA)
        with_wikidata = (
            OsmEntity.objects.using("vectors")
            .exclude(wikidata_uri__isnull=True)
            .count()
        )
        self.stdout.write(f"Entities with wikidata_uri: {with_wikidata:,}")

        # GV-NLE embedding stats
        with_gv_nle = (
            OsmEntity.objects.using("vectors")
            .exclude(gv_nle_embedding__isnull=True)
            .count()
        )
        self.stdout.write(f"Entities with GV-NLE embedding: {with_gv_nle:,}")

        # Sample enriched entities (both WorldKG + Wikidata, if any)
        self.stdout.write("\nSample enriched entities (up to 5 with wkg_class & wikidata_uri):")
        sample_qs = (
            OsmEntity.objects.using("vectors")
            .exclude(wkg_class__isnull=True)
            .exclude(wikidata_uri__isnull=True)[:5]
        )
        if not sample_qs:
            self.stdout.write("  (no entities found with both wkg_class and wikidata_uri)")
        else:
            for e in sample_qs:
                name = e.tags.get("name") or e.tags.get("amenity") or e.tags.get("highway") or "<unnamed>"
                self.stdout.write(
                    f"  {e.osm_type}/{e.osm_id}: {name}\n"
                    f"    wkg_class      = {e.wkg_class}\n"
                    f"    wkg_depth      = {e.wkg_depth}\n"
                    f"    wkg_type       = {e.wkg_type_key}={e.wkg_type_value}\n"
                    f"    wikidata_uri   = {e.wikidata_uri}\n"
                    f"    wkg_enriched_at= {e.wkg_enriched_at}\n"
                )

        self.stdout.write("\n=== USLP Spatial Links (default DB) — igea_spatial_triplet_score ===\n")

        total_links = SpatialTripletScore.objects.count()
        predicted_links = SpatialTripletScore.objects.filter(predicted=True).count()

        self.stdout.write(f"Total SpatialTripletScore records: {total_links:,}")
        self.stdout.write(f"Accepted (predicted=True, normalized_score >= threshold): {predicted_links:,}")

        # Show relation distribution among accepted links
        self.stdout.write("\nTop relations among accepted links:")
        relation_counts = (
            SpatialTripletScore.objects
            .filter(predicted=True)
            .values("relation")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )
        if not relation_counts:
            self.stdout.write("  (no predicted links found)")
        else:
            for rc in relation_counts:
                self.stdout.write(f"  {rc['relation']}: {rc['count']:,}")

            # For each top relation, show a few sample links
            self.stdout.write("\nSample accepted links by relation (up to 3 per relation):")
            for rc in relation_counts:
                rel = rc["relation"]
                self.stdout.write(f"\nRelation: {rel}")
                rel_links = SpatialTripletScore.objects.filter(predicted=True, relation=rel)[:3]
                for link in rel_links:
                    self.stdout.write(
                        f"  ({link.head_osm_type}/{link.head_osm_id}) --{link.relation}-> "
                        f"({link.tail_osm_type}/{link.tail_osm_id})\n"
                        f"    geo={link.geo_score:.3f}, name={link.name_score:.3f}, "
                        f"topo={link.topo_score:.3f}, norm={link.normalized_score:.3f}, "
                        f"predicted={link.predicted}, geohash_precision={link.geohash_precision}\n"
                    )

        # ------------------------------------------------------------------
        # IE (Ireland) subset: links with heads in IE bounding box
        # BBox taken from WikidataCandidate: country=IE bbox=[-10.50,51.42,-5.30,55.38]
        #
        # NOTE: This section is best-effort. If the vectors DB is under memory
        # pressure and closes the connection, we skip IE-specific stats rather
        # than failing the whole command.
        # ------------------------------------------------------------------
        try:
            ie_bbox = (-10.50, 51.42, -5.30, 55.38)
            ie_polygon = Polygon.from_bbox(ie_bbox)

            # Step 1: unique heads from predicted links (default DB, small)
            head_ids = list(
                SpatialTripletScore.objects
                .filter(predicted=True)
                .values_list("head_osm_id", flat=True)
                .distinct()
            )

            # Step 2: fetch only those entities from vectors DB
            head_entities = OsmEntity.objects.using("vectors").filter(osm_id__in=head_ids)

            # Step 3: filter in Python by IE polygon
            ie_head_ids = []
            for e in head_entities.iterator(chunk_size=500):
                if e.geom and e.geom.within(ie_polygon):
                    ie_head_ids.append(e.osm_id)

            self.stdout.write(
                f"\n=== IE (Ireland) USLP Spatial Links — heads within IE bounding box ({len(ie_head_ids):,} OSM entities) ===\n"
            )

            if ie_head_ids:
                ie_links_qs = SpatialTripletScore.objects.filter(
                    predicted=True,
                    head_osm_id__in=ie_head_ids,
                )

                ie_total = ie_links_qs.count()
                self.stdout.write(f"Total predicted links with heads in IE bbox: {ie_total:,}")

                if ie_total:
                    # Relation distribution for IE-only links
                    self.stdout.write("\nTop relations among IE predicted links:")
                    ie_rel_counts = (
                        ie_links_qs.values("relation")
                        .annotate(count=Count("id"))
                        .order_by("-count")[:10]
                    )

                    if not ie_rel_counts:
                        self.stdout.write("  (no IE predicted links found)")
                    else:
                        for rc in ie_rel_counts:
                            self.stdout.write(
                                f"  {rc['relation']}: {rc['count']:,}"
                            )
                else:
                    self.stdout.write("  (no predicted links with heads in IE bbox)")
            else:
                self.stdout.write("No OSM heads from predicted links fall within IE bounding box.")
        except OperationalError as exc:
            self.stdout.write(
                "\n[WARN] Skipping IE (Ireland) subset stats due to vectors DB error: "
                f"{exc}"
            )

        self.stdout.write("\nInspection complete.\n")
