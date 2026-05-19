from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.document import (
    DocumentVersion,
    FormatAtom,
    FormatProfile,
    MappingResult,
    MediaAsset,
    OOXMLPart,
    ProfileRule,
    Relationship,
    TargetElement,
)
from app.services.atoms import body_atoms, document_setup_atoms, header_footer_atoms, note_atoms
from app.services.docx_package import DocxSecurityError, PackagePart, inspect_docx_package
from app.services.ooxml import image_size, parse_numbering, parse_relationships, parse_styles
from app.services.profile_builder import rebuild_deterministic_profile


def ingest_template_version(db: Session, version: DocumentVersion) -> None:
    try:
        _set_status(db, version, "parsing", 5)
        parts = inspect_docx_package(version.raw_file)
        part_by_name = {part.name: part for part in parts}

        _clear_existing_parse(db, version.id)
        _store_parts(db, version.id, parts)
        _set_status(db, version, "parsing", 30)

        _store_relationships(db, version.id, parts)
        _store_media(db, version.id, parts)
        _set_status(db, version, "parsing", 50)

        styles_info = parse_styles(_part_text(part_by_name.get("word/styles.xml")))
        numbering_info = parse_numbering(_part_text(part_by_name.get("word/numbering.xml")))
        atoms = _build_atoms(part_by_name, styles_info, numbering_info)
        _store_atoms(db, version.id, atoms)
        if version.document.kind == "template":
            rebuild_deterministic_profile(db, version.id, f"{version.filename} profile")
        _set_status(db, version, "done", 100)
    except (DocxSecurityError, ValueError) as exc:
        _set_status(db, version, "failed", version.progress, str(exc))
        raise
    except Exception as exc:
        _set_status(db, version, "failed", version.progress, f"Unexpected ingestion failure: {exc}")
        raise


def _set_status(
    db: Session,
    version: DocumentVersion,
    status: str,
    progress: int,
    error_message: str | None = None,
) -> None:
    version.status = status
    version.progress = progress
    version.error_message = error_message
    db.add(version)
    db.commit()
    db.refresh(version)


def _clear_existing_parse(db: Session, version_id: str) -> None:
    for model in (
        MappingResult,
        TargetElement,
        ProfileRule,
        FormatProfile,
        FormatAtom,
        MediaAsset,
        Relationship,
        OOXMLPart,
    ):
        db.execute(delete(model).where(model.document_version_id == version_id))
    db.commit()


def _store_parts(db: Session, version_id: str, parts: list[PackagePart]) -> None:
    for part in parts:
        xml_text = None
        binary_data = None
        if part.is_xml:
            xml_text = part.data.decode("utf-8", errors="replace")
        else:
            binary_data = part.data
        db.add(
            OOXMLPart(
                document_version_id=version_id,
                part_name=part.name,
                content_type=part.content_type,
                is_xml=part.is_xml,
                size_bytes=len(part.data),
                sha256=part.sha256,
                xml_text=xml_text,
                binary_data=binary_data,
                parsed_summary=_part_summary(part),
            )
        )
    db.commit()


def _store_relationships(db: Session, version_id: str, parts: list[PackagePart]) -> None:
    for part in parts:
        if not part.name.endswith(".rels"):
            continue
        xml_text = part.data.decode("utf-8", errors="replace")
        for parsed in parse_relationships(part.name, xml_text):
            db.add(
                Relationship(
                    document_version_id=version_id,
                    source_part=parsed.source_part,
                    relationship_id=parsed.relationship_id,
                    relationship_type=parsed.relationship_type,
                    target=parsed.target,
                    target_mode=parsed.target_mode,
                    resolved_target=parsed.resolved_target,
                )
            )
    db.commit()


def _store_media(db: Session, version_id: str, parts: list[PackagePart]) -> None:
    for part in parts:
        if not part.name.startswith("word/media/"):
            continue
        width, height = image_size(part.data)
        db.add(
            MediaAsset(
                document_version_id=version_id,
                part_name=part.name,
                content_type=part.content_type,
                size_bytes=len(part.data),
                sha256=part.sha256,
                width_px=width,
                height_px=height,
                data=part.data,
            )
        )
    db.commit()


def _store_atoms(db: Session, version_id: str, atoms: list[dict]) -> None:
    for atom in atoms:
        db.add(
            FormatAtom(
                document_version_id=version_id,
                atom_type=atom["atom_type"],
                part_name=atom["part_name"],
                xml_path=atom.get("xml_path"),
                raw_xml=atom.get("raw_xml"),
                normalized=atom.get("normalized") or {},
                text_summary=atom.get("text_summary"),
                element_category=atom.get("element_category"),
                page_context=atom.get("page_context"),
                style_id=atom.get("style_id"),
                numbering_id=atom.get("numbering_id"),
                relationship_id=atom.get("relationship_id"),
                render_metrics=atom.get("render_metrics"),
                embedding=None,
            )
        )
    db.commit()


def _build_atoms(
    part_by_name: dict[str, PackagePart], styles_info: dict, numbering_info: dict
) -> list[dict]:
    atoms: list[dict] = []
    document_xml = _part_text(part_by_name.get("word/document.xml"))
    if document_xml:
        atoms.extend(document_setup_atoms(document_xml, "word/document.xml"))
        atoms.extend(body_atoms(document_xml, styles_info, numbering_info, "word/document.xml"))

    for name, part in sorted(part_by_name.items()):
        if name.startswith("word/header") and name.endswith(".xml"):
            atoms.extend(header_footer_atoms(_part_text(part), styles_info, numbering_info, name))
        elif name.startswith("word/footer") and name.endswith(".xml"):
            atoms.extend(header_footer_atoms(_part_text(part), styles_info, numbering_info, name))
        elif name == "word/footnotes.xml":
            atoms.extend(
                note_atoms(_part_text(part), styles_info, numbering_info, name, "footnote")
            )
        elif name == "word/endnotes.xml":
            atoms.extend(note_atoms(_part_text(part), styles_info, numbering_info, name, "endnote"))
    return atoms


def _part_text(part: PackagePart | None) -> str | None:
    if part is None:
        return None
    return part.data.decode("utf-8", errors="replace")


def _part_summary(part: PackagePart) -> dict:
    if part.name == "[Content_Types].xml":
        return {"role": "content_types"}
    if part.name.endswith(".rels"):
        return {"role": "relationships"}
    if part.name == "word/document.xml":
        return {"role": "main_document"}
    if part.name.startswith("word/header"):
        return {"role": "header"}
    if part.name.startswith("word/footer"):
        return {"role": "footer"}
    if part.name.startswith("word/media/"):
        return {"role": "media"}
    if part.name in {
        "word/styles.xml",
        "word/numbering.xml",
        "word/settings.xml",
        "word/fontTable.xml",
    }:
        return {"role": part.name.removeprefix("word/").removesuffix(".xml")}
    return {}
