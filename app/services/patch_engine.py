import hashlib
import re
import zipfile
from collections import Counter
from io import BytesIO
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentVersion,
    PatchExecution,
    PatchOperation,
    PatchPlan,
)
from app.services.docx_package import inspect_docx_package
from app.services.ooxml import NS, parse_xml, qn

ET.register_namespace("w", NS["w"])
ET.register_namespace("r", NS["r"])
ET.register_namespace("wp", NS["wp"])
ET.register_namespace("a", NS["a"])

SUPPORTED_OPERATIONS = {
    "apply_heading_rule",
    "apply_list_rule",
    "apply_caption_rule",
    "apply_citation_rule",
    "apply_paragraph_rule",
}


def execute_patch_plan(db: Session, patch_plan_id: str) -> PatchExecution:
    plan = db.get(PatchPlan, patch_plan_id)
    if plan is None:
        raise ValueError(f"PatchPlan not found: {patch_plan_id}")
    source_version = db.get(DocumentVersion, plan.document_version_id)
    if source_version is None:
        raise ValueError(f"DocumentVersion not found: {plan.document_version_id}")

    execution = PatchExecution(
        patch_plan_id=plan.id,
        document_version_id=source_version.id,
        status="running",
        summary={},
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    try:
        raw_output, summary = _apply_plan_to_docx(db, source_version.raw_file, plan)
        output_version = _store_output_version(db, source_version, raw_output)
        plan.status = "applied"
        plan.output_document_version_id = output_version.id
        plan.summary = {**(plan.summary or {}), "execution": summary}
        execution.status = "done"
        execution.output_document_version_id = output_version.id
        execution.summary = summary
        db.add_all([plan, execution])
        db.commit()
        db.refresh(execution)
        return execution
    except Exception as exc:
        plan.status = "failed"
        execution.status = "failed"
        execution.error_message = str(exc)
        execution.summary = {"error": str(exc)}
        db.add_all([plan, execution])
        db.commit()
        raise


def _apply_plan_to_docx(db: Session, raw_file: bytes, plan: PatchPlan) -> tuple[bytes, dict]:
    parts = {part.name: part.data for part in inspect_docx_package(raw_file)}
    document_xml = parts.get("word/document.xml")
    if document_xml is None:
        raise ValueError("DOCX is missing word/document.xml")
    root = parse_xml(document_xml)
    paragraphs = root.findall("./w:body/w:p", NS)

    operations = list(
        db.scalars(
            select(PatchOperation)
            .where(PatchOperation.patch_plan_id == plan.id)
            .order_by(PatchOperation.created_at, PatchOperation.id)
        )
    )
    counts = Counter()
    for operation in operations:
        if operation.operation_type not in SUPPORTED_OPERATIONS:
            operation.status = "skipped"
            operation.rationale = {
                **(operation.rationale or {}),
                "skipReason": "unsupported_in_patch_engine_v0",
            }
            counts["skipped"] += 1
            db.add(operation)
            continue
        paragraph = _paragraph_for_operation(paragraphs, operation)
        if paragraph is None:
            operation.status = "skipped"
            operation.rationale = {
                **(operation.rationale or {}),
                "skipReason": "paragraph_not_found_or_not_top_level",
            }
            counts["skipped"] += 1
            db.add(operation)
            continue
        _apply_paragraph_properties(paragraph, operation.payload)
        operation.status = "applied"
        counts["applied"] += 1
        db.add(operation)

    parts["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output = _zip_parts(parts)
    inspect_docx_package(output)
    db.commit()
    return output, {
        "applied": counts["applied"],
        "skipped": counts["skipped"],
        "supportedOperations": sorted(SUPPORTED_OPERATIONS),
        "outputOpenabilityCheck": "zip_and_required_parts_valid",
    }


def _paragraph_for_operation(
    paragraphs: list[ET.Element], operation: PatchOperation
) -> ET.Element | None:
    if operation.part_name != "word/document.xml":
        return None
    xml_path = operation.xml_path or ""
    match = re.search(r"/w:p\[(\d+)\]$", xml_path)
    if not match:
        return None
    index = int(match.group(1)) - 1
    if index < 0 or index >= len(paragraphs):
        return None
    return paragraphs[index]


def _apply_paragraph_properties(paragraph: ET.Element, payload: dict) -> None:
    rule = payload.get("ruleProperties", {})
    effective = rule.get("effective") or rule.get("representative", {}).get("effective") or {}
    selector = payload.get("ruleSelector", {})
    p_pr = _get_or_insert_first(paragraph, qn("w", "pPr"))

    style_id = selector.get("styleId") or effective.get("paragraphStyle")
    if style_id:
        _set_single_val_child(p_pr, "pStyle", str(style_id), first=True)

    if effective.get("alignment"):
        _set_single_val_child(p_pr, "jc", str(effective["alignment"]))
    if effective.get("keepNext") is True:
        _ensure_empty_child(p_pr, "keepNext")
    if effective.get("pageBreakBefore") is True:
        _ensure_empty_child(p_pr, "pageBreakBefore")
    if isinstance(effective.get("spacing"), dict):
        _set_attrs_child(p_pr, "spacing", effective["spacing"])
    if isinstance(effective.get("indent"), dict):
        _set_attrs_child(p_pr, "ind", effective["indent"])
    if isinstance(effective.get("numbering"), dict):
        _set_numbering(p_pr, effective["numbering"])


def _get_or_insert_first(parent: ET.Element, tag: str) -> ET.Element:
    child = parent.find(f"./{tag}")
    if child is not None:
        return child
    child = ET.Element(tag)
    parent.insert(0, child)
    return child


def _set_single_val_child(
    parent: ET.Element, local_name: str, value: str, first: bool = False
) -> ET.Element:
    child = parent.find(f"w:{local_name}", NS)
    if child is None:
        child = ET.Element(qn("w", local_name))
        if first:
            parent.insert(0, child)
        else:
            parent.append(child)
    child.set(qn("w", "val"), value)
    return child


def _ensure_empty_child(parent: ET.Element, local_name: str) -> ET.Element:
    child = parent.find(f"w:{local_name}", NS)
    if child is None:
        child = ET.SubElement(parent, qn("w", local_name))
    return child


def _set_attrs_child(parent: ET.Element, local_name: str, attrs: dict) -> ET.Element:
    child = parent.find(f"w:{local_name}", NS)
    if child is None:
        child = ET.SubElement(parent, qn("w", local_name))
    for key, value in attrs.items():
        if value is not None:
            child.set(qn("w", key), str(value))
    return child


def _set_numbering(parent: ET.Element, numbering: dict) -> None:
    num_pr = parent.find("w:numPr", NS)
    if num_pr is None:
        num_pr = ET.SubElement(parent, qn("w", "numPr"))
    if numbering.get("level") is not None:
        _set_single_val_child(num_pr, "ilvl", str(numbering["level"]))
    if numbering.get("numId") is not None:
        _set_single_val_child(num_pr, "numId", str(numbering["numId"]))


def _zip_parts(parts: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _store_output_version(
    db: Session, source_version: DocumentVersion, raw_output: bytes
) -> DocumentVersion:
    sha256 = hashlib.sha256(raw_output).hexdigest()
    document = Document(
        kind="output",
        name=f"patched-{source_version.filename}",
        sha256=sha256,
        is_current_template=False,
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id,
        version=1,
        filename=f"patched-{source_version.filename}",
        content_type=source_version.content_type,
        size_bytes=len(raw_output),
        sha256=sha256,
        raw_file=raw_output,
        status="done",
        progress=100,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version
