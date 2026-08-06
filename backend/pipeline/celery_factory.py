"""Factory for Celery app creation — encapsulates bootstrap.

Celery is a hard dependency (pinned in ``requirements.txt`` as
``celery==4.4.7``). Every runtime path — the Docker worker service
(``celery -A pipeline.celery_app worker``), the eager ``--wait`` mode in
``run_pipeline``, and the normal async dispatch — requires it. There is no
``try/except ImportError`` guard because the "Celery not installed" branch
is dead code that can never trigger in any real environment.

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
