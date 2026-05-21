import hashlib
import json
import multiprocessing
import re
import tempfile
from pathlib import Path
from queue import Empty

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document, DocumentVersion
from app.services.docx_package import inspect_docx_package

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def apply_word_layout_postprocess(
    db: Session, document_version_id: str
) -> tuple[DocumentVersion, dict]:
    version = db.get(DocumentVersion, document_version_id)
    if version is None:
        raise ValueError(f"DocumentVersion not found: {document_version_id}")

    win32com = _load_win32com()
    if win32com is None:
        return version, {
            "status": "skipped",
            "reason": "pywin32_not_available",
            "documentVersionId": version.id,
        }

    try:
        raw_output, summary = _run_word_postprocess_with_timeout(
            version.raw_file, version.filename
        )
    except Exception as exc:
        return version, {
            "status": "skipped",
            "reason": "word_postprocess_failed",
            "error": str(exc),
            "documentVersionId": version.id,
        }

    inspect_docx_package(raw_output)
    output_version = _store_postprocessed_version(db, version, raw_output)
    return output_version, {
        **summary,
        "status": "done",
        "sourceDocumentVersionId": version.id,
        "outputDocumentVersionId": output_version.id,
    }


def _load_win32com():
    try:
        import win32com.client  # type: ignore[import-not-found]
    except Exception:
        return None
    return win32com


