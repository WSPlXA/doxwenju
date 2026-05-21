import hashlib
import re
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from app.core.config import settings
from app.services.docx_package import inspect_docx_package
from app.services.ooxml import NS, parse_xml, qn, text_content
from app.services.patch_engine import (
    P_PR_ORDER,
    R_PR_ORDER,
)
from app.services.patch_engine import (
    _serialize_xml as _serialize_xml,
)
from app.services.rendering import _libreoffice_executable, _renderer_version

ET.register_namespace("w", NS["w"])
ET.register_namespace("r", NS["r"])
ET.register_namespace("wp", NS["wp"])
ET.register_namespace("a", NS["a"])
ET.register_namespace("mc", NS["mc"])

SETTINGS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
)


def apply_ooxml_layout_postprocess(raw_file: bytes) -> tuple[bytes, dict]:
    parts = {part.name: part.data for part in inspect_docx_package(raw_file)}
    document_xml = parts.get("word/document.xml")
    if document_xml is None:
        raise ValueError("DOCX is missing word/document.xml")

    document_root = parse_xml(document_xml)
    summary = {
        "layoutEngine": "ooxml_v0",
        "pageSetupSections": _apply_page_setup(document_root),
        "coverTables": _fix_cover_table(document_root),
        "headerFooterParts": _format_header_footer_parts(parts),
    }
    summary.update(_replace_static_toc_with_field(document_root))
    parts["word/document.xml"] = _serialize_xml(document_root)
    parts["word/settings.xml"] = _settings_with_update_fields(parts.get("word/settings.xml"))
    parts["[Content_Types].xml"] = _content_types_with_settings(parts["[Content_Types].xml"])

    raw_output = _zip_parts(parts)
    inspect_docx_package(raw_output)
    refreshed_output, refresh_summary = refresh_docx_with_libreoffice(
        raw_output, "ooxml-postprocessed.docx"
    )
    return refreshed_output, {
        **summary,
        "libreOfficeRefresh": refresh_summary,
        "sha256": hashlib.sha256(refreshed_output).hexdigest(),
    }


