from django.urls import path
from .views import GeoVectorsEncodeView

urlpatterns = [
    path('encode/', GeoVectorsEncodeView.as_view(), name='geovectors-encode'),
]
