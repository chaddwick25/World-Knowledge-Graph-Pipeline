"""
warm_query_path — Pre-touch the request-path tables at startup.

The enrichment leg of ``execute_query`` joins OsmEntity +
SpatialTripletScore + factor_spectral_node_metric for the latest
snapshot; on a fresh restart Postgres serves those pages from disk and
the first user query pays the cold-read (~25s observed on a cold boot
vs ~3.5s warm). This runs ``EntityContextService.get_context`` — the
same code path the request uses — against representative entities,
which warms shared buffers and the OS page cache for every later
request. Fail-soft: exits quietly on an empty DB or any fetch error so
boot never blocks on it.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Warm the execute-query data path (DB buffers + snapshot lookup)."

    def handle(self, *args, **options):
        try:
            self._warm()
        except Exception as exc:  # noqa: BLE001 — boot must never block
            self.stderr.write(f"Query-path warm-up failed: {exc}")

    def _warm(self):
        from worldkg_nca.models import OsmEntity, SpectralNodeMetric
        from worldkg_nca.snapshot_utils import get_latest_snapshot_id
        from semantic_search.services.entity_context_service import (
            EntityContextService,
        )

        snapshot_id = get_latest_snapshot_id()

        # Entities carrying factor rows exercise the full fetch — the
        # community join (check_availability + resolve_metrics) only runs
        # when spectral data exists. Fall back to any classified rows.
        factor_rows = list(
            SpectralNodeMetric.objects.using("vectors")
            .filter(snapshot_id=snapshot_id)
            .values_list("osm_id", "country_code")[:10]
        )
        if factor_rows:
            osm_ids = [osm_id for osm_id, _ in factor_rows]
            country_code = factor_rows[0][1] or ""
        else:
            entity_rows = list(
                OsmEntity.objects.using("vectors")
                .filter(snapshot_id=snapshot_id)
                .exclude(wkg_class__isnull=True)
                .exclude(wkg_class="")
                .values_list("osm_id", "country_code")[:10]
            )
            if not entity_rows:
                self.stdout.write(
                    "No entities for snapshot — skipping query-path warm-up"
                )
                return
            osm_ids = [osm_id for osm_id, _ in entity_rows]
            country_code = entity_rows[0][1] or ""

        context = EntityContextService.get_context(
            osm_ids, country_code, snapshot_id=snapshot_id,
        )
        self.stdout.write(
            "Query-path warm-up OK (snapshot=%s, uslp=%d, communities=%d, "
            "classes=%d)"
            % (
                snapshot_id,
                len(context.get("uslp_links") or []),
                len(context.get("communities") or {}),
                len(context.get("class_distribution") or []),
            )
        )
