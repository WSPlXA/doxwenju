import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import (
    Document,
    DocumentVersion,
    FormatProfile,
    MappingCandidate,
    MappingResult,
    ProfileRule,
    TargetElement,
)
from app.services.ingestion import ingest_template_version
from app.services.target_mapping import rebuild_mapping_results
from tests.fixtures.docx_builder import build_minimal_docx


def _sqlite_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _version(db, kind: str, raw: bytes) -> DocumentVersion:
    sha = hashlib.sha256(raw).hexdigest()
    document = Document(
        kind=kind,
        name=f"{kind}.docx",
        sha256=sha,
        is_current_template=kind == "template",
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id,
        version=1,
        filename=f"{kind}.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(raw),
        sha256=sha,
        raw_file=raw,
        status="queued",
    )
    db.add(version)
    db.commit()
    return version


def test_template_ingestion_builds_profile_rules():
    db = _sqlite_session()
    try:
        version = _version(db, "template", build_minimal_docx())
        ingest_template_version(db, version)

        profile = db.query(FormatProfile).filter_by(document_version_id=version.id).one()
        rules = db.query(ProfileRule).filter_by(document_version_id=version.id).all()
        categories = {rule.element_category for rule in rules}

        assert profile.summary["atomCount"] > 0
        assert {
            "document_setup",
            "heading",
            "paragraph",
            "list",
            "table",
            "image",
            "footnote",
        }.issubset(categories)
    finally:
        db.close()


def test_target_ingestion_builds_elements_and_mapping_results():
    db = _sqlite_session()
    try:
        raw = build_minimal_docx()
        template = _version(db, "template", raw)
        target = _version(db, "target", raw)
        ingest_template_version(db, template)
        ingest_template_version(db, target)
        mappings = rebuild_mapping_results(db, target.id, template.id)

        elements = db.query(TargetElement).filter_by(document_version_id=target.id).all()
        stored_mappings = db.query(MappingResult).all()
        stored_candidates = db.query(MappingCandidate).all()

        assert elements
        assert len(mappings) == len(elements)
        assert len(stored_mappings) == len(elements)
        assert stored_candidates
        assert max(mapping.score for mapping in mappings) > 0
    finally:
        db.close()