def _run_word_postprocess_with_timeout(raw_file: bytes, filename: str) -> tuple[bytes, dict]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue(maxsize=1)
    process = context.Process(target=_word_postprocess_worker, args=(raw_file, filename, queue))
    process.start()
    process.join(settings.word_postprocess_timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(10)
        raise TimeoutError(
            "Word postprocess timed out after "
            f"{settings.word_postprocess_timeout_seconds}s"
        )
    try:
        result = queue.get_nowait()
    except Empty as exc:
        raise RuntimeError(
            f"Word postprocess exited without a result: exitcode={process.exitcode}"
        ) from exc
    if result["status"] == "error":
        raise RuntimeError(result["error"])
    return result["raw_output"], result["summary"]


def _word_postprocess_worker(raw_file: bytes, filename: str, queue) -> None:
    try:
        win32com = _load_win32com()
        if win32com is None:
            raise RuntimeError("pywin32_not_available")
        raw_output, summary = _postprocess_with_word(raw_file, filename, win32com)
        queue.put({"status": "ok", "raw_output": raw_output, "summary": summary})
    except Exception as exc:
        queue.put({"status": "error", "error": str(exc)})


def _postprocess_with_word(raw_file: bytes, filename: str, win32com) -> tuple[bytes, dict]:
    with tempfile.TemporaryDirectory(prefix="doxwenju-word-postprocess-") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / _safe_docx_name(filename)
        output_path = tmp_path / f"postprocessed-{input_path.name}"
        input_path.write_bytes(raw_file)
        output_path.write_bytes(raw_file)

        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            doc = word.Documents.Open(str(output_path))
            summary = _apply_word_document_layout(doc)
            doc.Repaginate()
            summary["pageCount"] = int(doc.ComputeStatistics(2))
            doc.Save()
            doc.Close(False)
        finally:
            word.Quit()

        return output_path.read_bytes(), summary


def _apply_word_document_layout(doc) -> dict:
    constants = _WordConstants()
    _apply_page_setup_and_headers(doc, constants)
    _fix_cover_tables(doc, constants)
    toc_summary = _rebuild_static_toc(doc, constants)
    _apply_page_setup_and_headers(doc, constants)
    _force_black_text(doc)
    return {
        "layoutEngine": "word_com_v0",
        **toc_summary,
    }


class _WordConstants:
    align_center = 1
    align_left = 0
    align_tab_right = 2
    tab_leader_dots = 1
    line_space_exactly = 4
    page_break = 7
    field_page = 33
    active_end_adjusted_page_number = 1


def _apply_page_setup_and_headers(doc, constants: _WordConstants) -> None:
    for section in doc.Sections:
        page_setup = section.PageSetup
        page_setup.PageWidth = 595.3
        page_setup.PageHeight = 841.9
        page_setup.TopMargin = 56.7
        page_setup.BottomMargin = 56.7
        page_setup.LeftMargin = 70.9
        page_setup.RightMargin = 70.9
        page_setup.HeaderDistance = 42.6
        page_setup.FooterDistance = 49.6

    for section in doc.Sections:
        for header in section.Headers:
            _format_header_footer_range(header.Range, constants)
        for footer in section.Footers:
            footer_range = footer.Range
            _format_header_footer_range(footer_range, constants)
            if _clean_range_text(footer_range) == "":
                footer_range.Fields.Add(footer_range, constants.field_page)
                footer_range.ParagraphFormat.Alignment = constants.align_center


def _format_header_footer_range(range_obj, constants: _WordConstants) -> None:
    range_obj.Font.Name = "Times New Roman"
    range_obj.Font.NameFarEast = "宋体"
    range_obj.Font.Size = 10.5
    range_obj.Font.Bold = 0
    range_obj.Font.Underline = 0
    range_obj.Font.Color = 0
    range_obj.ParagraphFormat.Alignment = constants.align_center


def _fix_cover_tables(doc, constants: _WordConstants) -> None:
    if doc.Tables.Count <= 0:
        return
    table = doc.Tables.Item(1)
    table.AllowAutoFit = False
    table.Rows.HeightRule = 0
    table.Range.ParagraphFormat.SpaceBefore = 0
    table.Range.ParagraphFormat.SpaceAfter = 0
    for row_index in range(1, table.Rows.Count + 1):
        for column_index in range(1, table.Columns.Count + 1):
            try:
                cell = table.Cell(row_index, column_index)
            except Exception:
                continue
            text = _clean_range_text(cell.Range)
            cell.VerticalAlignment = 1
            if len(text) > 22:
                cell.Range.Font.Size = 14
                cell.Range.Font.Bold = 1
                cell.Range.ParagraphFormat.Alignment = constants.align_center
                cell.Range.ParagraphFormat.LineSpacingRule = constants.line_space_exactly
                cell.Range.ParagraphFormat.LineSpacing = 17


def _rebuild_static_toc(doc, constants: _WordConstants) -> dict:
    toc_paragraph = _find_paragraph(doc, "目  录")
    body_paragraph = _find_paragraph(doc, "导  论")
    if toc_paragraph is None or body_paragraph is None:
        return {"tocStatus": "skipped", "tocReason": "markers_not_found", "tocEntries": 0}

    insert_at = toc_paragraph.Range.Start
    doc.Range(toc_paragraph.Range.Start, body_paragraph.Range.Start).Delete()
    doc.Range(insert_at, insert_at).InsertAfter("目  录\r")
    title_paragraph = _find_paragraph(doc, "目  录", start_after=insert_at)
    if title_paragraph is None:
        return {"tocStatus": "skipped", "tocReason": "title_insert_failed", "tocEntries": 0}
    title_paragraph.OutlineLevel = 10

    doc.Range(title_paragraph.Range.End, title_paragraph.Range.End).InsertBreak(
        constants.page_break
    )
    doc.Repaginate()
    body_paragraph = _find_paragraph(doc, "导  论", start_after=insert_at)
    if body_paragraph is None:
        return {"tocStatus": "skipped", "tocReason": "body_marker_missing", "tocEntries": 0}

    headings = _collect_headings(doc, body_paragraph.Range.Start, constants)
    doc.Range(title_paragraph.Range.End, body_paragraph.Range.Start).Delete()
    _write_static_toc_entries(doc, insert_at, title_paragraph, headings, constants)
    doc.Repaginate()

    body_paragraph = _find_paragraph(doc, "导  论", start_after=insert_at)
    if body_paragraph is None:
        return {"tocStatus": "skipped", "tocReason": "body_marker_missing_after_toc", "tocEntries": 0}
    final_headings = _collect_headings(doc, body_paragraph.Range.Start, constants)
    doc.Range(title_paragraph.Range.End, body_paragraph.Range.Start).Delete()
    _write_static_toc_entries(doc, insert_at, title_paragraph, final_headings, constants)
    _offset_toc_page_numbers(doc, insert_at, offset=1)
    _format_static_toc(doc, insert_at, constants)
    return {"tocStatus": "done", "tocEntries": len(final_headings), "tocPageOffset": 1}


def _write_static_toc_entries(
    doc,
    insert_at: int,
    title_paragraph,
    headings: list[tuple[int, str, int]],
    constants: _WordConstants,
) -> None:
    lines = "".join(f"{title}\t{page}\r" for _, title, page in headings)
    doc.Range(title_paragraph.Range.End, title_paragraph.Range.End).InsertAfter(lines)
    body_paragraph = _find_paragraph(doc, "导  论", start_after=insert_at)
    if body_paragraph is not None:
        doc.Range(body_paragraph.Range.Start, body_paragraph.Range.Start).InsertBreak(
            constants.page_break
        )
    _format_static_toc(doc, insert_at, constants)


def _collect_headings(
    doc, body_start: int, constants: _WordConstants
) -> list[tuple[int, str, int]]:
    headings = []
    seen = set()
    for index in range(1, doc.Paragraphs.Count + 1):
        paragraph = doc.Paragraphs.Item(index)
        if paragraph.Range.Start < body_start:
            continue
        text = _clean_range_text(paragraph.Range)
        if not text or len(text) > 80:
            continue
        try:
            level = int(paragraph.OutlineLevel)
        except Exception:
            continue
        if level < 1 or level > 3:
            continue
        key = (text, level, paragraph.Range.Start)
        if key in seen:
            continue
        seen.add(key)
        page = int(paragraph.Range.Information(constants.active_end_adjusted_page_number))
        headings.append((level, text, page))
    return headings


def _format_static_toc(doc, insert_at: int, constants: _WordConstants) -> None:
    body_paragraph = _find_paragraph(doc, "导  论", start_after=insert_at)
    if body_paragraph is None:
        return
    toc_end = body_paragraph.Range.Start
    content_width = (
        doc.PageSetup.PageWidth - doc.PageSetup.LeftMargin - doc.PageSetup.RightMargin
    )
    heading_levels = _heading_level_lookup(doc, body_paragraph.Range.Start)
    for index in range(1, doc.Paragraphs.Count + 1):
        paragraph = doc.Paragraphs.Item(index)
        if paragraph.Range.Start < insert_at or paragraph.Range.Start >= toc_end:
            continue
        text = _clean_range_text(paragraph.Range)
        range_obj = paragraph.Range
        range_obj.Font.Color = 0
        range_obj.Font.Underline = 0
        range_obj.Font.Name = "Times New Roman"
        range_obj.Font.NameFarEast = "宋体"
        paragraph.Format.SpaceBefore = 0
        paragraph.Format.SpaceAfter = 0
        paragraph.Format.LineSpacingRule = constants.line_space_exactly
        if text == "目  录":
            range_obj.Font.NameFarEast = "黑体"
            range_obj.Font.Size = 18
            range_obj.Font.Bold = 1
            paragraph.Format.Alignment = constants.align_center
            paragraph.Format.LineSpacing = 24
            paragraph.OutlineLevel = 10
        elif "\t" in paragraph.Range.Text:
            title = paragraph.Range.Text.replace("\r", "").replace("\x07", "").split("\t", 1)[0].strip()
            level = heading_levels.get(title, 1)
            range_obj.Font.Size = 10.5
            range_obj.Font.Bold = 0
            paragraph.Format.Alignment = constants.align_left
            paragraph.Format.LineSpacing = 13
            paragraph.Format.LeftIndent = (level - 1) * 21
            paragraph.Format.FirstLineIndent = 0
            paragraph.Format.TabStops.ClearAll()
            paragraph.Format.TabStops.Add(
                content_width, constants.align_tab_right, constants.tab_leader_dots
            )


def _offset_toc_page_numbers(doc, insert_at: int, offset: int) -> None:
    body_paragraph = _find_paragraph(doc, "导  论", start_after=insert_at)
    if body_paragraph is None:
        return
    for index in range(1, doc.Paragraphs.Count + 1):
        paragraph = doc.Paragraphs.Item(index)
        if paragraph.Range.Start <= insert_at or paragraph.Range.Start >= body_paragraph.Range.Start:
            continue
        raw = paragraph.Range.Text
        if "\t" not in raw:
            continue
        text = raw.replace("\r", "").replace("\x07", "")
        match = re.match(r"^(.*\t)(\d+)\s*$", text)
        if match:
            paragraph.Range.Text = f"{match.group(1)}{int(match.group(2)) + offset}\r"


def _heading_level_lookup(doc, body_start: int) -> dict[str, int]:
    lookup = {}
    for index in range(1, doc.Paragraphs.Count + 1):
        paragraph = doc.Paragraphs.Item(index)
        if paragraph.Range.Start < body_start:
            continue
        text = _clean_range_text(paragraph.Range)
        try:
            level = int(paragraph.OutlineLevel)
        except Exception:
            continue
        if 1 <= level <= 3 and text:
            lookup.setdefault(text, level)
    return lookup


def _force_black_text(doc) -> None:
    doc.Range().Font.Color = 0
    for section in doc.Sections:
        for header in section.Headers:
            header.Range.Font.Color = 0
        for footer in section.Footers:
            footer.Range.Font.Color = 0


def _find_paragraph(doc, text: str, start_after: int = 0):
    for index in range(1, doc.Paragraphs.Count + 1):
        paragraph = doc.Paragraphs.Item(index)
        if paragraph.Range.Start >= start_after and _clean_range_text(paragraph.Range) == text:
            return paragraph
    return None


def _clean_range_text(range_obj) -> str:
    return range_obj.Text.replace("\r", "").replace("\n", "").replace("\x07", "").strip()


def _safe_docx_name(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "document.docx"
    return stem if stem.lower().endswith(".docx") else f"{stem}.docx"


def _store_postprocessed_version(
    db: Session, source_version: DocumentVersion, raw_output: bytes
) -> DocumentVersion:
    sha256 = hashlib.sha256(raw_output).hexdigest()
    document = Document(
        kind="output",
        name=f"postprocessed-{source_version.filename}",
        sha256=sha256,
        is_current_template=False,
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id,
        version=1,
        filename=f"postprocessed-{source_version.filename}",
        content_type=source_version.content_type or DOCX_CONTENT_TYPE,
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


def postprocess_summary_json(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)
