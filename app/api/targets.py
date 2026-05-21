import hashlib
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.document import (
    AgentRun,
    AgentStep,
    Document,
    DocumentVersion,
    MappingCandidate,
    MappingResult,
    PatchExecution,
    PatchOperation,
    PatchPlan,
    RenderSnapshot,
    TargetElement,
)
from app.schemas.template import (
    AgentRunResponse,
    AgentRunStartResponse,
    AgentStepResponse,
    AutoRepairResponse,
    MappingCandidateResponse,
    MappingResponse,
    PageResponse,
    PatchExecuteResponse,
    PatchExecutionResponse,
    PatchOperationResponse,
    PatchPlanResponse,
    RenderPrecheckResponse,
    RenderSnapshotResponse,
    TargetElementResponse,
    TargetUploadResponse,
    TemplateStatusResponse,
)
from app.tasks.template_tasks import (
    auto_repair_cycle,
    format_agent_run,
    patch_plan_execute,
    render_precheck,
    target_ingestion,
)

router = APIRouter(prefix="/targets", tags=["targets"])


def _latest_target_version(db: Session) -> DocumentVersion | None:
    return db.scalar(
        select(DocumentVersion)
        .join(Document)
        .where(Document.kind == "target")
        .order_by(DocumentVersion.created_at.desc())
        .limit(1)
    )


def _current_template_version(db: Session) -> DocumentVersion | None:
    return db.scalar(
        select(DocumentVersion)
        .join(Document)
        .where(Document.is_current_template.is_(True))
        .order_by(DocumentVersion.version.desc(), DocumentVersion.created_at.desc())
        .limit(1)
    )


