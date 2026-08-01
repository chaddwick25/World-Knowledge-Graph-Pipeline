# USLP Score Normalization & Threshold Update

## Overview
This document describes the USLP (Unsupervised Spatial Link Prediction) score normalization changes implemented to fix thresholding issues.

## Problem
With threshold 0.6 on a sum of 3 components:
- Average geo_score: ~0.8-0.9
- Average name_score: ~0.6-0.8
- Average class_score: ~0.5-0.7
- Total average: ~2.0-2.4 (well above 0.6 threshold)

This meant almost all candidates passed the threshold, resulting in near-zero rejections.

## Solution
Normalize the score to [0,1] before applying threshold:
- `unnormalized_score = geo_score + name_score + class_score` (range [0, 3.0])
- `normalized_score = unnormalized_score / 3.0` (range [0, 1.0])
- Threshold updated from 0.6 → 0.7 (applied to normalized_score)

## Database Changes
- Renamed field: `total_score` → `unnormalized_score`
- Added field: `normalized_score` (default=0.0)
- Both fields stored in `SpatialTripletScore` and `SpatialTripletScoreRejected`

## Code Changes

### Models (`backend/igea/models.py`)
- Updated `SpatialTripletScore` and `SpatialTripletScoreRejected`
- Added `unnormalized_score` and `normalized_score` fields with `default=0.0`
- Updated indexes and ordering to use `normalized_score`

### Services
- `backend/igea/services/spatial_link_prediction.py`: Updated `_total_score()` to return tuple, threshold gates on normalized
- `backend/igea/services/gpu_uslp_service.py`: GPU path calculates normalized scores

### Tasks & Commands
- `backend/igea/tasks.py`: Default threshold 0.6 → 0.7
- `backend/igea/management/commands/predict_spatial_links.py`: Default threshold 0.6 → 0.7

### API & Views
- `backend/api/views_worldkg_pipeline.py`: Score bucketing uses `normalized_score`
- `backend/worldkg_nca/management/commands/inspect_worldkg_state.py`: Output references `normalized_score`

### Documentation
- `docs/preserving_the_process/USLP.md`: Updated score normalization section
- `WINDSURF_RULES.md`: Updated Rule 3.4
- `backend/igea/README.md`: Updated field descriptions and examples

## Migration
Since databases were dropped and recreated, old migrations were removed and fresh migration generated:
```bash
cd backend
python manage.py makemigrations igea
python manage.py migrate igea
python manage.py migrate igea --database=vectors
```

## Testing
Run pipeline test to verify normalization works correctly:
```bash
cd backend
python manage.py predict_spatial_links \
    --country LU \
    --max-heads 10000 \
    --limit 100000 \
    --threshold 0.7 \
    --top-k 5
```

Expected behavior:
- More candidates should be rejected (stricter filtering)
- Accepted links should have `normalized_score >= 0.7`
- Score distribution should show better separation
