from app.services.atoms import body_atoms, document_setup_atoms, header_footer_atoms, note_atoms
from app.services.ooxml import parse_numbering, parse_styles
from tests.fixtures.docx_builder import DOCUMENT, FOOTNOTES, HEADER, NUMBERING, STYLES


def test_body_atoms_cover_core_atom_types():
    styles = parse_styles(STYLES)
    numbering = parse_numbering(NUMBERING)
    atoms = body_atoms(DOCUMENT, styles, numbering, "word/document.xml")
    atom_types = {atom["atom_type"] for atom in atoms}

    assert {"heading", "paragraph", "list", "run", "table", "image"}.issubset(atom_types)
    heading = next(atom for atom in atoms if atom["atom_type"] == "heading")
    assert heading["style_id"] == "Heading1"
    assert heading["normalized"]["effective"]["keepNext"] is True


def test_document_setup_atom_extracts_page_margins():
    atoms = document_setup_atoms(DOCUMENT, "word/document.xml")
    assert len(atoms) == 1
    assert atoms[0]["normalized"]["pageMargins"]["top"] == "1440"


def test_note_and_header_atoms_are_created():
    styles = parse_styles(STYLES)
    numbering = parse_numbering(NUMBERING)
    footnote_atoms = note_atoms(FOOTNOTES, styles, numbering, "word/footnotes.xml", "footnote")
    header_atoms = header_footer_atoms(HEADER, styles, numbering, "word/header1.xml")

    assert any(atom["atom_type"] == "footnote" for atom in footnote_atoms)
    assert any(atom["atom_type"] == "header_footer" for atom in header_atoms)
