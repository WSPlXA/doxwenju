import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.document import (
    Document,
    DocumentVersion,
    FormatAtom,
    FormatProfile,
    OOXMLPart,
    ProfileRule,
)
from app.schemas.template import (
    AtomResponse,
    PageResponse,
    PartResponse,
    ProfileResponse,
    RuleResponse,
    TemplateStatusResponse,
    TemplateUploadResponse,
)
from app.tasks.template_tasks import template_ingestion

router = APIRouter(prefix="/templates", tags=["templates"])


def _current_template_version(db: Session) -> DocumentVersion | None:
    stmt = (
        select(DocumentVersion)
        .join(Document)
        .where(Document.is_current_template.is_(True))
        .order_by(DocumentVersion.version.desc(), DocumentVersion.created_at.desc())
        .limit(1)
    )
    return db.scalar(stmt)


@router.post(
    "/current", response_model=TemplateUploadResponse, status_code=status.HTTP_202_ACCEPTED
)
async def upload_current_template(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filename = file.filename or ""
    if not filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are accepted")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(data) > settings.max_docx_bytes:
        raise HTTPException(status_code=413, detail="DOCX exceeds configured size limit")

    sha256 = hashlib.sha256(data).hexdigest()

    existing_version = db.scalar(
        select(DocumentVersion)
        .join(Document)
        .where(Document.kind == "template", DocumentVersion.sha256 == sha256)
        .order_by(DocumentVersion.created_at.desc())
        .limit(1)
    )
    if existing_version:
        db.query(Document).update({Document.is_current_template: False})
        existing_version.document.is_current_template = True
        existing_version.status = "queued"
        existing_version.progress = 0
        existing_version.error_message = None
        db.commit()
        task = template_ingestion.delay(existing_version.id)
        return TemplateUploadResponse(
            document_id=existing_version.document_id,
            version_id=existing_version.id,
            task_id=task.id,
            status=existing_version.status,
        )

    db.query(Document).update({Document.is_current_template: False})
    document = Document(kind="template", name=filename, sha256=sha256, is_current_template=True)
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

    task = template_ingestion.delay(version.id)
    return TemplateUploadResponse(
        document_id=document.id, version_id=version.id, task_id=task.id, status=version.status
    )


@router.get("/current/status", response_model=TemplateStatusResponse)
def current_template_status(db: Session = Depends(get_db)):
    version = _current_template_version(db)
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


@router.get("/current/parts", response_model=PageResponse)
def current_template_parts(
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _current_template_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No current template")
    base = select(OOXMLPart).where(OOXMLPart.document_version_id == version.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    parts = db.scalars(base.order_by(OOXMLPart.part_name).limit(limit).offset(offset)).all()
    items = [
        PartResponse(
            id=p.id,
            part_name=p.part_name,
            content_type=p.content_type,
            is_xml=p.is_xml,
            size_bytes=p.size_bytes,
            sha256=p.sha256,
            parsed_summary=p.parsed_summary,
        )
        for p in parts
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)


@router.get("/current/atoms", response_model=PageResponse)
def current_template_atoms(
    atom_type: str | None = None,
    style_id: str | None = None,
    part_name: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _current_template_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No current template")

    base = select(FormatAtom).where(FormatAtom.document_version_id == version.id)
    if atom_type:
        base = base.where(FormatAtom.atom_type == atom_type)
    if style_id:
        base = base.where(FormatAtom.style_id == style_id)
    if part_name:
        base = base.where(FormatAtom.part_name == part_name)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    atoms = db.scalars(
        base.order_by(FormatAtom.created_at, FormatAtom.id).limit(limit).offset(offset)
    ).all()
    items = [
        AtomResponse(
            id=a.id,
            atom_type=a.atom_type,
            part_name=a.part_name,
            xml_path=a.xml_path,
            normalized=a.normalized,
            text_summary=a.text_summary,
            element_category=a.element_category,
            style_id=a.style_id,
            numbering_id=a.numbering_id,
            relationship_id=a.relationship_id,
        )
        for a in atoms
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)


@router.get("/current/profile", response_model=ProfileResponse)
def current_template_profile(db: Session = Depends(get_db)):
    version = _current_template_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No current template")
    profile = db.scalar(
        select(FormatProfile)
        .where(FormatProfile.document_version_id == version.id)
        .order_by(FormatProfile.created_at.desc())
        .limit(1)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="No profile for current template")
    return ProfileResponse(
        id=profile.id,
        document_version_id=profile.document_version_id,
        name=profile.name,
        version=profile.version,
        status=profile.status,
        source=profile.source,
        summary=profile.summary,
    )


@router.get("/current/rules", response_model=PageResponse)
def current_template_rules(
    rule_type: str | None = None,
    element_category: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    version = _current_template_version(db)
    if version is None:
        raise HTTPException(status_code=404, detail="No current template")

    base = select(ProfileRule).where(ProfileRule.document_version_id == version.id)
    if rule_type:
        base = base.where(ProfileRule.rule_type == rule_type)
    if element_category:
        base = base.where(ProfileRule.element_category == element_category)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rules = db.scalars(
        base.order_by(ProfileRule.priority, ProfileRule.created_at, ProfileRule.id)
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        RuleResponse(
            id=rule.id,
            rule_type=rule.rule_type,
            element_category=rule.element_category,
            name=rule.name,
            priority=rule.priority,
            selector=rule.selector,
            properties=rule.properties,
            source_atom_ids=rule.source_atom_ids,
            confidence=rule.confidence,
        )
        for rule in rules
    ]
    return PageResponse(items=items, limit=limit, offset=offset, total=total)
