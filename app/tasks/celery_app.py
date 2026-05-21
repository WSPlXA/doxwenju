from celery import Celery
from celery.signals import worker_ready

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


@worker_ready.connect
def _cleanup_stale_agent_runs(sender, **kwargs):
    """Mark in-flight agent runs and stuck document versions as failed/error
    when the worker (re)starts.

    Any run in a non-terminal status when the worker comes online means
    the previous worker process died while the task was in-flight.
    We surface it as 'failed' so the user can retry immediately.

    Similarly, document versions stuck in 'parsing' are reset to 'error'
    so users know to re-upload.
    """
    try:
        from sqlalchemy import update

        from app.db.session import SessionLocal
        from app.models.document import AgentRun, DocumentVersion

        db = SessionLocal()
        try:
            # Mark stuck agent runs as failed
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.status.in_(
                        ["queued", "ingesting", "mapping", "patching",
                         "postprocessing", "rendering", "repairing"]
                    )
                )
                .values(
                    status="failed",
                    error_message="Worker restarted mid-task. Please retry.",
                )
            )
            # Mark stuck document versions (stuck in 'parsing') as error
            db.execute(
                update(DocumentVersion)
                .where(DocumentVersion.status.in_(["parsing", "embedding"]))
                .values(
                    status="error",
                    error_message="Worker restarted during processing. Please re-upload.",
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # never crash the worker on startup
