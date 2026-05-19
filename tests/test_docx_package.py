import pytest

from app.services.docx_package import DocxSecurityError, inspect_docx_package
from tests.fixtures.docx_builder import build_minimal_docx


def test_inspect_docx_package_accepts_minimal_docx():
    parts = inspect_docx_package(build_minimal_docx())
    names = {part.name for part in parts}
    assert "word/document.xml" in names
    assert "word/styles.xml" in names
    assert "word/media/image1.png" in names


def test_inspect_docx_package_rejects_path_traversal():
    raw = build_minimal_docx({"../evil.xml": "<evil/>"})
    with pytest.raises(DocxSecurityError, match="Unsafe path"):
        inspect_docx_package(raw)


def test_inspect_docx_package_rejects_macro_payload():
    raw = build_minimal_docx({"word/vbaProject.bin": b"macro"})
    with pytest.raises(DocxSecurityError, match="Macro"):
        inspect_docx_package(raw)
