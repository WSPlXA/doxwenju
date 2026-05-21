"""Unit tests for patch_engine OOXML transformations.

These tests operate directly on XML elements and do not require a database.
"""
from xml.etree import ElementTree as ET

from app.services.ooxml import NS, parse_xml, qn
from app.services.patch_engine import (
    _apply_document_setup_properties,
    _apply_paragraph_properties,
    _apply_table_properties,
    _sectpr_for_operation,
    _serialize_xml,
)
from tests.fixtures.docx_builder import DOCUMENT, STYLES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paragraph(xml: str) -> ET.Element:
    return parse_xml(xml)


def _paragraph_payload(
    style_id: str | None = None,
    effective: dict | None = None,
    run_effective: dict | None = None,
) -> dict:
    props: dict = {}
    if effective:
        props["effective"] = effective
    return {
        "ruleSelector": {"styleId": style_id} if style_id else {},
        "ruleProperties": props,
    }


def _available_style_ids() -> set[str]:
    root = parse_xml(STYLES)
    return {
        style_id
        for style in root.findall("w:style", NS)
        if (style_id := style.attrib.get(qn("w", "styleId")))
    }


# ---------------------------------------------------------------------------
# Paragraph style application
# ---------------------------------------------------------------------------

class TestApplyParagraphStyle:
    def test_sets_pstyle_when_style_exists(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(style_id="Heading1")
        _apply_paragraph_properties(p, payload, _available_style_ids())
        p_pr = p.find("w:pPr", NS)
        assert p_pr is not None
        p_style = p_pr.find("w:pStyle", NS)
        assert p_style is not None
        assert p_style.attrib[qn("w", "val")] == "Heading1"

    def test_ignores_unknown_style_id(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(style_id="NonExistentStyle")
        _apply_paragraph_properties(p, payload, _available_style_ids())
        p_pr = p.find("w:pPr", NS)
        if p_pr is not None:
            assert p_pr.find("w:pStyle", NS) is None

    def test_sets_alignment(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"alignment": "center"})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        jc = p_pr.find("w:jc", NS)
        assert jc is not None
        assert jc.attrib[qn("w", "val")] == "center"

    def test_sets_spacing(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"spacing": {"before": "240", "after": "120"}})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        spacing = p_pr.find("w:spacing", NS)
        assert spacing is not None
        assert spacing.attrib[qn("w", "before")] == "240"

    def test_sets_keep_next(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"keepNext": True})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        assert p_pr.find("w:keepNext", NS) is not None


# ---------------------------------------------------------------------------
# outlineLvl (regression test for the fixed coupling)
# ---------------------------------------------------------------------------

class TestOutlineLvl:
    def test_applies_outline_level_zero(self):
        """outlineLvl=0 (Heading1) must be written — it is falsy so the check must use isinstance."""
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"outlineLvl": 0})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        outline = p_pr.find("w:outlineLvl", NS)
        assert outline is not None
        assert outline.attrib[qn("w", "val")] == "0"

    def test_applies_outline_level_two(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"outlineLvl": 2})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        outline = p_pr.find("w:outlineLvl", NS)
        assert outline.attrib[qn("w", "val")] == "2"

    def test_skips_outline_level_when_absent(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"alignment": "left"})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        assert p_pr.find("w:outlineLvl", NS) is None

    def test_skips_non_int_outline_level(self):
        p = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        payload = _paragraph_payload(effective={"outlineLvl": "0"})
        _apply_paragraph_properties(p, payload, set())
        p_pr = p.find("w:pPr", NS)
        # string "0" should not be applied (only int is accepted)
        assert p_pr.find("w:outlineLvl", NS) is None


# ---------------------------------------------------------------------------
# Table properties
# ---------------------------------------------------------------------------

class TestApplyTableProperties:
    def test_sets_table_width(self):
        tbl = parse_xml(
            '<w:tbl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:tblPr/></w:tbl>"
        )
        payload = {
            "ruleProperties": {
                "representative": {
                    "properties": {"width": {"w": "9360", "type": "dxa"}}
                }
            }
        }
        _apply_table_properties(tbl, payload)
        tbl_pr = tbl.find("w:tblPr", NS)
        tbl_w = tbl_pr.find("w:tblW", NS)
        assert tbl_w is not None
        assert tbl_w.attrib[qn("w", "w")] == "9360"

    def test_noop_when_no_properties(self):
        tbl = parse_xml(
            '<w:tbl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:tblPr/></w:tbl>"
        )
        _apply_table_properties(tbl, {"ruleProperties": {}})
        tbl_pr = tbl.find("w:tblPr", NS)
        assert tbl_pr.find("w:tblW", NS) is None


