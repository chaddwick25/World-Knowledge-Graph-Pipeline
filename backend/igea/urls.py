from django.urls import path
from . import views

urlpatterns = [
    # IGEA alignment
    path('align/', views.run_igea_alignment, name='igea_align'),
    path('align/status/', views.alignment_status, name='igea_status'),
    path('align/results/', views.alignment_results, name='igea_results'),
    
    # USLP triplet scoring
    path('triplets/predict/', views.predict_triplets, name='uslp_predict'),
    path('triplets/score/', views.score_triplet, name='uslp_score'),
    path('triplets/validate/', views.validate_triple, name='uslp_validate'),
]
