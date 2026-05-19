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
