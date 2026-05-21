import posixpath
import re
import struct
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as DET

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}

W = f"{{{NS['w']}}}"
R = f"{{{NS['r']}}}"


@dataclass(frozen=True)
class ParsedRelationship:
    source_part: str
    relationship_id: str
    relationship_type: str
    target: str
    target_mode: str | None
    resolved_target: str | None


def parse_xml(data: bytes | str) -> ET.Element:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return DET.fromstring(data)


def xml_to_string(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode")


def qn(namespace: str, local: str) -> str:
    return f"{{{NS[namespace]}}}{local}"


def attr(element: ET.Element, namespace: str, local: str) -> str | None:
    return element.attrib.get(qn(namespace, local))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text_content(element: ET.Element) -> str:
    return "".join(t.text or "" for t in element.iter(qn("w", "t"))).strip()


def rel_source_from_rels_part(part_name: str) -> str:
    if part_name == "_rels/.rels":
        return "/"
    match = re.match(r"(.*/)?_rels/(.+)\.rels$", part_name)
    if not match:
        return part_name
    prefix = match.group(1) or ""
    return prefix + match.group(2)


def resolve_relationship_target(
    source_part: str, target: str, target_mode: str | None
) -> str | None:
    if target_mode == "External":
        return None
    if source_part == "/":
        return posixpath.normpath(target).lstrip("/")
    source_dir = posixpath.dirname(source_part)
    return posixpath.normpath(posixpath.join(source_dir, target)).lstrip("/")


def parse_relationships(part_name: str, xml_text: str) -> list[ParsedRelationship]:
    source_part = rel_source_from_rels_part(part_name)
    root = parse_xml(xml_text)
    relationships: list[ParsedRelationship] = []
    for rel in root.findall("rel:Relationship", NS):
        target = rel.attrib.get("Target", "")
        target_mode = rel.attrib.get("TargetMode")
        relationships.append(
            ParsedRelationship(
                source_part=source_part,
                relationship_id=rel.attrib.get("Id", ""),
                relationship_type=rel.attrib.get("Type", ""),
                target=target,
                target_mode=target_mode,
                resolved_target=resolve_relationship_target(source_part, target, target_mode),
            )
        )
    return relationships


def parse_properties(container: ET.Element | None) -> dict:
    if container is None:
        return {}
    props: dict[str, object] = {}
    for child in list(container):
        name = local_name(child.tag)
        if name in {"rFonts"}:
            props["fonts"] = {
                "ascii": attr(child, "w", "ascii"),
                "eastAsia": attr(child, "w", "eastAsia"),
                "hAnsi": attr(child, "w", "hAnsi"),
                "cs": attr(child, "w", "cs"),
                "asciiTheme": attr(child, "w", "asciiTheme"),
                "eastAsiaTheme": attr(child, "w", "eastAsiaTheme"),
            }
        elif name in {
            "b",
            "i",
            "caps",
            "smallCaps",
            "strike",
            "keepNext",
            "keepLines",
            "pageBreakBefore",
        }:
            props[name] = attr(child, "w", "val") not in {"false", "0"}
        elif name in {"sz", "szCs"}:
            value = attr(child, "w", "val")
            props[name] = int(value) / 2 if value and value.isdigit() else value
        elif name == "color":
            props["color"] = attr(child, "w", "val") or attr(child, "w", "themeColor")
        elif name == "jc":
            props["alignment"] = attr(child, "w", "val")
        elif name == "spacing":
            props["spacing"] = {
                "before": attr(child, "w", "before"),
                "after": attr(child, "w", "after"),
                "line": attr(child, "w", "line"),
                "lineRule": attr(child, "w", "lineRule"),
            }
        elif name == "ind":
            props["indent"] = {
                "left": attr(child, "w", "left"),
                "right": attr(child, "w", "right"),
                "firstLine": attr(child, "w", "firstLine"),
                "hanging": attr(child, "w", "hanging"),
            }
        elif name == "numPr":
            ilvl = child.find("w:ilvl", NS)
            num_id = child.find("w:numId", NS)
            props["numbering"] = {
                "level": attr(ilvl, "w", "val") if ilvl is not None else None,
                "numId": attr(num_id, "w", "val") if num_id is not None else None,
            }
        elif name == "outlineLvl":
            value = attr(child, "w", "val")
            if value is not None:
                try:
                    props["outlineLvl"] = int(value)
                except (ValueError, TypeError):
                    pass
        elif name == "pStyle":
            props["paragraphStyle"] = attr(child, "w", "val")
        elif name == "rStyle":
            props["runStyle"] = attr(child, "w", "val")
        elif name == "tblW":
            props["width"] = {"w": attr(child, "w", "w"), "type": attr(child, "w", "type")}
        elif name == "tblBorders":
            props["borders"] = {
                local_name(border.tag): {
                    "val": attr(border, "w", "val"),
                    "sz": attr(border, "w", "sz"),
                    "color": attr(border, "w", "color"),
                }
                for border in list(child)
            }
        elif name == "tblCellMar":
            props["cellMargins"] = {
                local_name(margin.tag): {
                    "w": attr(margin, "w", "w"),
                    "type": attr(margin, "w", "type"),
                }
                for margin in list(child)
            }
    return {k: v for k, v in props.items() if v not in (None, {}, [])}


def parse_styles(xml_text: str | None) -> dict:
    if not xml_text:
        return {"styles": {}, "docDefaults": {}}
    root = parse_xml(xml_text)
    defaults: dict = {}
    doc_defaults = root.find("w:docDefaults", NS)
    if doc_defaults is not None:
        defaults = {
            "paragraph": parse_properties(doc_defaults.find("w:pPrDefault/w:pPr", NS)),
            "run": parse_properties(doc_defaults.find("w:rPrDefault/w:rPr", NS)),
        }

    styles: dict[str, dict] = {}
    for style in root.findall("w:style", NS):
        style_id = attr(style, "w", "styleId")
        if not style_id:
            continue
        name_el = style.find("w:name", NS)
        based_on = style.find("w:basedOn", NS)
        styles[style_id] = {
            "styleId": style_id,
            "type": attr(style, "w", "type"),
            "name": attr(name_el, "w", "val") if name_el is not None else style_id,
            "basedOn": attr(based_on, "w", "val") if based_on is not None else None,
            "paragraph": parse_properties(style.find("w:pPr", NS)),
            "run": parse_properties(style.find("w:rPr", NS)),
        }
    return {"styles": styles, "docDefaults": defaults}


def parse_numbering(xml_text: str | None) -> dict:
    if not xml_text:
        return {"abstractNums": {}, "nums": {}}
    root = parse_xml(xml_text)
    abstract_nums: dict[str, dict] = {}
    for abstract in root.findall("w:abstractNum", NS):
        abstract_id = attr(abstract, "w", "abstractNumId")
        if not abstract_id:
            continue
        levels = {}
        for lvl in abstract.findall("w:lvl", NS):
            ilvl = attr(lvl, "w", "ilvl") or "0"
            num_fmt = lvl.find("w:numFmt", NS)
            lvl_text = lvl.find("w:lvlText", NS)
            levels[ilvl] = {
                "format": attr(num_fmt, "w", "val") if num_fmt is not None else None,
                "text": attr(lvl_text, "w", "val") if lvl_text is not None else None,
                "paragraph": parse_properties(lvl.find("w:pPr", NS)),
                "run": parse_properties(lvl.find("w:rPr", NS)),
            }
        abstract_nums[abstract_id] = {"abstractNumId": abstract_id, "levels": levels}

    nums: dict[str, dict] = {}
    for num in root.findall("w:num", NS):
        num_id = attr(num, "w", "numId")
        abstract_ref = num.find("w:abstractNumId", NS)
        if num_id:
            nums[num_id] = {
                "numId": num_id,
                "abstractNumId": attr(abstract_ref, "w", "val")
                if abstract_ref is not None
                else None,
            }
    return {"abstractNums": abstract_nums, "nums": nums}


def merge_dicts(*items: dict) -> dict:
    merged: dict = {}
    for item in items:
        for key, value in (item or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = merge_dicts(merged[key], value)
            elif value not in (None, {}, []):
                merged[key] = value
    return merged


def resolve_style(style_id: str | None, styles_info: dict, prop_kind: str) -> dict:
    if not style_id:
        return {}
    styles = styles_info.get("styles", {})
    style = styles.get(style_id)
    if not style:
        return {}
    parent = resolve_style(style.get("basedOn"), styles_info, prop_kind)
    return merge_dicts(parent, style.get(prop_kind, {}))


def effective_paragraph_format(
    p_pr: ET.Element | None, styles_info: dict, numbering_info: dict
) -> dict:
    direct = parse_properties(p_pr)
    style_id = direct.get("paragraphStyle")
    style_props = resolve_style(style_id, styles_info, "paragraph")
    defaults = styles_info.get("docDefaults", {}).get("paragraph", {})
    numbering_props = {}
    numbering = direct.get("numbering") or style_props.get("numbering") or {}
    num_id = numbering.get("numId") if isinstance(numbering, dict) else None
    level = numbering.get("level") if isinstance(numbering, dict) else None
    if num_id and level is not None:
        num = numbering_info.get("nums", {}).get(str(num_id), {})
        abstract = numbering_info.get("abstractNums", {}).get(str(num.get("abstractNumId")), {})
        numbering_props = abstract.get("levels", {}).get(str(level), {}).get("paragraph", {})
    return merge_dicts(defaults, numbering_props, style_props, direct)


def effective_run_format(
    r_pr: ET.Element | None, paragraph_style_id: str | None, styles_info: dict, numbering_info: dict
) -> dict:
    direct = parse_properties(r_pr)
    run_style_id = direct.get("runStyle")
    defaults = styles_info.get("docDefaults", {}).get("run", {})
    paragraph_run = resolve_style(paragraph_style_id, styles_info, "run")
    run_style = resolve_style(run_style_id, styles_info, "run")
    return merge_dicts(defaults, paragraph_run, run_style, direct)


def classify_paragraph(
    style_id: str | None,
    text: str,
    numbering_id: str | None,
    style_name: str | None = None,
    outline_lvl: int | None = None,
) -> str:
    """Classify a paragraph into a semantic category.

    Detection priority (first match wins):
      1. Standard Word heading style ID ("Heading 1", etc.) or built-in title/subtitle
      2. Chinese / custom heading style *name* containing heading keywords
      3. outlineLvl attribute (0–8 means heading)
      4. Numbered section text pattern ("1 ", "1.1 ", "第一章", "一、")
      5. Ordered-list numbering id → list
      6. Caption / footnote / citation heuristics
      7. Default: paragraph
    """
    normalized_id = (style_id or "").lower()
    normalized_name = (style_name or "").lower()
    stripped = text.strip()
    stripped_lower = stripped.lower()

    # --- 1. Standard Word heading style ID ---
    if normalized_id.startswith("heading") or normalized_id in {"title", "subtitle"}:
        return "heading"

    # --- 2. Style name contains heading keywords (Chinese & English) ---
    _HEADING_NAME_KW = (
        "heading", "标题", "title", "chapter", "section",
        "一级", "二级", "三级", "四级",
    )
    if any(kw in normalized_name for kw in _HEADING_NAME_KW):
        return "heading"

    # --- 3. Explicit outline level in paragraph properties ---
    if isinstance(outline_lvl, int) and 0 <= outline_lvl <= 8:
        return "heading"

    # --- 4. Numbered-section text pattern (no numbering_id = not a list bullet) ---
    # Matches: "1 标题", "1.1 分析", "第一章", "第一节", "一、", "（一）"
    if not numbering_id and stripped:
        import re as _re
        _NUMBERED_HEADING = _re.compile(
            r"^("
            r"\d+(\.\d+)*\s+\S"                     # 1 X, 1.1 X, 1.1.1 X
            r"|第[一二三四五六七八九十百千万\d]+[章节篇部分]"  # 第一章, 第二节
            r"|[一二三四五六七八九十]+[、．.]\s*\S"     # 一、X, 二、X
            r"|（[一二三四五六七八九十]+）\s*\S"         # （一）X
            r")"
        )
        if _NUMBERED_HEADING.match(stripped):
            return "heading"

    # --- 5. List (Word numbering) ---
    if numbering_id:
        return "list"

    # --- 6. Caption / footnote / citation ---
    if stripped_lower.startswith(("figure ", "fig. ", "图", "表 ")) or "caption" in normalized_id:
        return "caption"
    if "footnote" in normalized_id:
        return "footnote"
    if "bibliograph" in normalized_id or stripped_lower in {"references", "bibliography", "参考文献"}:
        return "citation"

    return "paragraph"


def image_size(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            block_len = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                height = int.from_bytes(data[i + 5 : i + 7], "big")
                width = int.from_bytes(data[i + 7 : i + 9], "big")
                return width, height
            i += 2 + block_len
    return None, None
