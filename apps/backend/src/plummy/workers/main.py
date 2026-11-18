"""Worker entrypoint: ``celery -A plummy.workers.main worker``.

Configuration is validated on import: an invalid configuration stops the worker (the error never contains
secret values; ``python -m plummy.check_config`` runs first in the container command).
"""

from __future__ import annotations

from typing import Any

from celery import signals

from plummy.core.config import get_settings
from plummy.core.logging import configure_logging
from plummy.workers.app import create_celery_app
from plummy.workers.observability import connect_task_logging

# Celery writes task arguments ("received", celery.worker.strategy), return values and exception messages
# (celery.app.trace); lifecycle events without payloads are logged by plummy.workers.observability instead.
QUIET_LOGGERS = ("celery.app.trace", "celery.worker.strategy")

settings = get_settings()
app = create_celery_app(settings)
connect_task_logging()


def _setup_logging(**_: Any) -> None:
    configure_logging(settings.log_level.value, quiet_loggers=QUIET_LOGGERS)


# Connected receivers replace Celery's own logging setup.
signals.setup_logging.connect(_setup_logging, weak=False, dispatch_uid="plummy.setup_logging")
