"""Factory for Celery app creation — encapsulates bootstrap.

Callers pass the app name, Django settings module, and config namespace as
parameters. The factory returns a ``(app, TaskBase)`` tuple.
"""

from __future__ import annotations
import os

def create_celery_app(
    name: str,
    settings_module: str,
    namespace: str = "CELERY",
):
    """Create and configure a Celery app.

    Args:
        name: Celery app name (e.g. ``"worldkg_pipeline"``).
        settings_module: Django settings module (e.g. ``"backend.settings"``).
        namespace: Celery config namespace (default ``"CELERY"``).

    Returns:
        ``(app, TaskBase)`` where ``TaskBase`` is ``celery.Task``.
    """
    from celery import Celery, Task as CeleryTask

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    app = Celery(name)
    app.config_from_object("django.conf:settings", namespace=namespace)
    app.autodiscover_tasks()
    return app, CeleryTask
