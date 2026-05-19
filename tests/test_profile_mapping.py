import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
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


def test_mapping_uses_rerank_when_available(monkeypatch):
    db = _sqlite_session()
    original_key = settings.gemini_api_key
    original_enabled = settings.gemini_rerank_enabled
    original_limit = settings.gemini_rerank_max_elements_per_run
    try:
        raw = build_minimal_docx()
        template = _version(db, "template", raw)
        target = _version(db, "target", raw)
        ingest_template_version(db, template)
        ingest_template_version(db, target)

        settings.gemini_api_key = "test-key"
        settings.gemini_rerank_enabled = True
        settings.gemini_rerank_max_elements_per_run = 1

        def fake_rerank(db, document_version_id, target, candidates):
            return candidates[-1], {
                "provider": "gemini",
                "model": "fake",
                "selectedProfileRuleId": candidates[-1]["rule"].id,
                "confidence": 88,
                "rationale": "mocked choice",
                "riskFlags": [],
            }

        monkeypatch.setattr("app.services.target_mapping.maybe_rerank_candidates", fake_rerank)
        mappings = rebuild_mapping_results(db, target.id, template.id)

        assert mappings[0].strategy == "gemini_rerank_v0"
        assert mappings[0].rationale["rerank"]["confidence"] == 88
        assert any(mapping.strategy == "hybrid_v0" for mapping in mappings[1:])
    finally:
        settings.gemini_api_key = original_key
        settings.gemini_rerank_enabled = original_enabled
        settings.gemini_rerank_max_elements_per_run = original_limit
        db.close()
