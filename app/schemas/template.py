from datetime import datetime

from pydantic import BaseModel


class TemplateUploadResponse(BaseModel):
    document_id: str
    version_id: str
    task_id: str
    status: str


class TemplateStatusResponse(BaseModel):
    document_id: str | None
    version_id: str | None
    status: str
    progress: int
    error_message: str | None = None
    updated_at: datetime | None = None


class PartResponse(BaseModel):
    id: str
    part_name: str
    content_type: str | None
    is_xml: bool
    size_bytes: int
    sha256: str
    parsed_summary: dict | None


class AtomResponse(BaseModel):
    id: str
    atom_type: str
    part_name: str
    xml_path: str | None
    normalized: dict
    text_summary: str | None
    element_category: str | None
    style_id: str | None
    numbering_id: str | None
    relationship_id: str | None


class PageResponse(BaseModel):
    items: list
    limit: int
    offset: int
    total: int