@router.post("", response_model=TargetUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_target(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filename = file.filename or ""
    if not filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are accepted")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(data) > settings.max_docx_bytes:
        raise HTTPException(status_code=413, detail="DOCX exceeds configured size limit")

    sha256 = hashlib.sha256(data).hexdigest()
    document = Document(kind="target", name=filename, sha256=sha256, is_current_template=False)
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id,
        version=1,
        filename=filename,
        content_type=file.content_type
        or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(data),
        sha256=sha256,
        raw_file=data,
        status="queued",
        progress=0,
    )
    db.add(version)
    db.commit()

    task = target_ingestion.delay(version.id)
    return TargetUploadResponse(
        document_id=document.id,
        version_id=version.id,
        task_id=task.id,
        status=version.status,
    )


@router.get("/latest/status", response_model=TemplateStatusResponse)
def latest_target_status(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        return TemplateStatusResponse(
            document_id=None, version_id=None, status="missing", progress=0
        )
    return TemplateStatusResponse(
        document_id=version.document_id,
        version_id=version.id,
        status=version.status,
        progress=version.progress,
        error_message=version.error_message,
        updated_at=version.updated_at,
    )


@router.get("/latest/elements", response_model=PageResponse)
def latest_target_elements(
    element_type: str | None = None,
    element_category: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")

    base = select(TargetElement).where(TargetElement.document_version_id == version.id)
    if element_type:
        base = base.where(TargetElement.element_type == element_type)
    if element_category:
        base = base.where(TargetElement.element_category == element_category)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    elements = db.scalars(
        base.order_by(TargetElement.created_at, TargetElement.id).limit(limit).offset(offset)
    ).all()
    items = [
        TargetElementResponse(
            id=element.id,
            element_type=element.element_type,
            element_category=element.element_category,
            part_name=element.part_name,
            xml_path=element.xml_path,
            text_summary=element.text_summary,
            style_id=element.style_id,
            numbering_id=element.numbering_id,
            normalized=element.normalized,
            classification=element.classification,
        )
        for element in elements
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)


@router.post("/latest/patch-plan/execute", response_model=PatchExecuteResponse)
def execute_latest_target_patch_plan(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="No patch plan for latest target")
    task = patch_plan_execute.delay(plan.id)
    return PatchExecuteResponse(patch_plan_id=plan.id, task_id=task.id, status="queued")


@router.post("/latest/auto-repair", response_model=AutoRepairResponse)
def run_latest_target_auto_repair(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .where(PatchPlan.output_document_version_id.is_not(None))
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="No executed patch plan for latest target")
    task = auto_repair_cycle.delay(plan.id)
    return AutoRepairResponse(source_patch_plan_id=plan.id, task_id=task.id, status="queued")


@router.post("/latest/agent-run", response_model=AgentRunStartResponse)
def start_latest_target_agent_run(
    max_rounds: int = Query(default=3, ge=1, le=10),
    db: Session = Depends(get_db),
):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    template = _current_template_version(db)
    if template is None:
        raise HTTPException(status_code=404, detail="No current template")

    run = AgentRun(
        target_document_version_id=version.id,
        template_document_version_id=template.id,
        status="queued",
        max_rounds=max_rounds,
        summary={
            "targetDocumentVersionId": version.id,
            "templateDocumentVersionId": template.id,
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    task = format_agent_run.delay(run.id)
    return AgentRunStartResponse(
        agent_run_id=run.id,
        task_id=task.id,
        status=run.status,
        max_rounds=run.max_rounds,
    )


@router.get("/latest/agent-run/status", response_model=AgentRunResponse)
def latest_target_agent_run_status(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    run = db.scalar(
        select(AgentRun)
        .where(AgentRun.target_document_version_id == version.id)
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="No agent run for latest target")
    steps = list(
        db.scalars(
            select(AgentStep)
            .where(AgentStep.agent_run_id == run.id)
            .order_by(AgentStep.created_at, AgentStep.id)
        )
    )
    return _agent_run_response(run, steps)


@router.get("/latest/patch-plan/execution", response_model=PatchExecutionResponse)
def latest_target_patch_execution(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="No patch plan for latest target")
    execution = db.scalar(
        select(PatchExecution)
        .where(PatchExecution.patch_plan_id == plan.id)
        .order_by(PatchExecution.created_at.desc())
        .limit(1)
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="No patch execution for latest target")
    return PatchExecutionResponse(
        id=execution.id,
        patch_plan_id=execution.patch_plan_id,
        document_version_id=execution.document_version_id,
        output_document_version_id=execution.output_document_version_id,
        status=execution.status,
        summary=execution.summary,
        error_message=execution.error_message,
    )


@router.get("/latest/output.docx")
def download_latest_target_output(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None or plan.output_document_version_id is None:
        raise HTTPException(status_code=404, detail="No patched output for latest target")
    output = db.get(DocumentVersion, plan.output_document_version_id)
    if output is None:
        raise HTTPException(status_code=404, detail="Output document version is missing")
    return Response(
        content=output.raw_file,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": _attachment_disposition(output.filename)},
    )


@router.post("/latest/render-precheck", response_model=RenderPrecheckResponse)
def run_latest_target_render_precheck(db: Session = Depends(get_db)):
    output = _latest_output_version(db)
    if output is None:
        raise HTTPException(status_code=404, detail="No patched output for latest target")
    task = render_precheck.delay(output.id)
    return RenderPrecheckResponse(document_version_id=output.id, task_id=task.id, status="queued")


@router.get("/latest/render-snapshot", response_model=RenderSnapshotResponse)
def latest_target_render_snapshot(db: Session = Depends(get_db)):
    output = _latest_output_version(db)
    if output is None:
        raise HTTPException(status_code=404, detail="No patched output for latest target")
    snapshot = db.scalar(
        select(RenderSnapshot)
        .where(RenderSnapshot.document_version_id == output.id)
        .order_by(RenderSnapshot.created_at.desc())
        .limit(1)
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No render snapshot for latest output")
    return _render_snapshot_response(snapshot)


@router.get("/latest/render.pdf")
def download_latest_target_render_pdf(db: Session = Depends(get_db)):
    output = _latest_output_version(db)
    if output is None:
        raise HTTPException(status_code=404, detail="No patched output for latest target")
    snapshot = db.scalar(
        select(RenderSnapshot)
        .where(RenderSnapshot.document_version_id == output.id)
        .order_by(RenderSnapshot.created_at.desc())
        .limit(1)
    )
    if snapshot is None or snapshot.pdf_data is None:
        raise HTTPException(status_code=404, detail="No rendered PDF for latest output")
    return Response(
        content=snapshot.pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": _attachment_disposition("render.pdf")},
    )


@router.get("/latest/patch-plan", response_model=PatchPlanResponse)
def latest_target_patch_plan(db: Session = Depends(get_db)):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="No patch plan for latest target")
    return PatchPlanResponse(
        id=plan.id,
        document_version_id=plan.document_version_id,
        template_document_version_id=plan.template_document_version_id,
        round_number=plan.round_number,
        status=plan.status,
        source=plan.source,
        summary=plan.summary,
    )


@router.get("/latest/patch-plan/operations", response_model=PageResponse)
def latest_target_patch_operations(
    operation_type: str | None = None,
    risk_level: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="No patch plan for latest target")

    base = select(PatchOperation).where(PatchOperation.patch_plan_id == plan.id)
    if operation_type:
        base = base.where(PatchOperation.operation_type == operation_type)
    if risk_level:
        base = base.where(PatchOperation.risk_level == risk_level)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    operations = db.scalars(
        base.order_by(PatchOperation.created_at, PatchOperation.id).limit(limit).offset(offset)
    ).all()
    items = [
        PatchOperationResponse(
            id=operation.id,
            patch_plan_id=operation.patch_plan_id,
            target_element_id=operation.target_element_id,
            mapping_result_id=operation.mapping_result_id,
            profile_rule_id=operation.profile_rule_id,
            operation_type=operation.operation_type,
            part_name=operation.part_name,
            xml_path=operation.xml_path,
            selector=operation.selector,
            payload=operation.payload,
            risk_level=operation.risk_level,
            status=operation.status,
            rationale=operation.rationale,
        )
        for operation in operations
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)


def _latest_output_version(db: Session) -> DocumentVersion | None:
    version = _latest_target_version(db)
    if version is None:
        return None
    plan = db.scalar(
        select(PatchPlan)
        .where(PatchPlan.document_version_id == version.id)
        .where(PatchPlan.output_document_version_id.is_not(None))
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )
    if plan is None or plan.output_document_version_id is None:
        return None
    return db.get(DocumentVersion, plan.output_document_version_id)


def _render_snapshot_response(snapshot: RenderSnapshot) -> RenderSnapshotResponse:
    return RenderSnapshotResponse(
        id=snapshot.id,
        document_version_id=snapshot.document_version_id,
        renderer=snapshot.renderer,
        renderer_version=snapshot.renderer_version,
        status=snapshot.status,
        pdf_size_bytes=snapshot.pdf_size_bytes,
        page_count=snapshot.page_count,
        metrics=snapshot.metrics,
        error_message=snapshot.error_message,
    )


def _attachment_disposition(filename: str) -> str:
    fallback = "".join(char if ord(char) < 128 and char not in {'"', "\\"} else "_" for char in filename)
    fallback = fallback or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _agent_run_response(run: AgentRun, steps: list[AgentStep]) -> AgentRunResponse:
    return AgentRunResponse(
        id=run.id,
        target_document_version_id=run.target_document_version_id,
        template_document_version_id=run.template_document_version_id,
        current_output_document_version_id=run.current_output_document_version_id,
        status=run.status,
        round_count=run.round_count,
        max_rounds=run.max_rounds,
        summary=run.summary,
        error_message=run.error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
        steps=[
            AgentStepResponse(
                id=step.id,
                round_number=step.round_number,
                step_type=step.step_type,
                status=step.status,
                patch_plan_id=step.patch_plan_id,
                patch_execution_id=step.patch_execution_id,
                render_snapshot_id=step.render_snapshot_id,
                summary=step.summary,
                error_message=step.error_message,
                created_at=step.created_at,
            )
            for step in steps
        ],
    )


@router.get("/latest/mappings/{mapping_id}/candidates", response_model=PageResponse)
def latest_target_mapping_candidates(
    mapping_id: str,
    limit: int = Query(default=10, le=50, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")
    element_ids = select(TargetElement.id).where(TargetElement.document_version_id == version.id)
    mapping = db.scalar(
        select(MappingResult)
        .where(MappingResult.id == mapping_id)
        .where(MappingResult.target_element_id.in_(element_ids))
        .limit(1)
    )
    if mapping is None:
        raise HTTPException(status_code=404, detail="Mapping not found for latest target")

    base = select(MappingCandidate).where(
        MappingCandidate.target_element_id == mapping.target_element_id
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    candidates = db.scalars(base.order_by(MappingCandidate.rank).limit(limit).offset(offset)).all()
    items = [
        MappingCandidateResponse(
            id=candidate.id,
            target_element_id=candidate.target_element_id,
            profile_rule_id=candidate.profile_rule_id,
            rank=candidate.rank,
            score=candidate.score,
            strategy=candidate.strategy,
            rationale=candidate.rationale,
        )
        for candidate in candidates
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)


@router.get("/latest/mappings", response_model=PageResponse)
def latest_target_mappings(
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _latest_target_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No target document")

    element_ids = select(TargetElement.id).where(TargetElement.document_version_id == version.id)
    base = select(MappingResult).where(MappingResult.target_element_id.in_(element_ids))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    mappings = db.scalars(
        base.order_by(MappingResult.created_at, MappingResult.id).limit(limit).offset(offset)
    ).all()
    items = [
        MappingResponse(
            id=mapping.id,
            target_element_id=mapping.target_element_id,
            profile_rule_id=mapping.profile_rule_id,
            score=mapping.score,
            strategy=mapping.strategy,
            rationale=mapping.rationale,
        )
        for mapping in mappings
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)
