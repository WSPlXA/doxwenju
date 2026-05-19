import hashlib
import posixpath
import zipfile
from dataclasses import dataclass
from io import BytesIO

from defusedxml import ElementTree as DET

from app.core.config import settings


class DocxSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class PackagePart:
    name: str
    data: bytes
    content_type: str | None

    @property
    def is_xml(self) -> bool:
        return self.name.endswith(".xml") or (self.content_type or "").endswith("+xml")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _is_path_safe(name: str) -> bool:
    normalized = posixpath.normpath(name)
    return not (
        name.startswith("/")
        or name.startswith("\\")
        or normalized.startswith("../")
        or "/../" in normalized
        or "\\" in name
    )


def _parse_content_types(data: bytes) -> dict[str, str]:
    root = DET.fromstring(data)
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "Default":
            defaults[child.attrib.get("Extension", "").lower()] = child.attrib.get(
                "ContentType", ""
            )
        elif tag == "Override":
            part = child.attrib.get("PartName", "").lstrip("/")
            overrides[part] = child.attrib.get("ContentType", "")
    return {"defaults": defaults, "overrides": overrides}  # type: ignore[return-value]


def _content_type_for(name: str, content_types: dict) -> str | None:
    override = content_types.get("overrides", {}).get(name)
    if override:
        return override
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return content_types.get("defaults", {}).get(extension)


def inspect_docx_package(raw_file: bytes) -> list[PackagePart]:
    if len(raw_file) > settings.max_docx_bytes:
        raise DocxSecurityError("DOCX exceeds configured size limit")

    try:
        zf = zipfile.ZipFile(BytesIO(raw_file))
    except zipfile.BadZipFile as exc:
        raise DocxSecurityError("Uploaded file is not a valid ZIP/DOCX package") from exc

    with zf:
        infos = zf.infolist()
        if not infos:
            raise DocxSecurityError("DOCX package is empty")

        total_uncompressed = 0
        for info in infos:
            if not _is_path_safe(info.filename):
                raise DocxSecurityError(f"Unsafe path in DOCX package: {info.filename}")
            total_uncompressed += info.file_size
            compressed = max(info.compress_size, 1)
            if info.file_size / compressed > settings.max_zip_compression_ratio:
                raise DocxSecurityError(f"Suspicious compression ratio for {info.filename}")
            if info.filename == "word/vbaProject.bin":
                raise DocxSecurityError("Macro-enabled DOCX content is not accepted")

        if total_uncompressed > settings.max_zip_uncompressed_bytes:
            raise DocxSecurityError("DOCX uncompressed size exceeds configured limit")
        if "[Content_Types].xml" not in zf.namelist():
            raise DocxSecurityError("DOCX is missing [Content_Types].xml")
        if "word/document.xml" not in zf.namelist():
            raise DocxSecurityError("DOCX is missing word/document.xml")

        content_types = _parse_content_types(zf.read("[Content_Types].xml"))
        parts: list[PackagePart] = []
        for info in infos:
            if info.is_dir():
                continue
            data = zf.read(info.filename)
            parts.append(
                PackagePart(
                    name=info.filename,
                    data=data,
                    content_type=_content_type_for(info.filename, content_types),
                )
            )
        return parts
