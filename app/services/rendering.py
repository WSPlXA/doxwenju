import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import DocumentVersion, RenderSnapshot

PDF_MIME_HEADER = b"%PDF-"


def render_libreoffice_precheck(db: Session, document_version_id: str) -> RenderSnapshot:
    version = db.get(DocumentVersion, document_version_id)
    if version is None:
        raise ValueError(f"DocumentVersion not found: {document_version_id}")

    executable = _libreoffice_executable()
    if executable is None:
        snapshot = RenderSnapshot(
            document_version_id=document_version_id,
            renderer="libreoffice",
            renderer_version=None,
            status="skipped",
            pdf_data=None,
            pdf_size_bytes=None,
            page_count=None,
            metrics={"reason": "libreoffice_not_found"},
            error_message="LibreOffice/soffice executable was not found",
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return snapshot

    with tempfile.TemporaryDirectory(prefix="doxwenju-render-") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / _safe_filename(version.filename)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        input_path.write_bytes(version.raw_file)

        renderer_version = _renderer_version(executable)
        command = [
            executable,
            "--headless",
            "--convert-to",
            "pdf",
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
        except subprocess.TimeoutExpired as exc:
            snapshot = _failed_snapshot(
                document_version_id,
                renderer_version,
                f"LibreOffice timed out after {settings.libreoffice_timeout_seconds}s: {exc}",
            )
            db.add(snapshot)
            db.commit()
            db.refresh(snapshot)
            return snapshot

        pdf_path = output_dir / f"{input_path.stem}.pdf"
        if completed.returncode != 0 or not pdf_path.exists():
            snapshot = _failed_snapshot(
                document_version_id,
                renderer_version,
                (
                    f"LibreOffice conversion failed with code {completed.returncode}. "
                    f"stdout={completed.stdout[-1000:]} stderr={completed.stderr[-1000:]}"
                ),
            )
            db.add(snapshot)
            db.commit()
            db.refresh(snapshot)
            return snapshot

        pdf_data = pdf_path.read_bytes()
        status = "done" if pdf_data.startswith(PDF_MIME_HEADER) else "failed"
        snapshot = RenderSnapshot(
            document_version_id=document_version_id,
            renderer="libreoffice",
            renderer_version=renderer_version,
            status=status,
            pdf_data=pdf_data if status == "done" else None,
            pdf_size_bytes=len(pdf_data) if status == "done" else None,
            page_count=_pdf_page_count(pdf_data) if status == "done" else None,
            metrics={
                "stdoutTail": completed.stdout[-1000:],
                "stderrTail": completed.stderr[-1000:],
                "command": [command[0], *command[1:4], "..."],
            },
            error_message=None if status == "done" else "LibreOffice output was not a PDF",
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return snapshot


def latest_render_snapshot(db: Session, document_version_id: str) -> RenderSnapshot | None:
    return db.scalar(
        select(RenderSnapshot)
        .where(RenderSnapshot.document_version_id == document_version_id)
        .order_by(RenderSnapshot.created_at.desc())
        .limit(1)
    )


def _libreoffice_executable() -> str | None:
    if settings.libreoffice_path:
        path = Path(settings.libreoffice_path)
        return str(path) if path.exists() else None
    for candidate in (
        "libreoffice",
        "soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if resolved and Path(resolved).exists():
            return resolved
    return None


def _renderer_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    version = (completed.stdout or completed.stderr).strip()
    return version[:255] if version else None


def _failed_snapshot(
    document_version_id: str, renderer_version: str | None, error_message: str
) -> RenderSnapshot:
    return RenderSnapshot(
        document_version_id=document_version_id,
        renderer="libreoffice",
        renderer_version=renderer_version,
        status="failed",
        pdf_data=None,
        pdf_size_bytes=None,
        page_count=None,
        metrics=None,
        error_message=error_message,
    )


def _safe_filename(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "document.docx"
    return stem if stem.lower().endswith(".docx") else f"{stem}.docx"


def _pdf_page_count(pdf_data: bytes) -> int | None:
    matches = re.findall(rb"/Type\s*/Page\b", pdf_data)
    if matches:
        return len(matches)
    return None
