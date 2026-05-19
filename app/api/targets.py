import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.document import (
    Document,
    DocumentVersion,
    MappingCandidate,
    MappingResult,
    TargetElement,
)
from app.schemas.template import (
    MappingCandidateResponse,
    MappingResponse,
    PageResponse,
    TargetElementResponse,
    TargetUploadResponse,
    TemplateStatusResponse,
)
from app.tasks.template_tasks import target_ingestion

router = APIRouter(prefix="/targets", tags=["targets"])


def _latest_target_version(db: Session) -> DocumentVersion | None:
    return db.scalar(
        select(DocumentVersion)
        .join(Document)
        .where(Document.kind == "target")
        .order_by(DocumentVersion.created_at.desc())
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
