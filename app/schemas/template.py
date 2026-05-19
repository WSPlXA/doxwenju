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


class ProfileResponse(BaseModel):
    id: str
    document_version_id: str
    name: str
    version: int
    status: str
    source: str
    summary: dict


class RuleResponse(BaseModel):
    id: str
    rule_type: str
    element_category: str
    name: str
    priority: int
    selector: dict
    properties: dict
    source_atom_ids: list[str]
    confidence: int


class TargetUploadResponse(BaseModel):
    document_id: str
    version_id: str
    task_id: str
    status: str


class TargetElementResponse(BaseModel):
    id: str
    element_type: str
    element_category: str | None
    part_name: str
    xml_path: str | None
    text_summary: str | None
    style_id: str | None
    numbering_id: str | None
    normalized: dict
    classification: dict | None


class MappingResponse(BaseModel):
    id: str
    target_element_id: str
    profile_rule_id: str | None
    score: int
    strategy: str
    rationale: dict


class MappingCandidateResponse(BaseModel):
    id: str
    target_element_id: str
    profile_rule_id: str
    rank: int
    score: int
    strategy: str
    rationale: dict


class PatchPlanResponse(BaseModel):
    id: str
    document_version_id: str
    template_document_version_id: str
    round_number: int
    status: str
    source: str
    summary: dict


class PatchOperationResponse(BaseModel):
    id: str
    patch_plan_id: str
    target_element_id: str
    mapping_result_id: str | None
    profile_rule_id: str | None
    operation_type: str
    part_name: str
    xml_path: str | None
    selector: dict
    payload: dict
    risk_level: str
    status: str
    rationale: dict


class PatchExecuteResponse(BaseModel):
    patch_plan_id: str
    task_id: str
    status: str


class PatchExecutionResponse(BaseModel):
    id: str
    patch_plan_id: str
    document_version_id: str
    output_document_version_id: str | None
    status: str
    summary: dict
    error_message: str | None


class RenderPrecheckResponse(BaseModel):
    document_version_id: str
    task_id: str
    status: str


class RenderSnapshotResponse(BaseModel):
    id: str
    document_version_id: str
    renderer: str
    renderer_version: str | None
    status: str
    pdf_size_bytes: int | None
    page_count: int | None
    metrics: dict | None
    error_message: str | None


class PageResponse(BaseModel):
    items: list
    limit: int
    offset: int
    total: int
