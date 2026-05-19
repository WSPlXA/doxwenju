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
    PatchOperation,
    PatchPlan,
    ProfileRule,
    RenderSnapshot,
    TargetElement,
)
from app.services.docx_package import inspect_docx_package
from app.services.ingestion import ingest_template_version
from app.services.patch_engine import execute_patch_plan
from app.services.patch_planner import rebuild_patch_plan
from app.services.rendering import render_libreoffice_precheck
from app.services.repair_planner import build_internal_repair_plan
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


def test_patch_plan_is_generated_from_mapping_results():
    db = _sqlite_session()
    try:
        raw = build_minimal_docx()
        template = _version(db, "template", raw)
        target = _version(db, "target", raw)
        ingest_template_version(db, template)
        ingest_template_version(db, target)
        rebuild_mapping_results(db, target.id, template.id)
        plan = rebuild_patch_plan(db, target.id, template.id)

        operations = db.query(PatchOperation).filter_by(patch_plan_id=plan.id).all()
        stored_plan = db.query(PatchPlan).filter_by(id=plan.id).one()

        assert stored_plan.status == "draft"
        assert stored_plan.summary["operationCount"] == len(operations)
        assert operations
        assert all(operation.status == "planned" for operation in operations)
        assert {operation.risk_level for operation in operations}
        assert any(operation.operation_type == "apply_heading_rule" for operation in operations)
    finally:
        db.close()


def test_patch_engine_applies_supported_operations_and_outputs_docx():
    db = _sqlite_session()
    try:
        raw = build_minimal_docx()
        template = _version(db, "template", raw)
        target = _version(db, "target", raw)
        ingest_template_version(db, template)
        ingest_template_version(db, target)
        rebuild_mapping_results(db, target.id, template.id)
        plan = rebuild_patch_plan(db, target.id, template.id)
        execution = execute_patch_plan(db, plan.id)

        db.refresh(plan)
        output = db.get(DocumentVersion, execution.output_document_version_id)
        operations = db.query(PatchOperation).filter_by(patch_plan_id=plan.id).all()
        statuses = {operation.status for operation in operations}

        assert execution.status == "done"
        assert plan.status == "applied"
        assert output is not None
        assert inspect_docx_package(output.raw_file)
        assert "applied" in statuses
        assert "skipped" in statuses
        assert (
            db.query(PatchOperation)
            .filter_by(patch_plan_id=plan.id, operation_type="apply_table_rule", status="applied")
            .count()
            >= 1
        )
        assert (
            db.query(PatchOperation)
            .filter_by(
                patch_plan_id=plan.id,
                operation_type="apply_footnote_rule",
                status="applied",
            )
            .count()
            >= 1
        )
        assert (
            db.query(PatchOperation)
            .filter_by(
                patch_plan_id=plan.id,
                operation_type="apply_header_footer_rule",
                status="applied",
            )
            .count()
            >= 1
        )
        assert execution.summary["outputOpenabilityCheck"] == "zip_and_required_parts_valid"
    finally:
        db.close()


def test_internal_repair_plan_retries_supported_skipped_operations():
    db = _sqlite_session()
    try:
        raw = build_minimal_docx()
        template = _version(db, "template", raw)
        target = _version(db, "target", raw)
        ingest_template_version(db, template)
        ingest_template_version(db, target)
        rebuild_mapping_results(db, target.id, template.id)
        plan = rebuild_patch_plan(db, target.id, template.id)
        execution = execute_patch_plan(db, plan.id)

        operation = (
            db.query(PatchOperation)
            .filter_by(
                patch_plan_id=plan.id,
                operation_type="apply_header_footer_rule",
                status="applied",
            )
            .first()
        )
        assert operation is not None
        operation.status = "skipped"
        operation.rationale = {
            **operation.rationale,
            "skipReason": "unsupported_in_patch_engine_v0",
        }
        db.add(operation)
        db.commit()

        repair_plan = build_internal_repair_plan(db, plan.id)

        assert repair_plan is not None
        assert repair_plan.round_number == 2
        assert repair_plan.source == "internal_repair_v0"
        assert repair_plan.document_version_id == execution.output_document_version_id
        repair_execution = execute_patch_plan(db, repair_plan.id)
        assert repair_execution.status == "done"
        assert (
            db.query(PatchOperation)
            .filter_by(
                patch_plan_id=repair_plan.id,
                operation_type="apply_header_footer_rule",
                status="applied",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_render_precheck_records_skipped_when_libreoffice_missing():
    db = _sqlite_session()
    original_path = settings.libreoffice_path
    try:
        settings.libreoffice_path = "/definitely/missing/soffice"
        raw = build_minimal_docx()
        target = _version(db, "output", raw)
        snapshot = render_libreoffice_precheck(db, target.id)
        stored = db.query(RenderSnapshot).filter_by(id=snapshot.id).one()

        assert stored.status == "skipped"
        assert stored.renderer == "libreoffice"
        assert stored.pdf_data is None
        assert "not found" in stored.error_message.lower()
    finally:
        settings.libreoffice_path = original_path
        db.close()
