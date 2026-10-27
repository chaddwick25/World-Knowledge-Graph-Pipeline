from django import forms
from django.contrib import admin
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import path

from .models import PipelineRun


class PipelineRunTriggerForm(forms.Form):
    """Launch a country pipeline — the full capability the old frontend
    GUI trigger had (country, snapshot date, skip flags, force)."""
    country = forms.ChoiceField(label="Country")
    snapshot_date = forms.CharField(
        label="Snapshot date (optional)",
        required=False,
        help_text="YYYY_MM_DD — empty uses the deployment default",
    )
    skip_entropy_gate = forms.BooleanField(
        required=False,
        help_text="Bypass the entropy check (manual forcing)",
    )
    skip_enrich = forms.BooleanField(
        required=False,
        help_text="Skip the WorldKG enrichment step (debugging)",
    )
    force = forms.BooleanField(
        required=False,
        help_text="Re-run even if this (country, date) is already RUNNING",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import CountryPipelineProfile
        self.fields["country"].choices = [
            (p.iso2, f"{p.canonical_name} ({p.iso2})")
            for p in CountryPipelineProfile.objects.all().order_by(
                "canonical_name"
            )
        ]


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    """Run history + the pipeline trigger (launch lives with the runs)."""
    list_display = (
        "country_code", "country_name", "pipeline_type", "status",
        "current_stage", "queued_at", "completed_at",
    )
    list_filter = ("pipeline_type", "status")
    search_fields = ("country_name", "country_code", "id")
    readonly_fields = [
        f.name for f in PipelineRun._meta.fields
    ]
    change_list_template = "admin/core/pipelinerun/change_list.html"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "trigger/",
                self.admin_site.admin_view(self.trigger_view),
                name="core_pipelinerun_trigger",
            ),
        ]
        return custom + urls

    def trigger_view(self, request):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
            dispatch_country_pipeline,
        )
        if request.method == "POST":
            form = PipelineRunTriggerForm(request.POST)
            if form.is_valid():
                try:
                    result = dispatch_country_pipeline(
                        country_name=form.cleaned_data["country"],
                        snapshot_date=form.cleaned_data["snapshot_date"] or None,
                        skip_entropy_gate=form.cleaned_data["skip_entropy_gate"],
                        skip_enrich=form.cleaned_data["skip_enrich"],
                        force=form.cleaned_data["force"],
                    )
                    messages.success(
                        request,
                        f"Pipeline dispatched for {result['country_name']} "
                        f"@ {result['snapshot_date']} "
                        f"(run {result['pipeline_run_id']}).",
                    )
                    return HttpResponseRedirect("..")
                except PipelineDispatchError as exc:
                    form.add_error(None, str(exc))
        else:
            form = PipelineRunTriggerForm()
        from django.shortcuts import render
        ctx = self.admin_site.each_context(request)
        ctx.update({"form": form, "title": "Run country pipeline"})
        return render(
            request, "admin/core/pipelinerun/trigger.html", ctx,
        )
