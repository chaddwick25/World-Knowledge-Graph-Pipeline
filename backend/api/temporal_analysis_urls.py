"""
URL patterns for Temporal Tag Tracking (In-Memory) API endpoints
"""

from django.urls import path
from .temporal_analysis_views import (
    AnalyzeTagEvolutionView,
    GetTagTimeSeriesView
)

urlpatterns = [
    path('', AnalyzeTagEvolutionView.as_view(), name='analyze-tag-evolution'),
    path('time-series/', GetTagTimeSeriesView.as_view(), name='get-tag-time-series'),
]
