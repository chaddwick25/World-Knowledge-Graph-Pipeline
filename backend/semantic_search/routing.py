from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/pipeline/(?P<session_id>[0-9a-f-]+)/$', consumers.WorldKGPipelineConsumer.as_asgi()),
]
