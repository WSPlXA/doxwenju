from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.document import Document, DocumentVersion, PatchPlan
from app.services.embeddings import embed_format_atoms, embed_target_elements
from app.services.ingestion import ingest_template_version
from app.services.patch_engine import execute_patch_plan
from app.services.patch_planner import rebuild_patch_plan
from app.services.rendering import render_libreoffice_precheck
from app.services.repair_planner import build_internal_repair_plan
from app.services.target_mapping import rebuild_mapping_results
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


@celery_app.task(name="embedding_backfill")
def embedding_backfill(document_version_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        version = db.get(DocumentVersion, document_version_id)
        if version is None:
            raise ValueError(f"DocumentVersion not found: {document_version_id}")
        atom_counts = embed_format_atoms(db, document_version_id)
        target_counts = (
            embed_target_elements(db, document_version_id)
            if version.document.kind == "target"
            else {"embedded": 0, "skipped": 0, "failed": 0}
        )
        return {
            "document_version_id": document_version_id,
            "format_atoms": atom_counts,
            "target_elements": target_counts,
        }
    finally:
        db.close()


@celery_app.task(name="target_ingestion")
def target_ingestion(document_version_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        version = db.get(DocumentVersion, document_version_id)
        if version is None:
            raise ValueError(f"DocumentVersion not found: {document_version_id}")
        ingest_template_version(db, version)
        template_version = db.scalar(
            select(DocumentVersion)
            .join(Document)
            .where(Document.is_current_template.is_(True), DocumentVersion.status == "done")
            .order_by(DocumentVersion.version.desc(), DocumentVersion.created_at.desc())
            .limit(1)
        )
        mapping_count = 0
        patch_plan_id = None
        if template_version is not None:
            mapping_count = len(
                rebuild_mapping_results(db, document_version_id, template_version.id)
            )
            patch_plan = rebuild_patch_plan(db, document_version_id, template_version.id)
            patch_plan_id = patch_plan.id
        return {
            "document_version_id": document_version_id,
            "status": "done",
            "mapping_count": mapping_count,
            "patch_plan_id": patch_plan_id,
        }
    finally:
        db.close()


@celery_app.task(name="patch_plan_execute")
def patch_plan_execute(patch_plan_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        plan = db.get(PatchPlan, patch_plan_id)
        if plan is None:
            raise ValueError(f"PatchPlan not found: {patch_plan_id}")
        execution = execute_patch_plan(db, patch_plan_id)
        return {
            "patch_plan_id": patch_plan_id,
            "execution_id": execution.id,
            "status": execution.status,
            "output_document_version_id": execution.output_document_version_id,
        }
    finally:
        db.close()


@celery_app.task(name="render_precheck")
def render_precheck(document_version_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        snapshot = render_libreoffice_precheck(db, document_version_id)
        return {
            "document_version_id": document_version_id,
            "snapshot_id": snapshot.id,
            "status": snapshot.status,
            "renderer": snapshot.renderer,
            "page_count": snapshot.page_count,
        }
    finally:
        db.close()


@celery_app.task(name="auto_repair_cycle")
def auto_repair_cycle(patch_plan_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        repair_plan = build_internal_repair_plan(db, patch_plan_id)
        if repair_plan is None:
            return {
                "source_patch_plan_id": patch_plan_id,
                "status": "no_repair_needed",
                "repair_patch_plan_id": None,
            }
        execution = execute_patch_plan(db, repair_plan.id)
        return {
            "source_patch_plan_id": patch_plan_id,
            "status": execution.status,
            "repair_patch_plan_id": repair_plan.id,
            "execution_id": execution.id,
            "output_document_version_id": execution.output_document_version_id,
        }
    finally:
        db.close()
