import json
from channels.generic.websocket import AsyncWebsocketConsumer

class WorldKGPipelineConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for WorldKG pipeline progress.

    Clients connect to ws/pipeline/<session_id>/ and receive JSON messages:
      { "type": "step_update", "step": 3, "total": 8, "name": "...",
        "status": "in_progress|completed|failed|skipped",
        "message": "...", "pct": 62 }
      { "type": "pipeline_complete", "session_id": "...", "status": "completed|failed" }
      { "type": "log", "message": "..." }
    """

    async def connect(self):
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        self.group_name = f'pipeline_{self.session_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        pass

    async def step_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'step_update',
            'step': event.get('step', 0),
            'total': event.get('total', 6),
            'name': event['name'],
            'status': event['status'],
            'message': event['message'],
            'pct': event.get('pct', 0),
        }))

    async def pipeline_complete(self, event):
        await self.send(text_data=json.dumps({
            'type': 'pipeline_complete',
            'session_id': event['session_id'],
            'status': event['status'],
            'error': event.get('error', ''),
        }))

    async def log(self, event):
        await self.send(text_data=json.dumps({
            'type': 'log',
            'message': event['message'],
        }))
