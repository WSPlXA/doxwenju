from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.document import (
    AgentRun,
    AgentStep,
    Document,
    DocumentVersion,
    MappingResult,
    PatchOperation,
    PatchPlan,
    TargetElement,
)
from app.services.embeddings import embed_format_atoms, embed_target_elements
from app.services.ingestion import ingest_template_version
from app.services.patch_engine import execute_patch_plan
from app.services.patch_planner import rebuild_patch_plan
from app.services.rendering import render_libreoffice_precheck
from app.services.repair_planner import build_internal_repair_plan
from app.services.target_mapping import rebuild_mapping_results
from app.services.word_postprocess import apply_word_layout_postprocess
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
    except Exception as exc:
        _mark_version_failed(db, document_version_id, exc)
        raise
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
            .where(Document.is_current_template.is_(True))
            .order_by(DocumentVersion.version.desc(), DocumentVersion.created_at.desc())
            .limit(1)
        )
        mapping_count = 0
        patch_plan_id = None
        if template_version is not None:
            if template_version.status != "done":
                ingest_template_version(db, template_version)
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
    except Exception as exc:
        _mark_version_failed(db, document_version_id, exc)
        raise
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


@celery_app.task(name="format_agent_run")
def format_agent_run(agent_run_id: str) -> dict:
    db: Session = SessionLocal()
    try:
        run = db.get(AgentRun, agent_run_id)
        if run is None:
            raise ValueError(f"AgentRun not found: {agent_run_id}")
        return _run_format_agent(db, run)
    finally:
        db.close()


def _run_format_agent(db: Session, run: AgentRun) -> dict:
    try:
        _update_agent_run(db, run, "ingesting")
        target_version = db.get(DocumentVersion, run.target_document_version_id)
        template_version = db.get(DocumentVersion, run.template_document_version_id)
        if target_version is None:
            raise ValueError(f"Target DocumentVersion not found: {run.target_document_version_id}")
        if template_version is None:
            raise ValueError(
                f"Template DocumentVersion not found: {run.template_document_version_id}"
            )
        if template_version.status != "done":
            ingest_template_version(db, template_version)

        _record_agent_step(
            db,
            run.id,
            0,
            "ingest",
            "running",
            {"targetDocumentVersionId": target_version.id},
        )
        target_element_count = _target_element_count(db, target_version.id)
        if target_version.status != "done" or target_element_count == 0:
            ingest_template_version(db, target_version)
            target_element_count = _target_element_count(db, target_version.id)
        _record_agent_step(
            db,
            run.id,
            0,
            "ingest",
            "done",
            {
                "targetDocumentVersionId": target_version.id,
                "status": target_version.status,
                "targetElementCount": target_element_count,
            },
        )

        _update_agent_run(db, run, "mapping")
        mapping_count = len(
            rebuild_mapping_results(db, target_version.id, template_version.id)
        )
        _record_agent_step(
            db,
            run.id,
            0,
            "mapping",
            "done",
            {
                "mappingCount": mapping_count,
                "reusedExistingMappings": False,
                "templateDocumentVersionId": template_version.id,
            },
        )
        current_plan = _latest_draft_patch_plan(db, target_version.id) or rebuild_patch_plan(
            db, target_version.id, template_version.id
        )
        for round_number in range(1, run.max_rounds + 1):
            _update_agent_run(db, run, "patching", round_count=round_number)
            _record_agent_step(
                db,
                run.id,
                round_number,
                "patch",
                "running",
                {"patchPlanId": current_plan.id, "source": current_plan.source},
                patch_plan_id=current_plan.id,
            )
            execution = execute_patch_plan(db, current_plan.id)
            db.refresh(current_plan)
            run.current_output_document_version_id = execution.output_document_version_id
            _record_agent_step(
                db,
                run.id,
                round_number,
                "patch",
                execution.status,
                execution.summary,
                patch_plan_id=current_plan.id,
                patch_execution_id=execution.id,
            )
            if execution.output_document_version_id is None:
                raise ValueError("Patch execution did not produce an output document")

            _update_agent_run(db, run, "postprocessing")
            postprocessed_version, postprocess_summary = apply_word_layout_postprocess(
                db, execution.output_document_version_id
            )
            run.current_output_document_version_id = postprocessed_version.id
            db.add(run)
            db.commit()
            db.refresh(run)
            _record_agent_step(
                db,
                run.id,
                round_number,
                "word_postprocess",
                postprocess_summary["status"],
                postprocess_summary,
            )

            _update_agent_run(db, run, "rendering")
            baseline_snapshot = render_libreoffice_precheck(db, target_version.id)
            snapshot = render_libreoffice_precheck(db, postprocessed_version.id)
            _record_agent_step(
                db,
                run.id,
                round_number,
                "render",
                snapshot.status,
                {
                    "documentVersionId": postprocessed_version.id,
                    "renderer": snapshot.renderer,
                    "pageCount": snapshot.page_count,
                    "pdfSizeBytes": snapshot.pdf_size_bytes,
                },
                render_snapshot_id=snapshot.id,
            )

            skipped_count = _skipped_operation_count(db, current_plan.id)
            page_drift = _page_count_drift(baseline_snapshot.page_count, snapshot.page_count)
            if skipped_count == 0 and page_drift <= 1:
                return _finish_agent_run(
                    db,
                    run,
                    status="done",
                    summary={
                        "stopReason": "all_operations_applied",
                        "rounds": round_number,
                        "baselinePageCount": baseline_snapshot.page_count,
                        "outputPageCount": snapshot.page_count,
                        "pageCountDrift": page_drift,
                        "lastPatchPlanId": current_plan.id,
                        "lastExecutionId": execution.id,
                        "postprocess": postprocess_summary,
                        "lastRenderSnapshotId": snapshot.id,
                        "renderStatus": snapshot.status,
                    },
                )
            if skipped_count == 0 and page_drift > 1:
                return _finish_agent_run(
                    db,
                    run,
                    status="needs_human",
                    summary={
                        "stopReason": "layout_drift_too_large",
                        "rounds": round_number,
                        "baselinePageCount": baseline_snapshot.page_count,
                        "outputPageCount": snapshot.page_count,
                        "pageCountDrift": page_drift,
                        "lastPatchPlanId": current_plan.id,
                        "lastExecutionId": execution.id,
                        "postprocess": postprocess_summary,
                        "lastRenderSnapshotId": snapshot.id,
                        "renderStatus": snapshot.status,
                    },
                )

            if round_number >= run.max_rounds:
                return _finish_agent_run(
                    db,
                    run,
                    status="needs_human",
                    summary={
                        "stopReason": "max_rounds_reached",
                        "rounds": round_number,
                        "skippedOperations": skipped_count,
                        "lastPatchPlanId": current_plan.id,
                        "lastExecutionId": execution.id,
                        "postprocess": postprocess_summary,
                        "lastRenderSnapshotId": snapshot.id,
                        "renderStatus": snapshot.status,
                    },
                )

            _update_agent_run(db, run, "repairing")
            repair_plan = build_internal_repair_plan(db, current_plan.id)
            if repair_plan is None:
                return _finish_agent_run(
                    db,
                    run,
                    status="needs_human",
                    summary={
                        "stopReason": "no_repairable_operations",
                        "rounds": round_number,
                        "skippedOperations": skipped_count,
                        "lastPatchPlanId": current_plan.id,
                        "lastExecutionId": execution.id,
                        "postprocess": postprocess_summary,
                        "lastRenderSnapshotId": snapshot.id,
                        "renderStatus": snapshot.status,
                    },
                )
            _record_agent_step(
                db,
                run.id,
                round_number + 1,
                "repair_plan",
                "done",
                repair_plan.summary,
                patch_plan_id=repair_plan.id,
            )
            current_plan = repair_plan

        return _finish_agent_run(
            db,
            run,
            status="needs_human",
            summary={"stopReason": "loop_exhausted", "rounds": run.round_count},
        )
    except Exception as exc:
        return _finish_agent_run(
            db,
            run,
            status="failed",
            summary={"stopReason": "exception", "error": str(exc)},
            error_message=str(exc),
        )


