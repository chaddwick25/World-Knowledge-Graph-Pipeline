import os
import sys

from django.apps import AppConfig


class SemanticSearchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'semantic_search'

    def ready(self):
        """Pre-load FastText only for long-running server/worker processes.

        ``ready()`` fires on every Django bootstrap — including the three
        management commands the entrypoint runs before the server starts
        (``makemigrations``, ``migrate``, ``migrate --database=vectors``).
        Loading cc.en.300.bin (~7 GB) in those one-shot commands wasted
        10–30 s each and caused the startup stutter.

        By checking ``sys.argv`` we skip the preload for management
        commands and only warm the model in the process that actually
        serves queries or runs Celery tasks.  ``runserver`` uses Django's
        autoreloader, which spawns a parent (file watcher) and a child
        (request handler); the child sets ``RUN_MAIN=true``, so we preload
        only there to avoid a redundant load in the parent.
        """
        argv = sys.argv
        is_celery = 'celery' in argv
        is_runserver = 'runserver' in argv
        is_asgi = 'daphne' in argv or 'gunicorn' in argv
        is_reloader_child = os.environ.get('RUN_MAIN') == 'true'

        # runserver: only preload in the child process (RUN_MAIN=true)
        # daphne/gunicorn/celery: no autoreload parent, always preload
        should_preload = is_celery or is_asgi or (is_runserver and is_reloader_child)

        if should_preload:
            from semantic_search.services.model_registry_service import ModelRegistryService
            ModelRegistryService.get_fasttext_model()
