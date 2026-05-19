from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.document import DocumentVersion
from app.services.ingestion import ingest_template_version
from app.tasks.celery_app import celery_app


@celery_app.task(name="template_ingestion")
def template_ingestion(document_version_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        version = db.get(DocumentVersion, document_version_id)
        if version is None:
            raise ValueError(f"DocumentVersion not found: {document_version_id}")
        ingest_template_version(db, version)
        return {"document_version_id": document_version_id, "status": "done"}
    finally:
        db.close()