def refresh_docx_with_libreoffice(raw_file: bytes, filename: str) -> tuple[bytes, dict]:
    executable = _libreoffice_executable()
    if executable is None:
        return raw_file, {"status": "skipped", "reason": "libreoffice_not_found"}

    with tempfile.TemporaryDirectory(prefix="doxwenju-ooxml-refresh-") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / _safe_docx_name(filename)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        input_path.write_bytes(raw_file)
        command = [
            executable,
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(output_dir),
            str(input_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=settings.libreoffice_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return raw_file, {
                "status": "skipped",
                "reason": "libreoffice_timeout",
                "timeoutSeconds": settings.libreoffice_timeout_seconds,
            }
        output_path = output_dir / input_path.name
        if completed.returncode != 0 or not output_path.exists():
            return raw_file, {
                "status": "skipped",
                "reason": "libreoffice_docx_refresh_failed",
                "rendererVersion": _renderer_version(executable),
                "stdoutTail": completed.stdout[-1000:],
                "stderrTail": completed.stderr[-1000:],
            }
        refreshed = output_path.read_bytes()
        inspect_docx_package(refreshed)
        return refreshed, {
            "status": "done",
            "rendererVersion": _renderer_version(executable),
            "sizeBytes": len(refreshed),
            "stdoutTail": completed.stdout[-1000:],
            "stderrTail": completed.stderr[-1000:],
        }


def _apply_page_setup(document_root: ET.Element) -> int:
    count = 0
    for sect_pr in document_root.findall(".//w:sectPr", NS):
        _set_attrs_child(sect_pr, "pgSz", {"w": "11906", "h": "16838"})
        _set_attrs_child(
            sect_pr,
            "pgMar",
            {
                "top": "1134",
                "right": "1418",
                "bottom": "1134",
                "left": "1418",
                "header": "852",
                "footer": "992",
                "gutter": "0",
            },
        )
        count += 1
    return count


def _fix_cover_table(document_root: ET.Element) -> int:
    table = document_root.find(".//w:tbl", NS)
    if table is None:
        return 0

    tbl_pr = _get_or_insert_first(table, qn("w", "tblPr"))
    _set_single_val_child(tbl_pr, "tblLayout", "fixed")
    for paragraph in table.findall(".//w:p", NS):
        p_pr = _get_or_insert_first(paragraph, qn("w", "pPr"))
        _set_attrs_child(p_pr, "spacing", {"before": "0", "after": "0"})
        _sort_ooxml_children(p_pr, P_PR_ORDER)

    long_cells = 0
    for cell in table.findall(".//w:tc", NS):
        text = text_content(cell)
        if not text:
            continue
        tc_pr = _get_or_insert_first(cell, qn("w", "tcPr"))
        _set_single_val_child(tc_pr, "vAlign", "center")
        if len(text) <= 22:
            continue
        long_cells += 1
        for paragraph in cell.findall(".//w:p", NS):
            p_pr = _get_or_insert_first(paragraph, qn("w", "pPr"))
            _set_single_val_child(p_pr, "jc", "center")
            _set_attrs_child(p_pr, "spacing", {"before": "0", "after": "0", "line": "340", "lineRule": "exact"})
            _sort_ooxml_children(p_pr, P_PR_ORDER)
            for run in paragraph.findall("w:r", NS):
                r_pr = _get_or_insert_first(run, qn("w", "rPr"))
                _set_run_fonts(r_pr, ascii_font="Times New Roman", east_asia_font="宋体")
                _ensure_empty_child(r_pr, "b")
                _set_single_val_child(r_pr, "sz", "28")
                _set_single_val_child(r_pr, "szCs", "28")
                _sort_ooxml_children(r_pr, R_PR_ORDER)
    return long_cells


def _format_header_footer_parts(parts: dict[str, bytes]) -> int:
    count = 0
    for part_name in list(parts):
        if not re.fullmatch(r"word/(header|footer)\d+\.xml", part_name):
            continue
        root = parse_xml(parts[part_name])
        _format_paragraphs(root.findall(".//w:p", NS), center=True, size="21")
        if "/footer" in part_name and not text_content(root):
            paragraph = root.find(".//w:p", NS)
            if paragraph is not None:
                paragraph[:] = [_page_field_run()]
        parts[part_name] = _serialize_xml(root)
        count += 1
    return count


def _replace_static_toc_with_field(document_root: ET.Element) -> dict:
    body = document_root.find("w:body", NS)
    if body is None:
        return {"tocStatus": "skipped", "tocReason": "body_not_found"}
    children = list(body)
    toc_index = _index_of_paragraph(children, "目录")
    body_index = _index_of_paragraph(children, "导论")
    if body_index is None:
        return {"tocStatus": "skipped", "tocReason": "body_marker_not_found"}

    replace_start = toc_index if toc_index is not None and toc_index < body_index else body_index
    section_tail = [
        child for child in children[replace_start:body_index] if _local_name(child.tag) == "sectPr"
    ]
    replacement = [
        _toc_title_paragraph(),
        _toc_field_paragraph(),
        _page_break_paragraph(),
        *section_tail,
    ]
    body[replace_start:body_index] = replacement
    mode = "replaced_static_range" if replace_start != body_index else "inserted_before_body"
    return {
        "tocStatus": "field_inserted",
        "tocMode": mode,
        "tocField": 'TOC \\o "1-3" \\h \\z \\u',
    }


def _toc_title_paragraph() -> ET.Element:
    paragraph = ET.Element(qn("w", "p"))
    p_pr = ET.SubElement(paragraph, qn("w", "pPr"))
    _set_single_val_child(p_pr, "jc", "center")
    _set_attrs_child(p_pr, "spacing", {"before": "0", "after": "0", "line": "480", "lineRule": "exact"})
    _set_single_val_child(p_pr, "outlineLvl", "9")
    run = ET.SubElement(paragraph, qn("w", "r"))
    r_pr = ET.SubElement(run, qn("w", "rPr"))
    _set_run_fonts(r_pr, ascii_font="Times New Roman", east_asia_font="黑体")
    _ensure_empty_child(r_pr, "b")
    _set_single_val_child(r_pr, "sz", "36")
    _set_single_val_child(r_pr, "szCs", "36")
    ET.SubElement(run, qn("w", "t")).text = "目  录"
    return paragraph


def _toc_field_paragraph() -> ET.Element:
    paragraph = ET.Element(qn("w", "p"))
    p_pr = ET.SubElement(paragraph, qn("w", "pPr"))
    _set_attrs_child(p_pr, "spacing", {"before": "0", "after": "0", "line": "260", "lineRule": "exact"})
    field = ET.SubElement(paragraph, qn("w", "fldSimple"))
    field.set(qn("w", "instr"), 'TOC \\o "1-3" \\h \\z \\u')
    run = ET.SubElement(field, qn("w", "r"))
    r_pr = ET.SubElement(run, qn("w", "rPr"))
    _set_run_fonts(r_pr, ascii_font="Times New Roman", east_asia_font="宋体")
    _set_single_val_child(r_pr, "sz", "21")
    _set_single_val_child(r_pr, "szCs", "21")
    ET.SubElement(run, qn("w", "t")).text = "右键更新目录，或由 LibreOffice/Word 打开时刷新"
    return paragraph


def _page_break_paragraph() -> ET.Element:
    paragraph = ET.Element(qn("w", "p"))
    run = ET.SubElement(paragraph, qn("w", "r"))
    br = ET.SubElement(run, qn("w", "br"))
    br.set(qn("w", "type"), "page")
    return paragraph


def _settings_with_update_fields(settings_xml: bytes | None) -> bytes:
    if settings_xml:
        root = parse_xml(settings_xml)
    else:
        root = ET.Element(qn("w", "settings"))
    update_fields = root.find("w:updateFields", NS)
    if update_fields is None:
        update_fields = ET.SubElement(root, qn("w", "updateFields"))
    update_fields.set(qn("w", "val"), "true")
    return _serialize_xml(root)


def _content_types_with_settings(content_types_xml: bytes) -> bytes:
    if b'PartName="/word/settings.xml"' in content_types_xml:
        return content_types_xml
    override = (
        f'<Override PartName="/word/settings.xml" ContentType="{SETTINGS_CONTENT_TYPE}"/>'
    ).encode()
    closing = b"</Types>"
    if closing not in content_types_xml:
        raise ValueError("[Content_Types].xml is missing </Types>")
    return content_types_xml.replace(closing, override + closing, 1)


def _format_paragraphs(paragraphs: Iterable[ET.Element], center: bool, size: str) -> None:
    for paragraph in paragraphs:
        p_pr = _get_or_insert_first(paragraph, qn("w", "pPr"))
        if center:
            _set_single_val_child(p_pr, "jc", "center")
        _sort_ooxml_children(p_pr, P_PR_ORDER)
        for run in paragraph.findall("w:r", NS):
            r_pr = _get_or_insert_first(run, qn("w", "rPr"))
            _set_run_fonts(r_pr, ascii_font="Times New Roman", east_asia_font="宋体")
            _set_single_val_child(r_pr, "sz", size)
            _set_single_val_child(r_pr, "szCs", size)
            _sort_ooxml_children(r_pr, R_PR_ORDER)


def _page_field_run() -> ET.Element:
    run = ET.Element(qn("w", "r"))
    fld_char_begin = ET.SubElement(run, qn("w", "fldChar"))
    fld_char_begin.set(qn("w", "fldCharType"), "begin")
    instr = ET.SubElement(run, qn("w", "instrText"))
    instr.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instr.text = " PAGE "
    fld_char_sep = ET.SubElement(run, qn("w", "fldChar"))
    fld_char_sep.set(qn("w", "fldCharType"), "separate")
    ET.SubElement(run, qn("w", "t")).text = "1"
    fld_char_end = ET.SubElement(run, qn("w", "fldChar"))
    fld_char_end.set(qn("w", "fldCharType"), "end")
    return run


def _index_of_paragraph(children: list[ET.Element], text: str) -> int | None:
    marker = _normalize_marker(text)
    for index, child in enumerate(children):
        if child.tag == qn("w", "p") and _normalize_marker(text_content(child)) == marker:
            return index
    return None


def _normalize_marker(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text)


def _set_run_fonts(r_pr: ET.Element, ascii_font: str, east_asia_font: str) -> None:
    child = r_pr.find("w:rFonts", NS)
    if child is None:
        child = ET.SubElement(r_pr, qn("w", "rFonts"))
    child.set(qn("w", "ascii"), ascii_font)
    child.set(qn("w", "hAnsi"), ascii_font)
    child.set(qn("w", "eastAsia"), east_asia_font)
    child.set(qn("w", "cs"), ascii_font)


def _get_or_insert_first(parent: ET.Element, tag: str) -> ET.Element:
    child = parent.find(f"./{tag}")
    if child is not None:
        return child
    child = ET.Element(tag)
    parent.insert(0, child)
    return child


def _set_single_val_child(parent: ET.Element, local_name: str, value: str) -> ET.Element:
    child = parent.find(f"w:{local_name}", NS)
    if child is None:
        child = ET.SubElement(parent, qn("w", local_name))
    child.set(qn("w", "val"), value)
    return child


def _ensure_empty_child(parent: ET.Element, local_name: str) -> ET.Element:
    child = parent.find(f"w:{local_name}", NS)
    if child is None:
        child = ET.SubElement(parent, qn("w", local_name))
    return child


def _set_attrs_child(parent: ET.Element, local_name: str, attrs: dict[str, str]) -> ET.Element:
    child = parent.find(f"w:{local_name}", NS)
    if child is None:
        child = ET.SubElement(parent, qn("w", local_name))
    for key, value in attrs.items():
        child.set(qn("w", key), value)
    return child


def _sort_ooxml_children(parent: ET.Element, order: dict[str, int]) -> None:
    children = list(parent)
    if len(children) < 2:
        return
    parent[:] = [
        child
        for _, child in sorted(
            enumerate(children),
            key=lambda item: (order.get(_local_name(item[1].tag), 10_000), item[0]),
        )
    ]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _safe_docx_name(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "document.docx"
    return stem if stem.lower().endswith(".docx") else f"{stem}.docx"


def _zip_parts(parts: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buffer.getvalue()
