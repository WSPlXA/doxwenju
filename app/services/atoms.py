from xml.etree import ElementTree as ET

from app.services.ooxml import (
    NS,
    attr,
    classify_paragraph,
    effective_paragraph_format,
    effective_run_format,
    parse_properties,
    parse_xml,
    qn,
    text_content,
    xml_to_string,
)


def document_setup_atoms(document_xml: str, part_name: str) -> list[dict]:
    root = parse_xml(document_xml)
    atoms: list[dict] = []
    for idx, sect_pr in enumerate(root.findall(".//w:sectPr", NS), start=1):
        pg_sz = sect_pr.find("w:pgSz", NS)
        pg_mar = sect_pr.find("w:pgMar", NS)
        headers = [
            {"type": attr(ref, "w", "type"), "rId": attr(ref, "r", "id")}
            for ref in sect_pr.findall("w:headerReference", NS)
        ]
        footers = [
            {"type": attr(ref, "w", "type"), "rId": attr(ref, "r", "id")}
            for ref in sect_pr.findall("w:footerReference", NS)
        ]
        atoms.append(
            {
                "atom_type": "document_setup",
                "part_name": part_name,
                "xml_path": f"/w:document/w:body/w:sectPr[{idx}]",
                "raw_xml": xml_to_string(sect_pr),
                "normalized": {
                    "pageSize": {
                        "width": attr(pg_sz, "w", "w") if pg_sz is not None else None,
                        "height": attr(pg_sz, "w", "h") if pg_sz is not None else None,
                        "orient": attr(pg_sz, "w", "orient") if pg_sz is not None else None,
                    },
                    "pageMargins": {
                        "top": attr(pg_mar, "w", "top") if pg_mar is not None else None,
                        "right": attr(pg_mar, "w", "right") if pg_mar is not None else None,
                        "bottom": attr(pg_mar, "w", "bottom") if pg_mar is not None else None,
                        "left": attr(pg_mar, "w", "left") if pg_mar is not None else None,
                        "header": attr(pg_mar, "w", "header") if pg_mar is not None else None,
                        "footer": attr(pg_mar, "w", "footer") if pg_mar is not None else None,
                    },
                    "headers": headers,
                    "footers": footers,
                },
                "text_summary": "document section setup",
                "element_category": "document_setup",
            }
        )
    return atoms


def body_atoms(
    document_xml: str, styles_info: dict, numbering_info: dict, part_name: str
) -> list[dict]:
    root = parse_xml(document_xml)
    atoms: list[dict] = []
    paragraph_index = 0
    table_index = 0

    for child in root.findall("./w:body/*", NS):
        if child.tag == qn("w", "p"):
            paragraph_index += 1
            atoms.extend(
                _paragraph_atoms(child, paragraph_index, styles_info, numbering_info, part_name)
            )
        elif child.tag == qn("w", "tbl"):
            table_index += 1
            atoms.append(_table_atom(child, table_index, part_name))
            for p_idx, paragraph in enumerate(child.findall(".//w:p", NS), start=1):
                atoms.extend(
                    _paragraph_atoms(
                        paragraph,
                        p_idx,
                        styles_info,
                        numbering_info,
                        part_name,
                        prefix=f"/w:document/w:body/w:tbl[{table_index}]",
                    )
                )
    return atoms


def header_footer_atoms(
    xml_text: str, styles_info: dict, numbering_info: dict, part_name: str
) -> list[dict]:
    root = parse_xml(xml_text)
    atoms: list[dict] = []
    for idx, paragraph in enumerate(root.findall(".//w:p", NS), start=1):
        paragraph_atoms = _paragraph_atoms(paragraph, idx, styles_info, numbering_info, part_name)
        for atom in paragraph_atoms:
            atom["atom_type"] = (
                "header_footer" if atom["atom_type"] == "paragraph" else atom["atom_type"]
            )
            atom["element_category"] = "header_footer"
        atoms.extend(paragraph_atoms)
    return atoms


def note_atoms(
    xml_text: str, styles_info: dict, numbering_info: dict, part_name: str, atom_type: str
) -> list[dict]:
    root = parse_xml(xml_text)
    atoms: list[dict] = []
    note_tag = "footnote" if atom_type == "footnote" else "endnote"
    for note_idx, note in enumerate(root.findall(f"w:{note_tag}", NS), start=1):
        note_id = attr(note, "w", "id")
        note_text = text_content(note)
        normalized = {"noteId": note_id, "textLength": len(note_text)}
        atoms.append(
            {
                "atom_type": atom_type,
                "part_name": part_name,
                "xml_path": f"/w:{note_tag}s/w:{note_tag}[{note_idx}]",
                "raw_xml": xml_to_string(note),
                "normalized": normalized,
                "text_summary": note_text[:500],
                "element_category": atom_type,
            }
        )
        for p_idx, paragraph in enumerate(note.findall("w:p", NS), start=1):
            paragraph_atoms = _paragraph_atoms(
                paragraph,
                p_idx,
                styles_info,
                numbering_info,
                part_name,
                prefix=f"/w:{note_tag}s/w:{note_tag}[{note_idx}]",
            )
            for atom in paragraph_atoms:
                atom["atom_type"] = atom_type
                atom["element_category"] = atom_type
                atom["normalized"] = {"noteId": note_id, **atom["normalized"]}
            atoms.extend(paragraph_atoms)
    return atoms


