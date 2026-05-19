import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base


def new_id() -> str:
    return str(uuid.uuid4())


JsonType = JSON().with_variant(JSONB, "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_current_template: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base, TimestampMixin):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_file: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="versions")
    parts: Mapped[list["OOXMLPart"]] = relationship(cascade="all, delete-orphan")
    relationships: Mapped[list["Relationship"]] = relationship(cascade="all, delete-orphan")
    media_assets: Mapped[list["MediaAsset"]] = relationship(cascade="all, delete-orphan")
    format_atoms: Mapped[list["FormatAtom"]] = relationship(cascade="all, delete-orphan")
    target_elements: Mapped[list["TargetElement"]] = relationship(cascade="all, delete-orphan")


class OOXMLPart(Base, TimestampMixin):
    __tablename__ = "ooxml_parts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    part_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    content_type: Mapped[str | None] = mapped_column(String(255))
    is_xml: Mapped[bool] = mapped_column(Boolean, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    xml_text: Mapped[str | None] = mapped_column(Text)
    binary_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    parsed_summary: Mapped[dict | None] = mapped_column(JsonType)


class Relationship(Base, TimestampMixin):
    __tablename__ = "relationships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    source_part: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    relationship_id: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(512), nullable=False)
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    target_mode: Mapped[str | None] = mapped_column(String(64))
    resolved_target: Mapped[str | None] = mapped_column(String(512), index=True)


class MediaAsset(Base, TimestampMixin):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    part_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class FormatAtom(Base, TimestampMixin):
    __tablename__ = "format_atoms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    atom_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    part_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    xml_path: Mapped[str | None] = mapped_column(String(1024))
    raw_xml: Mapped[str | None] = mapped_column(Text)
    normalized: Mapped[dict] = mapped_column(JsonType, nullable=False)
    text_summary: Mapped[str | None] = mapped_column(Text)
    element_category: Mapped[str | None] = mapped_column(String(128), index=True)
    page_context: Mapped[dict | None] = mapped_column(JsonType)
    style_id: Mapped[str | None] = mapped_column(String(255), index=True)
    numbering_id: Mapped[str | None] = mapped_column(String(255), index=True)
    relationship_id: Mapped[str | None] = mapped_column(String(255))
    render_metrics: Mapped[dict | None] = mapped_column(JsonType)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))


class ProviderCall(Base, TimestampMixin):
    __tablename__ = "provider_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_versions.id"))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)


class FormatProfile(Base, TimestampMixin):
    __tablename__ = "format_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    summary: Mapped[dict] = mapped_column(JsonType, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="deterministic_v0")

    rules: Mapped[list["ProfileRule"]] = relationship(cascade="all, delete-orphan")


class ProfileRule(Base, TimestampMixin):
    __tablename__ = "profile_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(ForeignKey("format_profiles.id"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    element_category: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    selector: Mapped[dict] = mapped_column(JsonType, nullable=False)
    properties: Mapped[dict] = mapped_column(JsonType, nullable=False)
    source_atom_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class TargetElement(Base, TimestampMixin):
    __tablename__ = "target_elements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    source_atom_id: Mapped[str | None] = mapped_column(ForeignKey("format_atoms.id"))
    element_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    element_category: Mapped[str | None] = mapped_column(String(128), index=True)
    part_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    xml_path: Mapped[str | None] = mapped_column(String(1024))
    text_summary: Mapped[str | None] = mapped_column(Text)
    style_id: Mapped[str | None] = mapped_column(String(255), index=True)
    numbering_id: Mapped[str | None] = mapped_column(String(255), index=True)
    normalized: Mapped[dict] = mapped_column(JsonType, nullable=False)
    classification: Mapped[dict | None] = mapped_column(JsonType)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))


class MappingResult(Base, TimestampMixin):
    __tablename__ = "mapping_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_element_id: Mapped[str] = mapped_column(
        ForeignKey("target_elements.id"), nullable=False, index=True
    )
    profile_rule_id: Mapped[str | None] = mapped_column(ForeignKey("profile_rules.id"))
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[dict] = mapped_column(JsonType, nullable=False)


class MappingCandidate(Base, TimestampMixin):
    __tablename__ = "mapping_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_element_id: Mapped[str] = mapped_column(
        ForeignKey("target_elements.id"), nullable=False, index=True
    )
    profile_rule_id: Mapped[str] = mapped_column(ForeignKey("profile_rules.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[dict] = mapped_column(JsonType, nullable=False)


class PatchPlan(Base, TimestampMixin):
    __tablename__ = "patch_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    template_document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="deterministic_v0")
    summary: Mapped[dict] = mapped_column(JsonType, nullable=False)
    output_document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_versions.id"), nullable=True
    )

    operations: Mapped[list["PatchOperation"]] = relationship(cascade="all, delete-orphan")


class PatchOperation(Base, TimestampMixin):
    __tablename__ = "patch_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patch_plan_id: Mapped[str] = mapped_column(ForeignKey("patch_plans.id"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    target_element_id: Mapped[str] = mapped_column(ForeignKey("target_elements.id"), nullable=False)
    mapping_result_id: Mapped[str | None] = mapped_column(ForeignKey("mapping_results.id"))
    profile_rule_id: Mapped[str | None] = mapped_column(ForeignKey("profile_rules.id"))
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    part_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    xml_path: Mapped[str | None] = mapped_column(String(1024))
    selector: Mapped[dict] = mapped_column(JsonType, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="P2", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned", index=True)
    rationale: Mapped[dict] = mapped_column(JsonType, nullable=False)


class PatchExecution(Base, TimestampMixin):
    __tablename__ = "patch_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patch_plan_id: Mapped[str] = mapped_column(
        ForeignKey("patch_plans.id"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    output_document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    summary: Mapped[dict] = mapped_column(JsonType, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class RenderSnapshot(Base, TimestampMixin):
    __tablename__ = "render_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    renderer: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    renderer_version: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    pdf_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    pdf_size_bytes: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict | None] = mapped_column(JsonType)
    error_message: Mapped[str | None] = mapped_column(Text)
