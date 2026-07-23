from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from api.models import PbfFile
import logging
from pathlib import Path

# NOTE: AssetGenerationService imported lazily inside GenerateAssetBundleView.post

logger = logging.getLogger(__name__)


class AssetBundleListView(APIView):
    """List all asset bundles with optional filtering"""
    
    def get(self, request):
        bundles = ProcessedAssetBundle.objects.all()
        
        # Filter by status
        status_filter = request.query_params.get('status')
        if status_filter:
            bundles = bundles.filter(status=status_filter)
        
        # Filter by source PBF
        pbf_id = request.query_params.get('pbf_id')
        if pbf_id:
            bundles = bundles.filter(source_pbf_id=pbf_id)
        
        data = [{
            'id': bundle.id,
            'source_pbf': {
                'id': str(bundle.source_pbf.id),
                'path': bundle.source_pbf.path,
                'size_bytes': bundle.source_pbf.size_bytes
            },
            'status': bundle.status,
            'asset_directory': bundle.asset_directory,
            'generation_config': bundle.generation_config,
            'created_at': bundle.created_at,
            'updated_at': bundle.updated_at
        } for bundle in bundles]
        
        return Response(data)


class AssetBundleDetailView(APIView):
    """Get details of a specific asset bundle"""
    
    def get(self, request, bundle_id):
        bundle = get_object_or_404(ProcessedAssetBundle, id=bundle_id)
        
        # Get file statistics if available
        asset_files = {}
        try:
            from django.conf import settings
            base_data_dir = settings.BASE_DATA_DIR
            base_dir = Path(getattr(settings, 'ASSET_BUNDLE_DIR', str(Path(base_data_dir) / 'assets')))
            bundle_dir = base_dir / bundle.asset_directory
            
            if bundle_dir.exists():
                for file_name in ['nodes.tsv', 'edges.tsv', 'tags.tsv']:
                    file_path = bundle_dir / file_name
                    if file_path.exists():
                        asset_files[file_name] = {
                            'exists': True,
                            'size_bytes': file_path.stat().st_size,
                            'path': str(file_path)
                        }
                    else:
                        asset_files[file_name] = {'exists': False}
        except Exception as e:
            logger.warning(f"Could not read asset files: {e}")
        
        data = {
            'id': bundle.id,
            'source_pbf': {
                'id': str(bundle.source_pbf.id),
                'path': bundle.source_pbf.path,
                'size_bytes': bundle.source_pbf.size_bytes,
                'has_history': bundle.source_pbf.has_history
            },
            'status': bundle.status,
            'asset_directory': bundle.asset_directory,
            'generation_config': bundle.generation_config,
            'created_at': bundle.created_at,
            'updated_at': bundle.updated_at,
            'asset_files': asset_files
        }
        
        return Response(data)


class GenerateAssetBundleView(APIView):
    """Generate a new asset bundle from a PBF file"""
    
    def post(self, request):
        pbf_id = request.data.get('pbf_id')
        
        if not pbf_id:
            return Response(
                {'error': 'pbf_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Verify PBF file exists
            pbf_file = get_object_or_404(PbfFile, id=pbf_id)
            
            # Check if asset bundle already exists
            existing_bundle = ProcessedAssetBundle.objects.filter(
                source_pbf=pbf_file,
                status__in=[
                    ProcessedAssetBundle.Status.PENDING,
                    ProcessedAssetBundle.Status.PROCESSING,
                    ProcessedAssetBundle.Status.COMPLETED
                ]
            ).first()
            
            if existing_bundle:
                return Response(
                    {
                        'message': 'Asset bundle already exists for this PBF file',
                        'bundle_id': existing_bundle.id,
                        'status': existing_bundle.status
                    },
                    status=status.HTTP_200_OK
                )
            
            # Start asset generation
            from extraction.services.asset_generation_service import AssetGenerationService
            service = AssetGenerationService(pbf_id)
            asset_bundle = service.generate_assets()
            
            return Response(
                {
                    'message': 'Asset generation completed successfully',
                    'bundle_id': asset_bundle.id,
                    'status': asset_bundle.status,
                    'statistics': asset_bundle.generation_config.get('statistics', {})
                },
                status=status.HTTP_201_CREATED
            )
            
        except PbfFile.DoesNotExist:
            return Response(
                {'error': f'PBF file with ID {pbf_id} not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Asset generation failed: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DeleteAssetBundleView(APIView):
    """Delete an asset bundle and its files"""
    
    def delete(self, request, bundle_id):
        bundle = get_object_or_404(ProcessedAssetBundle, id=bundle_id)
        
        # Delete physical files
        try:
            from django.conf import settings
            import shutil
            
            base_data_dir = settings.BASE_DATA_DIR
            base_dir = Path(getattr(settings, 'ASSET_BUNDLE_DIR', str(Path(base_data_dir) / 'assets')))
            bundle_dir = base_dir / bundle.asset_directory
            
            if bundle_dir.exists():
                shutil.rmtree(bundle_dir)
                logger.info(f"Deleted asset directory: {bundle_dir}")
        except Exception as e:
            logger.warning(f"Could not delete asset files: {e}")
        
        # Delete database record
        bundle.delete()
        
        return Response(
            {'message': 'Asset bundle deleted successfully'},
            status=status.HTTP_204_NO_CONTENT
        )


class AssetBundleStatsView(APIView):
    """Get statistics about asset bundles"""
    
    def get(self, request):
        total_bundles = ProcessedAssetBundle.objects.count()
        
        stats = {
            'total_bundles': total_bundles,
            'by_status': {
                'pending': ProcessedAssetBundle.objects.filter(status=ProcessedAssetBundle.Status.PENDING).count(),
                'processing': ProcessedAssetBundle.objects.filter(status=ProcessedAssetBundle.Status.PROCESSING).count(),
                'completed': ProcessedAssetBundle.objects.filter(status=ProcessedAssetBundle.Status.COMPLETED).count(),
                'failed': ProcessedAssetBundle.objects.filter(status=ProcessedAssetBundle.Status.FAILED).count()
            },
            'recent_bundles': []
        }
        
        # Get 5 most recent bundles
        recent = ProcessedAssetBundle.objects.all()[:5]
        for bundle in recent:
            stats['recent_bundles'].append({
                'id': bundle.id,
                'source_pbf_path': bundle.source_pbf.path,
                'status': bundle.status,
                'created_at': bundle.created_at
            })
        
        return Response(stats)
