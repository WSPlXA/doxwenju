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
ET.register_namespace("mc", NS["mc"])

SUPPORTED_OPERATIONS = {
    "apply_document_setup_rule",
    "apply_endnote_rule",
    "apply_footnote_rule",
    "apply_header_footer_rule",
    "apply_table_rule",
    "apply_heading_rule",
    "apply_list_rule",
    "apply_caption_rule",
    "apply_citation_rule",
    "apply_paragraph_rule",
}

P_PR_ORDER = {
    "pStyle": 10,
    "keepNext": 20,
    "keepLines": 30,
    "pageBreakBefore": 40,
    "framePr": 50,
    "widowControl": 60,
    "numPr": 70,
    "suppressLineNumbers": 80,
    "pBdr": 90,
    "shd": 100,
    "tabs": 110,
    "suppressAutoHyphens": 120,
    "kinsoku": 130,
    "wordWrap": 140,
    "overflowPunct": 150,
    "topLinePunct": 160,
    "autoSpaceDE": 170,
    "autoSpaceDN": 180,
    "bidi": 190,
    "adjustRightInd": 200,
    "snapToGrid": 210,
    "spacing": 220,
    "ind": 230,
    "contextualSpacing": 240,
    "mirrorIndents": 250,
    "suppressOverlap": 260,
    "jc": 270,
    "textDirection": 280,
    "textAlignment": 290,
    "textboxTightWrap": 300,
    "outlineLvl": 310,
    "divId": 320,
    "cnfStyle": 330,
    "rPr": 340,
    "sectPr": 350,
    "pPrChange": 360,
}

TBL_PR_ORDER = {
    "tblStyle": 10,
    "tblpPr": 20,
    "tblOverlap": 30,
    "bidiVisual": 40,
    "tblStyleRowBandSize": 50,
    "tblStyleColBandSize": 60,
    "tblW": 70,
    "jc": 80,
    "tblCellSpacing": 90,
    "tblInd": 100,
    "tblBorders": 110,
    "shd": 120,
    "tblLayout": 130,
    "tblCellMar": 140,
    "tblLook": 150,
    "tblCaption": 160,
    "tblDescription": 170,
    "tblPrChange": 180,
}

