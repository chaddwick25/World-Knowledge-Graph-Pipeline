from django.urls import path
from .api import VectorSearchAPIView

urlpatterns = [
    path('search/', VectorSearchAPIView.as_view(), name='vector-search'),
]