def _update_agent_run(
    db: Session,
    run: AgentRun,
    status: str,
    round_count: int | None = None,
) -> None:
    run.status = status
    if round_count is not None:
        run.round_count = round_count
    db.add(run)
    db.commit()
    db.refresh(run)


def _finish_agent_run(
    db: Session,
    run: AgentRun,
    status: str,
    summary: dict,
    error_message: str | None = None,
) -> dict:
    run.status = status
    run.summary = summary
    run.error_message = error_message
    db.add(run)
    db.commit()
    db.refresh(run)
    return {
        "agent_run_id": run.id,
        "status": run.status,
        "round_count": run.round_count,
        "current_output_document_version_id": run.current_output_document_version_id,
        "summary": run.summary,
    }


def _record_agent_step(
    db: Session,
    agent_run_id: str,
    round_number: int,
    step_type: str,
    status: str,
    summary: dict,
    patch_plan_id: str | None = None,
    patch_execution_id: str | None = None,
    render_snapshot_id: str | None = None,
) -> AgentStep:
    step = AgentStep(
        agent_run_id=agent_run_id,
        round_number=round_number,
        step_type=step_type,
        status=status,
        patch_plan_id=patch_plan_id,
        patch_execution_id=patch_execution_id,
        render_snapshot_id=render_snapshot_id,
        summary=summary,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def _skipped_operation_count(db: Session, patch_plan_id: str) -> int:
    return (
        db.query(PatchOperation)
        .filter_by(patch_plan_id=patch_plan_id, status="skipped")
        .count()
    )


def _target_element_count(db: Session, document_version_id: str) -> int:
    return db.query(TargetElement).filter_by(document_version_id=document_version_id).count()


def _mapping_count(db: Session, document_version_id: str) -> int:
    target_ids = select(TargetElement.id).where(
        TargetElement.document_version_id == document_version_id
    )
    return (
        db.query(func.count(func.distinct(MappingResult.target_element_id)))
        .filter(MappingResult.target_element_id.in_(target_ids))
        .scalar()
        or 0
    )


def _page_count_drift(left: int | None, right: int | None) -> int:
    if left is None or right is None:
        return 0
    return abs(left - right)


def _latest_draft_patch_plan(db: Session, document_version_id: str) -> PatchPlan | None:
    return db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == document_version_id)
        .where(PatchPlan.status == "draft")
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )


def _mark_version_failed(db: Session, document_version_id: str, exc: Exception) -> None:
    try:
        version = db.get(DocumentVersion, document_version_id)
        if version is not None and version.status not in ("done",):
            version.status = "failed"
            version.error_message = str(exc)
            db.add(version)
            db.commit()
    except Exception:
        pass