R_PR_ORDER = {
    "rStyle": 10,
    "rFonts": 20,
    "b": 30,
    "bCs": 40,
    "i": 50,
    "iCs": 60,
    "caps": 70,
    "smallCaps": 80,
    "strike": 90,
    "dstrike": 100,
    "outline": 110,
    "shadow": 120,
    "emboss": 130,
    "imprint": 140,
    "noProof": 150,
    "snapToGrid": 160,
    "vanish": 170,
    "webHidden": 180,
    "color": 190,
    "spacing": 200,
    "w": 210,
    "kern": 220,
    "position": 230,
    "sz": 240,
    "szCs": 250,
    "highlight": 260,
    "u": 270,
    "effect": 280,
    "bdr": 290,
    "shd": 300,
    "fitText": 310,
    "vertAlign": 320,
    "rtl": 330,
    "cs": 340,
    "em": 350,
    "lang": 360,
    "eastAsianLayout": 370,
    "specVanish": 380,
    "oMath": 390,
    "rPrChange": 400,
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
    available_style_ids = _available_style_ids(parts.get("word/styles.xml"))
    paragraphs = root.findall("./w:body/w:p", NS)
    tables = root.findall("./w:body/w:tbl", NS)

    operations = list(
        db.scalars(
            select(PatchOperation)
            .where(PatchOperation.patch_plan_id == plan.id)
            .order_by(PatchOperation.created_at, PatchOperation.id)
        )
    )
    needs_note_parts = any(
        operation.operation_type in {"apply_footnote_rule", "apply_endnote_rule"}
        for operation in operations
    )
    needs_header_footer_parts = any(
        operation.operation_type == "apply_header_footer_rule" for operation in operations
    )
    note_roots = _parse_note_roots(parts) if needs_note_parts else {}
    header_footer_roots = (
        _parse_header_footer_roots(parts) if needs_header_footer_parts else {}
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
        applied = _apply_supported_operation(
            root, paragraphs, tables, note_roots, header_footer_roots, operation, available_style_ids
        )
        if not applied:
            counts["skipped"] += 1
            db.add(operation)
            continue
        operation.status = "applied"
        counts["applied"] += 1
        db.add(operation)

    parts["word/document.xml"] = _serialize_xml(root)
    for part_name, note_root in note_roots.items():
        parts[part_name] = _serialize_xml(note_root)
    for part_name, part_root in header_footer_roots.items():
        parts[part_name] = _serialize_xml(part_root)
    output = _zip_parts(parts)
    inspect_docx_package(output)
    db.commit()
    return output, {
        "applied": counts["applied"],
        "skipped": counts["skipped"],
        "supportedOperations": sorted(SUPPORTED_OPERATIONS),
        "outputOpenabilityCheck": "zip_and_required_parts_valid",
    }


def _apply_supported_operation(
    root: ET.Element,
    paragraphs: list[ET.Element],
    tables: list[ET.Element],
    note_roots: dict[str, ET.Element],
    header_footer_roots: dict[str, ET.Element],
    operation: PatchOperation,
    available_style_ids: set[str],
) -> bool:
    if operation.operation_type == "apply_document_setup_rule":
        sectpr = _sectpr_for_operation(root, operation)
        if sectpr is None:
            operation.status = "skipped"
            operation.rationale = {
                **(operation.rationale or {}),
                "skipReason": "sectpr_not_found",
            }
            return False
        _apply_document_setup_properties(sectpr, operation.payload)
        return True

    if operation.operation_type == "apply_table_rule":
        table = _table_for_operation(tables, operation)
        if table is None:
            operation.status = "skipped"
            operation.rationale = {
                **(operation.rationale or {}),
                "skipReason": "table_not_found_or_not_top_level",
            }
            return False
        _apply_table_properties(table, operation.payload)
        return True

    if operation.operation_type in {"apply_footnote_rule", "apply_endnote_rule"}:
        paragraph = _note_paragraph_for_operation(note_roots, operation)
        if paragraph is None:
            operation.status = "skipped"
            operation.rationale = {
                **(operation.rationale or {}),
                "skipReason": "note_paragraph_not_found_or_not_paragraph_level",
            }
            return False
        _apply_paragraph_properties(paragraph, operation.payload, available_style_ids)
        return True

    if operation.operation_type == "apply_header_footer_rule":
        paragraph = _part_paragraph_for_operation(header_footer_roots, operation)
        if paragraph is None:
            operation.status = "skipped"
            operation.rationale = {
                **(operation.rationale or {}),
                "skipReason": "header_footer_paragraph_not_found",
            }
            return False
        _apply_paragraph_properties(paragraph, operation.payload, available_style_ids)
        return True

    paragraph = _paragraph_for_operation(paragraphs, operation)
    if paragraph is None:
        operation.status = "skipped"
        operation.rationale = {
            **(operation.rationale or {}),
            "skipReason": "paragraph_not_found_or_not_top_level",
        }
        return False
    _apply_paragraph_properties(paragraph, operation.payload, available_style_ids)
    return True


def _sectpr_for_operation(root: ET.Element, operation: PatchOperation) -> ET.Element | None:
    if operation.part_name != "word/document.xml":
        return None
    xml_path = operation.xml_path or ""
    match = re.search(r"/w:sectPr\[(\d+)\]$", xml_path)
    if not match:
        # Accept a bare sectPr path without an index as the first (and usually only) one
        if not xml_path.endswith("/w:sectPr"):
            return None
        sect_prs = root.findall(".//w:sectPr", NS)
        return sect_prs[0] if sect_prs else None
    index = int(match.group(1)) - 1
    sect_prs = root.findall(".//w:sectPr", NS)
    return sect_prs[index] if 0 <= index < len(sect_prs) else None


_PG_SZ_ATTR_MAP = {"width": "w", "height": "h", "orient": "orient"}


def _apply_document_setup_properties(sectpr: ET.Element, payload: dict) -> None:
    rule_props = payload.get("ruleProperties", {})

    pg_sz_data = rule_props.get("pageSize")
    if isinstance(pg_sz_data, dict):
        pg_sz = sectpr.find("w:pgSz", NS)
        if pg_sz is None:
            pg_sz = ET.SubElement(sectpr, qn("w", "pgSz"))
        for key, ooxml_attr in _PG_SZ_ATTR_MAP.items():
            value = pg_sz_data.get(key)
            if value is not None:
                pg_sz.set(qn("w", ooxml_attr), str(value))

    pg_mar_data = rule_props.get("pageMargins")
    if isinstance(pg_mar_data, dict):
        pg_mar = sectpr.find("w:pgMar", NS)
        if pg_mar is None:
            pg_mar = ET.SubElement(sectpr, qn("w", "pgMar"))
        for attr_name, value in pg_mar_data.items():
            if value is not None:
                pg_mar.set(qn("w", attr_name), str(value))


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


def _parse_note_roots(parts: dict[str, bytes]) -> dict[str, ET.Element]:
    roots: dict[str, ET.Element] = {}
    for part_name in ("word/footnotes.xml", "word/endnotes.xml"):
        if part_name in parts:
            roots[part_name] = parse_xml(parts[part_name])
    return roots


def _parse_header_footer_roots(parts: dict[str, bytes]) -> dict[str, ET.Element]:
    roots: dict[str, ET.Element] = {}
    for part_name, data in parts.items():
        if re.fullmatch(r"word/(header|footer)\d+\.xml", part_name):
            roots[part_name] = parse_xml(data)
    return roots


def _part_paragraph_for_operation(
    part_roots: dict[str, ET.Element], operation: PatchOperation
) -> ET.Element | None:
    root = part_roots.get(operation.part_name)
    if root is None:
        return None
    xml_path = operation.xml_path or ""
    match = re.search(r"/w:p\[(\d+)\]$", xml_path)
    if not match:
        return None
    index = int(match.group(1)) - 1
    paragraphs = root.findall(".//w:p", NS)
    if index < 0 or index >= len(paragraphs):
        return None
    return paragraphs[index]


def _note_paragraph_for_operation(
    note_roots: dict[str, ET.Element], operation: PatchOperation
) -> ET.Element | None:
    expected_part = (
        "word/footnotes.xml"
        if operation.operation_type == "apply_footnote_rule"
        else "word/endnotes.xml"
    )
    if operation.part_name != expected_part:
        return None

    root = note_roots.get(expected_part)
    if root is None:
        return None

    note_local = "footnote" if operation.operation_type == "apply_footnote_rule" else "endnote"
    xml_path = operation.xml_path or ""
    paragraph_match = re.search(
        rf"/w:{note_local}s/w:{note_local}\[(\d+)\]/w:p\[(\d+)\](?:/.*)?$", xml_path
    )
    note_match = re.search(rf"/w:{note_local}s/w:{note_local}\[(\d+)\]$", xml_path)
    if paragraph_match:
        note_index = int(paragraph_match.group(1)) - 1
        paragraph_index = int(paragraph_match.group(2)) - 1
    elif note_match:
        note_index = int(note_match.group(1)) - 1
        paragraph_index = 0
    else:
        return None

    notes = root.findall(f"w:{note_local}", NS)
    if note_index < 0 or note_index >= len(notes):
        return None

    paragraphs = notes[note_index].findall("w:p", NS)
    if paragraph_index < 0 or paragraph_index >= len(paragraphs):
        return None
    return paragraphs[paragraph_index]


def _table_for_operation(tables: list[ET.Element], operation: PatchOperation) -> ET.Element | None:
    if operation.part_name != "word/document.xml":
        return None
    xml_path = operation.xml_path or ""
    match = re.search(r"/w:tbl\[(\d+)\]$", xml_path)
    if not match:
        return None
    index = int(match.group(1)) - 1
    if index < 0 or index >= len(tables):
        return None
    return tables[index]


def _apply_paragraph_properties(
    paragraph: ET.Element, payload: dict, available_style_ids: set[str]
) -> None:
    rule = payload.get("ruleProperties", {})
    effective = rule.get("effective") or rule.get("representative", {}).get("effective") or {}
    selector = payload.get("ruleSelector", {})
    p_pr = _get_or_insert_first(paragraph, qn("w", "pPr"))

    style_id = selector.get("styleId") or effective.get("paragraphStyle")
    if style_id and style_id in available_style_ids:
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
    outline_level = effective.get("outlineLvl")
    if isinstance(outline_level, int):
        _set_single_val_child(p_pr, "outlineLvl", str(outline_level))
    _sort_ooxml_children(p_pr, P_PR_ORDER)
    run_effective = rule.get("runEffective")
    if isinstance(run_effective, dict) and run_effective:
        _apply_text_run_properties(paragraph, run_effective)


def _apply_table_properties(table: ET.Element, payload: dict) -> None:
    rule = payload.get("ruleProperties", {})
    representative = rule.get("representative", {})
    table_props = representative.get("properties") or rule.get("properties") or {}
    if not table_props:
        return
    tbl_pr = _get_or_insert_first(table, qn("w", "tblPr"))
    if isinstance(table_props.get("width"), dict):
        _set_attrs_child(tbl_pr, "tblW", table_props["width"])
    if isinstance(table_props.get("borders"), dict):
        _set_table_borders(tbl_pr, table_props["borders"])
    if isinstance(table_props.get("cellMargins"), dict):
        _set_table_cell_margins(tbl_pr, table_props["cellMargins"])
    _sort_ooxml_children(tbl_pr, TBL_PR_ORDER)


def _apply_text_run_properties(paragraph: ET.Element, effective: dict) -> None:
    for run in paragraph.findall("w:r", NS):
        if run.find("w:t", NS) is None:
            continue
        r_pr = _get_or_insert_first(run, qn("w", "rPr"))
        fonts = effective.get("fonts")
        if isinstance(fonts, dict):
            _set_run_fonts(r_pr, fonts)
        if effective.get("b") is True:
            _ensure_empty_child(r_pr, "b")
        if effective.get("i") is True:
            _ensure_empty_child(r_pr, "i")
        if effective.get("caps") is True:
            _ensure_empty_child(r_pr, "caps")
        if effective.get("smallCaps") is True:
            _ensure_empty_child(r_pr, "smallCaps")
        if effective.get("sz") is not None:
            half_points = _half_point_value(effective["sz"])
            _set_single_val_child(r_pr, "sz", half_points)
            _set_single_val_child(r_pr, "szCs", half_points)
        _sort_ooxml_children(r_pr, R_PR_ORDER)


def _set_run_fonts(r_pr: ET.Element, fonts: dict) -> None:
    child = r_pr.find("w:rFonts", NS)
    if child is None:
        child = ET.SubElement(r_pr, qn("w", "rFonts"))
    for key in ("ascii", "eastAsia", "hAnsi", "cs", "asciiTheme", "eastAsiaTheme"):
        value = fonts.get(key)
        if value:
            child.set(qn("w", key), str(value))


def _half_point_value(value: object) -> str:
    if isinstance(value, int):
        return str(value * 2)
    if isinstance(value, float):
        return str(int(round(value * 2)))
    text = str(value)
    try:
        return str(int(round(float(text) * 2)))
    except ValueError:
        return text


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


def _set_table_borders(tbl_pr: ET.Element, borders: dict) -> None:
    tbl_borders = tbl_pr.find("w:tblBorders", NS)
    if tbl_borders is None:
        tbl_borders = ET.SubElement(tbl_pr, qn("w", "tblBorders"))
    for border_name, attrs in borders.items():
        if not isinstance(attrs, dict):
            continue
        border = tbl_borders.find(f"w:{border_name}", NS)
        if border is None:
            border = ET.SubElement(tbl_borders, qn("w", border_name))
        for key, value in attrs.items():
            if value is not None:
                border.set(qn("w", key), str(value))


def _set_table_cell_margins(tbl_pr: ET.Element, margins: dict) -> None:
    tbl_cell_mar = tbl_pr.find("w:tblCellMar", NS)
    if tbl_cell_mar is None:
        tbl_cell_mar = ET.SubElement(tbl_pr, qn("w", "tblCellMar"))
    for margin_name, attrs in margins.items():
        if not isinstance(attrs, dict):
            continue
        margin = tbl_cell_mar.find(f"w:{margin_name}", NS)
        if margin is None:
            margin = ET.SubElement(tbl_cell_mar, qn("w", margin_name))
        for key, value in attrs.items():
            if value is not None:
                margin.set(qn("w", key), str(value))


def _sort_ooxml_children(parent: ET.Element, order: dict[str, int]) -> None:
    children = list(parent)
    if len(children) < 2:
        return

    def sort_key(item: tuple[int, ET.Element]) -> tuple[int, int]:
        index, child = item
        local_name = child.tag.rsplit("}", 1)[-1]
        return (order.get(local_name, 10_000), index)

    parent[:] = [child for _, child in sorted(enumerate(children), key=sort_key)]


def _available_style_ids(styles_xml: bytes | None) -> set[str]:
    if not styles_xml:
        return set()
    root = parse_xml(styles_xml)
    return {
        style_id
        for style in root.findall("w:style", NS)
        if (style_id := style.attrib.get(qn("w", "styleId")))
    }


def _serialize_xml(root: ET.Element) -> bytes:
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    reparsed = ET.fromstring(raw)
    declared_prefixes = _declared_prefixes(raw)
    ignorable_attr = qn("mc", "Ignorable")
    ignorable_value = reparsed.attrib.get(ignorable_attr)
    if ignorable_value:
        valid_tokens = [
            token for token in ignorable_value.split() if token in declared_prefixes
        ]
        if valid_tokens:
            reparsed.set(ignorable_attr, " ".join(valid_tokens))
        else:
            reparsed.attrib.pop(ignorable_attr, None)
        raw = ET.tostring(reparsed, encoding="utf-8", xml_declaration=True)
    return raw


def _declared_prefixes(raw_xml: bytes) -> set[str]:
    root_match = re.search(rb"<[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?\b[^>]*>", raw_xml)
    if not root_match:
        return set()
    root_start = root_match.group(0)
    return {
        match.group(1).decode("ascii")
        for match in re.finditer(rb"\sxmlns:([A-Za-z_][\w.-]*)=", root_start)
    }


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
