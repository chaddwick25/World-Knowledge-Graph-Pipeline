from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from core.models import Task, ProcessingSession
from core.services.planet_init.osm_wikidata_resolver import get_country_by_name

import logging
import threading

logger = logging.getLogger('geovectors_encoder.services.geovectors_service')


class GeoVectorsEncodeView(APIView):
    """
    Trigger GeoVectors encoding for a country.
    Runs in a background thread with WebSocket progress updates.
    """
    def post(self, request):
        country_name = request.data.get('country_name')
        force = request.data.get('force', False)
        session_id = request.data.get('session_id')

        if not country_name:
            return Response({'error': 'country_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        iso = None
        try:
            country_meta = get_country_by_name(country_name)
            if country_meta and country_meta.get('iso2'):
                iso = country_meta['iso2']
        except Exception:
            iso = None

        session = None
        if session_id:
            try:
                session = ProcessingSession.objects.get(id=session_id)
            except ProcessingSession.DoesNotExist:
                logger.warning(f"ProcessingSession {session_id} not found, creating new")
                session_id = None

        if not session:
            session = ProcessingSession.objects.create(
                session_name=f"GeoVectors Encoding: {country_name}",
                session_type=ProcessingSession.SessionType.GEOVECTORS_ENCODING,
                configuration={'country': country_name, 'force': force}
            )
            session_id = str(session.id)

        task = Task.objects.create(
            task_type=Task.TaskType.GEOVECTORS_ENCODE,
            parameters={
                'country_name': country_name,
                'iso': iso,
                'force': bool(force),
                'session_id': session_id,
            },
            processing_session=session,
        )

        logger.info(
            "Starting GeoVectors encoding for country=%s force=%s session_id=%s",
            country_name, bool(force), session_id,
        )

        def run_encoding_background():
            try:
                from channels.layers import get_channel_layer
                from asgiref.sync import async_to_sync
                channel_layer = get_channel_layer()
                group_name = f'pipeline_{session_id}'

                def push_update(name, status, message, pct=0):
                    try:
                        async_to_sync(channel_layer.group_send)(
                            group_name,
                            {'type': 'step_update', 'step': 0, 'total': 4,
                             'name': name, 'status': status, 'message': message, 'pct': pct}
                        )
                    except Exception:
                        pass

                def push_complete(status, error=''):
                    try:
                        async_to_sync(channel_layer.group_send)(
                            group_name,
                            {'type': 'pipeline_complete', 'session_id': session_id,
                             'status': status, 'error': error}
                        )
                    except Exception:
                        pass

                push_update('geovectors_encode', 'in_progress', 'Starting GeoVectors encoding...', 10)

                from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService
                service = GeoVectorsEncoderService(n_jobs=4)
                result = service.run_for_country(country_name, force=force)

                task.status = Task.TaskStatus.COMPLETED
                task.results = {'session_id': session_id}
                task.save()

                push_update('geovectors_encode', 'completed', 'GeoVectors encoding complete', 100)
                push_complete('completed')

            except Exception as e:
                logger.error(f"GeoVectors encoding failed for {country_name}: {e}", exc_info=True)
                task.status = Task.TaskStatus.FAILED
                task.results = {'error': str(e)}
                task.save()
                try:
                    from channels.layers import get_channel_layer
                    from asgiref.sync import async_to_sync
                    channel_layer = get_channel_layer()
                    group_name = f'pipeline_{session_id}'
                    async_to_sync(channel_layer.group_send)(
                        group_name,
                        {'type': 'pipeline_complete', 'session_id': session_id,
                         'status': 'failed', 'error': str(e)}
                    )
                except Exception:
                    pass

        thread = threading.Thread(target=run_encoding_background, daemon=True)
        thread.start()

        return Response(
            {
                'task_id': str(task.id),
                'status': task.get_status_display(),
                'task_type': task.get_task_type_display(),
                'country_name': country_name,
                'force': bool(force),
                'session_id': session_id,
            },
            status=status.HTTP_202_ACCEPTED,
        )

