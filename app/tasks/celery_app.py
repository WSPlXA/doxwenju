from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "doxwenju",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.template_tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
)
