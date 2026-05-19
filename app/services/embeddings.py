import hashlib
import math
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import FormatAtom, ProviderCall, TargetElement

GEMINI_EMBED_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
)


class EmbeddingError(RuntimeError):
    pass


class GeminiEmbeddingProvider:
    provider = "gemini"
    purpose = "format_atom_embedding"

    def __init__(self, api_key: str, model: str, dimensions: int) -> None:
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions

    def embed_text(self, text: str) -> list[float]:
        if not text.strip():
            raise EmbeddingError("Cannot embed empty text")
        url = GEMINI_EMBED_ENDPOINT.format(model=self.model)
        payload = {
            "content": {"parts": [{"text": text[:12000]}]},
            "output_dimensionality": self.dimensions,
        }
        response = httpx.post(
            url,
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=settings.gemini_embedding_timeout_seconds,
        )
        if response.status_code >= 400:
            raise EmbeddingError(f"Gemini embedding request failed: HTTP {response.status_code}")
        data = response.json()
        values = _extract_values(data)
        if len(values) != self.dimensions:
            raise EmbeddingError(
                f"Gemini embedding dimension mismatch: expected {self.dimensions}, got {len(values)}"
            )
        return _normalize(values)


def embed_format_atoms(db: Session, document_version_id: str) -> dict[str, int]:
    return _embed_records(
        db=db,
        document_version_id=document_version_id,
        provider_purpose="format_atom_embedding",
        records=list(
            db.scalars(
                select(FormatAtom)
                .where(FormatAtom.document_version_id == document_version_id)
                .where(FormatAtom.embedding.is_(None))
                .order_by(FormatAtom.created_at, FormatAtom.id)
                .limit(settings.gemini_embedding_max_atoms_per_document)
            )
        ),
        text_builder=_format_atom_embedding_text,
    )


def embed_target_elements(db: Session, document_version_id: str) -> dict[str, int]:
    return _embed_records(
        db=db,
        document_version_id=document_version_id,
        provider_purpose="target_element_embedding",
        records=list(
            db.scalars(
                select(TargetElement)
                .where(TargetElement.document_version_id == document_version_id)
                .where(TargetElement.embedding.is_(None))
                .order_by(TargetElement.created_at, TargetElement.id)
                .limit(settings.gemini_embedding_max_atoms_per_document)
            )
        ),
        text_builder=_target_element_embedding_text,
    )


def _embed_records(
    db: Session,
    document_version_id: str,
    provider_purpose: str,
    records: list[FormatAtom | TargetElement],
    text_builder,
) -> dict[str, int]:
    if not settings.gemini_api_key:
        return {"embedded": 0, "skipped": 0, "failed": 0}

    provider = GeminiEmbeddingProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_embedding_model,
        dimensions=settings.gemini_embedding_dimensions,
    )
    counts = {"embedded": 0, "skipped": 0, "failed": 0}
    for record in records:
        text = text_builder(record)
        if not text.strip():
            counts["skipped"] += 1
            continue
        started = time.perf_counter()
        try:
            record.embedding = provider.embed_text(text)
            db.add(
                ProviderCall(
                    document_version_id=document_version_id,
                    provider=provider.provider,
                    model=provider.model,
                    purpose=provider_purpose,
                    prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    output_hash=_embedding_hash(record.embedding),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            counts["embedded"] += 1
        except Exception as exc:
            counts["failed"] += 1
            db.add(
                ProviderCall(
                    document_version_id=document_version_id,
                    provider=provider.provider,
                    model=provider.model,
                    purpose=provider_purpose,
                    prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error_message=str(exc),
                )
            )
        db.commit()
    return counts


def _format_atom_embedding_text(atom: FormatAtom) -> str:
    normalized = atom.normalized or {}
    parts = [
        f"atom_type: {atom.atom_type}",
        f"element_category: {atom.element_category or ''}",
        f"style_id: {atom.style_id or ''}",
        f"numbering_id: {atom.numbering_id or ''}",
        f"part_name: {atom.part_name}",
        f"xml_path: {atom.xml_path or ''}",
        f"text: {atom.text_summary or ''}",
        f"format: {_compact_jsonish(normalized)}",
    ]
    return "\n".join(parts)


def _target_element_embedding_text(element: TargetElement) -> str:
    normalized = element.normalized or {}
    parts = [
        f"element_type: {element.element_type}",
        f"element_category: {element.element_category or ''}",
        f"style_id: {element.style_id or ''}",
        f"numbering_id: {element.numbering_id or ''}",
        f"part_name: {element.part_name}",
        f"xml_path: {element.xml_path or ''}",
        f"text: {element.text_summary or ''}",
        f"format: {_compact_jsonish(normalized)}",
    ]
    return "\n".join(parts)


def _extract_values(data: dict[str, Any]) -> list[float]:
    if "embedding" in data:
        values = data["embedding"].get("values")
        if values is not None:
            return [float(value) for value in values]
    embeddings = data.get("embeddings")
    if embeddings:
        values = embeddings[0].get("values")
        if values is not None:
            return [float(value) for value in values]
    raise EmbeddingError("Gemini embedding response did not include values")


def _normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


def _embedding_hash(values: list[float]) -> str:
    payload = ",".join(f"{value:.8f}" for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compact_jsonish(value: Any) -> str:
    text = repr(value)
    return text[:2000]