# ---------------------------------------------------------------------------
# Document setup (new feature)
# ---------------------------------------------------------------------------

class TestApplyDocumentSetup:
    def _make_sectpr(self) -> ET.Element:
        return parse_xml(
            '<w:sectPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
            "</w:sectPr>"
        )

    def test_updates_page_margins(self):
        sectpr = self._make_sectpr()
        payload = {
            "ruleProperties": {
                "pageMargins": {"top": "720", "right": "720", "bottom": "720", "left": "1800"}
            }
        }
        _apply_document_setup_properties(sectpr, payload)
        pg_mar = sectpr.find("w:pgMar", NS)
        assert pg_mar.attrib[qn("w", "top")] == "720"
        assert pg_mar.attrib[qn("w", "left")] == "1800"

    def test_updates_page_size(self):
        sectpr = self._make_sectpr()
        payload = {
            "ruleProperties": {
                "pageSize": {"width": "15840", "height": "12240", "orient": "landscape"}
            }
        }
        _apply_document_setup_properties(sectpr, payload)
        pg_sz = sectpr.find("w:pgSz", NS)
        assert pg_sz.attrib[qn("w", "w")] == "15840"
        assert pg_sz.attrib[qn("w", "orient")] == "landscape"

    def test_creates_missing_pgsz(self):
        sectpr = parse_xml(
            '<w:sectPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
        )
        payload = {"ruleProperties": {"pageSize": {"width": "12240", "height": "15840"}}}
        _apply_document_setup_properties(sectpr, payload)
        pg_sz = sectpr.find("w:pgSz", NS)
        assert pg_sz is not None
        assert pg_sz.attrib[qn("w", "w")] == "12240"

    def test_noop_when_no_rule_properties(self):
        sectpr = self._make_sectpr()
        _apply_document_setup_properties(sectpr, {})
        pg_mar = sectpr.find("w:pgMar", NS)
        assert pg_mar.attrib[qn("w", "top")] == "1440"

    def test_skips_none_values(self):
        sectpr = self._make_sectpr()
        payload = {
            "ruleProperties": {
                "pageMargins": {"top": "720", "right": None, "bottom": "720", "left": "1440"}
            }
        }
        _apply_document_setup_properties(sectpr, payload)
        pg_mar = sectpr.find("w:pgMar", NS)
        assert pg_mar.attrib[qn("w", "top")] == "720"
        # right was None — original value preserved
        assert pg_mar.attrib[qn("w", "right")] == "1440"


# ---------------------------------------------------------------------------
# _sectpr_for_operation
# ---------------------------------------------------------------------------

class TestSectprForOperation:
    class _Op:
        def __init__(self, part_name: str, xml_path: str):
            self.part_name = part_name
            self.xml_path = xml_path

    def test_finds_indexed_sectpr(self):
        root = parse_xml(DOCUMENT)
        op = self._Op("word/document.xml", "/w:document/w:body/w:sectPr[1]")
        sectpr = _sectpr_for_operation(root, op)
        assert sectpr is not None
        assert sectpr.tag == qn("w", "sectPr")

    def test_returns_none_for_wrong_part(self):
        root = parse_xml(DOCUMENT)
        op = self._Op("word/header1.xml", "/w:document/w:body/w:sectPr[1]")
        assert _sectpr_for_operation(root, op) is None

    def test_returns_none_for_out_of_range_index(self):
        root = parse_xml(DOCUMENT)
        op = self._Op("word/document.xml", "/w:document/w:body/w:sectPr[99]")
        assert _sectpr_for_operation(root, op) is None

    def test_accepts_path_without_index(self):
        root = parse_xml(DOCUMENT)
        op = self._Op("word/document.xml", "/w:document/w:body/w:sectPr")
        sectpr = _sectpr_for_operation(root, op)
        assert sectpr is not None


# ---------------------------------------------------------------------------
# XML serialization round-trip
# ---------------------------------------------------------------------------

class TestSerializeXml:
    def test_round_trip_preserves_content(self):
        root = parse_xml(DOCUMENT)
        raw = _serialize_xml(root)
        reparsed = parse_xml(raw)
        paragraphs = reparsed.findall("./w:body/w:p", NS)
        assert len(paragraphs) >= 3

    def test_output_is_valid_utf8_xml(self):
        root = parse_xml(DOCUMENT)
        raw = _serialize_xml(root)
        assert raw.startswith(b"<?xml")
        ET.fromstring(raw)  # must not raise