def _paragraph_atoms(
    paragraph: ET.Element,
    paragraph_index: int,
    styles_info: dict,
    numbering_info: dict,
    part_name: str,
    prefix: str = "/w:document/w:body",
) -> list[dict]:
    p_pr = paragraph.find("w:pPr", NS)
    effective = effective_paragraph_format(p_pr, styles_info, numbering_info)
    style_id = effective.get("paragraphStyle")
    # Look up the human-readable style name (e.g. "Heading 1", "一级标题") so that
    # classify_paragraph can detect heading styles that use non-standard IDs.
    style_name: str | None = (
        styles_info.get("styles", {}).get(style_id, {}).get("name") if style_id else None
    )
    numbering = effective.get("numbering") if isinstance(effective.get("numbering"), dict) else {}
    numbering_id = numbering.get("numId") if isinstance(numbering, dict) else None
    outline_lvl = effective.get("outlineLvl")
    text = text_content(paragraph)
    category = classify_paragraph(style_id, text, numbering_id, style_name, outline_lvl)
    atom_type = (
        "heading" if category == "heading" else "list" if category == "list" else "paragraph"
    )

    atoms = [
        {
            "atom_type": atom_type,
            "part_name": part_name,
            "xml_path": f"{prefix}/w:p[{paragraph_index}]",
            "raw_xml": xml_to_string(paragraph),
            "normalized": {
                "effective": effective,
                "direct": parse_properties(p_pr),
                "textLength": len(text),
            },
            "text_summary": text[:500],
            "element_category": category,
            "style_id": style_id,
            "numbering_id": numbering_id,
        }
    ]

    for run_idx, run in enumerate(paragraph.findall("w:r", NS), start=1):
        run_text = text_content(run)
        drawing = run.find("w:drawing", NS)
        relationship_id = _drawing_relationship_id(drawing) if drawing is not None else None
        if drawing is not None:
            atoms.append(
                {
                    "atom_type": "image",
                    "part_name": part_name,
                    "xml_path": f"{prefix}/w:p[{paragraph_index}]/w:r[{run_idx}]/w:drawing",
                    "raw_xml": xml_to_string(drawing),
                    "normalized": _drawing_normalized(drawing),
                    "text_summary": "embedded drawing",
                    "element_category": "image",
                    "style_id": style_id,
                    "numbering_id": numbering_id,
                    "relationship_id": relationship_id,
                }
            )
        if run_text:
            r_pr = run.find("w:rPr", NS)
            atoms.append(
                {
                    "atom_type": "run",
                    "part_name": part_name,
                    "xml_path": f"{prefix}/w:p[{paragraph_index}]/w:r[{run_idx}]",
                    "raw_xml": xml_to_string(run),
                    "normalized": {
                        "effective": effective_run_format(
                            r_pr, style_id, styles_info, numbering_info
                        ),
                        "direct": parse_properties(r_pr),
                    },
                    "text_summary": run_text[:500],
                    "element_category": category,
                    "style_id": style_id,
                    "numbering_id": numbering_id,
                }
            )
    return atoms


def _table_atom(table: ET.Element, table_index: int, part_name: str) -> dict:
    tbl_pr = table.find("w:tblPr", NS)
    rows = table.findall("w:tr", NS)
    grid_cols = table.findall("w:tblGrid/w:gridCol", NS)
    text = text_content(table)
    return {
        "atom_type": "table",
        "part_name": part_name,
        "xml_path": f"/w:document/w:body/w:tbl[{table_index}]",
        "raw_xml": xml_to_string(table),
        "normalized": {
            "properties": parse_properties(tbl_pr),
            "rowCount": len(rows),
            "gridColumnCount": len(grid_cols),
            "gridColumns": [attr(col, "w", "w") for col in grid_cols],
            "textLength": len(text),
        },
        "text_summary": text[:500],
        "element_category": "table",
    }


def _drawing_relationship_id(drawing: ET.Element | None) -> str | None:
    if drawing is None:
        return None
    for blip in drawing.findall(".//a:blip", NS):
        embed = attr(blip, "r", "embed")
        if embed:
            return embed
    return None


def _drawing_normalized(drawing: ET.Element) -> dict:
    extent = drawing.find(".//wp:extent", NS)
    inline = drawing.find(".//wp:inline", NS) is not None
    anchor = drawing.find(".//wp:anchor", NS) is not None
    return {
        "relationshipId": _drawing_relationship_id(drawing),
        "layout": "inline" if inline else "anchor" if anchor else "unknown",
        "extentEmu": {
            "cx": extent.attrib.get("cx") if extent is not None else None,
            "cy": extent.attrib.get("cy") if extent is not None else None,
        },
    }
