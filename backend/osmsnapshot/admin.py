"""Admin for the osmsnapshot app — the operator job-control surface.

ISSUE-admin-job-control-plane: the Vue-side pipeline triggers are disabled,
so countries are launched from here. Two operator forms (run country
pipeline, backfill snapshots) plus a per-country status view whose
"country complete" state is derived from SnapshotJob rows, never stored.

Admin is staff-only and local/tailnet-gated (AdminHostGateMiddleware), so
no additional permission layer is added here.
"""

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import path
from django.utils.html import format_html

from .models import Snapshot, SnapshotJob


# ── Operator forms ───────────────────────────────────────────────────────
def _country_choices():
    from core.models import CountryPipelineProfile
    return [
        (p.iso2, f"{p.canonical_name} ({p.iso2})")
        for p in CountryPipelineProfile.objects.all().order_by("canonical_name")
    ]


class BackfillForm(forms.Form):
    """Control 2: backfill drift-source snapshots, one task per month."""
    country = forms.ChoiceField(label="Country")
    start_month = forms.CharField(help_text="YYYY_MM (floor 2020_09)")
    end_month = forms.CharField(help_text="YYYY_MM (ceiling: latest snapshot)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["country"].choices = _country_choices()


# ── SnapshotJob admin (status table + operator forms) ────────────────────
@admin.register(SnapshotJob)
class SnapshotJobAdmin(admin.ModelAdmin):
    list_display = (
        "country_code", "country_name", "snapshot_date", "status_badge",
        "tier", "total_entities", "started_at", "completed_at",
    )
    list_filter = ("status", "is_drift_source", "country_code")
    search_fields = ("country_name", "country_code", "snapshot_date")
    ordering = ("-snapshot_date",)
    change_list_template = "admin/osmsnapshot/snapshotjob/change_list.html"
    readonly_fields = (
        "id", "snapshot_date", "country_code", "country_name", "status",
        "is_drift_source", "pipeline_run", "celery_task_id",
        "total_entities", "total_aligned", "total_spatial_links",
        "error_message", "started_at", "completed_at", "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Rows are written by the pipeline, not by hand — read-only detail.
        return False

    def status_badge(self, obj):
        colors = {
            "pending": "#6b7280", "running": "#2563eb",
            "completed": "#16a34a", "failed": "#dc2626",
        }
        return format_html(
            '<b style="color: {};">{}</b>',
            colors.get(obj.status, "#6b7280"), obj.status,
        )
    status_badge.short_description = "Status"

    def tier(self, obj):
        return "drift-source" if obj.is_drift_source else "queryable"
    tier.short_description = "Tier"

    # ── Operator views ────────────────────────────────────────────────
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "backfill/",
                self.admin_site.admin_view(self.backfill_view),
                name="osmsnapshot_snapshotjob_backfill",
            ),
            path(
                "job-status/",
                self.admin_site.admin_view(self.job_status_view),
                name="osmsnapshot_snapshotjob_job_status",
            ),
        ]
        return custom + urls

    def _render(self, request, template, context):
        from django.shortcuts import render
        ctx = self.admin_site.each_context(request)
        ctx.update(context)
        return render(request, template, ctx)

    def backfill_view(self, request):
        from pipeline.backfill import BackfillError, run_snapshot_backfill
        if request.method == "POST":
            form = BackfillForm(request.POST)
            if form.is_valid():
                try:
                    result = run_snapshot_backfill(
                        country_name=form.cleaned_data["country"],
                        start_month=form.cleaned_data["start_month"],
                        end_month=form.cleaned_data["end_month"],
                    )
                    if result["dispatched"]:
                        months = ", ".join(result["months"])
                        messages.success(
                            request,
                            f"Backfill dispatched: {len(result['months'])} "
                            f"month(s) [{months}] "
                            f"(run {result['pipeline_run_id']}).",
                        )
                    else:
                        messages.warning(
                            request, result.get(
                                "message", "Nothing to backfill"
                            ),
                        )
                    return HttpResponseRedirect(
                        "../../snapshotjob/?is_drift_source__exact=1"
                    )
                except BackfillError as exc:
                    form.add_error(None, str(exc))
        else:
            form = BackfillForm()
        return self._render(
            request, "admin/osmsnapshot/snapshotjob/backfill.html",
            {"form": form, "title": "Backfill snapshots"},
        )

    def job_status_view(self, request):
        """Per-country status table + derived 'country complete' state.

        Derived, never stored: latest queryable snapshot exists AND the
        completed drift-source series length >=
        ``DRIFT_SERIES_COMPLETE_THRESHOLD``.
        """
        from django.conf import settings

        from core.models import CountryPipelineProfile
        from .models import SnapshotJob

        threshold = getattr(settings, "DRIFT_SERIES_COMPLETE_THRESHOLD", 3)
        iso = (request.GET.get("country") or "").strip().upper()
        countries = [
            (p.iso2, p.canonical_name)
            for p in CountryPipelineProfile.objects.all().order_by(
                "canonical_name"
            )
        ]

        jobs, has_latest_queryable, drift_series_len, complete = [], False, 0, False
        if iso:
            jobs = list(
                SnapshotJob.objects.filter(country_code__iexact=iso)
                .order_by("-snapshot_date")
            )
            queryable = [
                j for j in jobs
                if j.status == SnapshotJob.Status.COMPLETED
                and not j.is_drift_source
            ]
            has_latest_queryable = bool(queryable)
            drift_series_len = sum(
                1 for j in jobs
                if j.status == SnapshotJob.Status.COMPLETED and j.is_drift_source
            )
            complete = has_latest_queryable and drift_series_len >= threshold

        return self._render(
            request, "admin/osmsnapshot/snapshotjob/job_status.html",
            {
                "title": "Job status",
                "countries": countries,
                "selected_iso": iso,
                "jobs": jobs,
                "threshold": threshold,
                "has_latest_queryable": has_latest_queryable,
                "drift_series_len": drift_series_len,
                "country_complete": complete,
            },
        )


# ── Snapshot admin (read-only registry) ──────────────────────────────────
@admin.register(Snapshot)
class SnapshotAdmin(admin.ModelAdmin):
    list_display = ("country_code", "snapshot_date", "pbf_file", "created_at")
    list_filter = ("country_code",)
    search_fields = ("country_code", "snapshot_date")
    readonly_fields = [f.name for f in Snapshot._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
