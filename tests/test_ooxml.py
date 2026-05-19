from app.services.ooxml import parse_numbering, parse_relationships, parse_styles
from tests.fixtures.docx_builder import DOCUMENT_RELS, NUMBERING, STYLES


def test_parse_relationships_resolves_relative_targets():
    rels = parse_relationships("word/_rels/document.xml.rels", DOCUMENT_RELS)
    resolved = {rel.relationship_id: rel.resolved_target for rel in rels}
    assert resolved["rIdImage1"] == "word/media/image1.png"
    assert resolved["rIdHeader1"] == "word/header1.xml"


def test_parse_styles_extracts_defaults_and_heading():
    parsed = parse_styles(STYLES)
    assert parsed["docDefaults"]["run"]["fonts"]["ascii"] == "Times New Roman"
    assert parsed["styles"]["Heading1"]["run"]["b"] is True
    assert parsed["styles"]["Heading1"]["run"]["sz"] == 16


def test_parse_numbering_extracts_levels():
    parsed = parse_numbering(NUMBERING)
    assert parsed["nums"]["7"]["abstractNumId"] == "1"
    assert parsed["abstractNums"]["1"]["levels"]["0"]["format"] == "bullet"
