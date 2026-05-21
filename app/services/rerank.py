import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import ProviderCall, TargetElement

# Max concurrent Gemini rerank threads — keeps us within free-tier rate limits
_RERANK_CONCURRENCY = 6

GEMINI_GENERATE_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_profile_rule_id": {"type": "string"},
        "confidence": {"type": "integer"},
        "rationale": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["selected_profile_rule_id", "confidence", "rationale", "risk_flags"],
}


class RerankError(RuntimeError):
    pass


@dataclass(frozen=True)
class RerankDecision:
    selected_profile_rule_id: str
    confidence: int
    rationale: str
    risk_flags: list[str]
    raw: dict


class GeminiRerankProvider:
    provider = "gemini"
    purpose = "mapping_rerank"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def rerank(self, target: TargetElement, candidates: list[dict]) -> RerankDecision:
        if not candidates:
            raise RerankError("Cannot rerank empty candidates")
        prompt = _build_prompt(target, candidates)
        response = httpx.post(
            GEMINI_GENERATE_ENDPOINT.format(model=self.model),
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": RERANK_SCHEMA,
                },
            },
            timeout=settings.gemini_rerank_timeout_seconds,
        )
        if response.status_code >= 400:
            raise RerankError(f"Gemini rerank request failed: HTTP {response.status_code}")
        data = _extract_json(response.json())
        candidate_ids = {candidate["rule"].id for candidate in candidates}
        selected = str(data.get("selected_profile_rule_id", ""))
        if selected not in candidate_ids:
            raise RerankError("Gemini selected a rule outside the candidate set")
        confidence = int(data.get("confidence", 0))
        if not 0 <= confidence <= 100:
            raise RerankError("Gemini confidence is outside 0..100")
        risk_flags = data.get("risk_flags") or []
        if not isinstance(risk_flags, list):
            raise RerankError("Gemini risk_flags is not a list")
        return RerankDecision(
            selected_profile_rule_id=selected,
            confidence=confidence,
            rationale=str(data.get("rationale", ""))[:1000],
            risk_flags=[str(flag)[:120] for flag in risk_flags[:8]],
            raw=data,
        )


def maybe_rerank_candidates(
    db: Session,
    document_version_id: str,
    target: TargetElement,
    candidates: list[dict],
) -> tuple[dict | None, dict | None]:
    if not _rerank_available(candidates):
        return (candidates[0] if candidates else None), None

    provider = GeminiRerankProvider(
        api_key=settings.gemini_api_key or "",
        model=settings.gemini_rerank_model,
    )
    prompt_hash = hashlib.sha256(_build_prompt(target, candidates).encode("utf-8")).hexdigest()
    started = time.perf_counter()
    try:
        decision = provider.rerank(target, candidates)
        selected = next(
            candidate
            for candidate in candidates
            if candidate["rule"].id == decision.selected_profile_rule_id
        )
        call = ProviderCall(
            document_version_id=document_version_id,
            provider=provider.provider,
            model=provider.model,
            purpose=provider.purpose,
            prompt_hash=prompt_hash,
            output_hash=hashlib.sha256(
                json.dumps(decision.raw, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        db.add(call)
        return selected, {
            "provider": provider.provider,
            "model": provider.model,
            "selectedProfileRuleId": decision.selected_profile_rule_id,
            "confidence": decision.confidence,
            "rationale": decision.rationale,
            "riskFlags": decision.risk_flags,
        }
    except Exception as exc:
        db.add(
            ProviderCall(
                document_version_id=document_version_id,
                provider=provider.provider,
                model=provider.model,
                purpose=provider.purpose,
                prompt_hash=prompt_hash,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )
        )
        return candidates[0] if candidates else None, {"error": str(exc), "fallback": "hybrid_top1"}


def rerank_candidates_parallel(
    db: Session,
    document_version_id: str,
    elements_with_candidates: list[tuple["TargetElement", list[dict]]],
) -> list[tuple[dict | None, dict | None]]:
    """Rerank multiple elements concurrently.

    Runs all Gemini HTTP calls in a thread pool (I/O bound, no DB access
    inside threads), then writes ProviderCall records back in the main
    thread after all calls complete.

    Returns a list of (selected_candidate, rerank_info) in the same
    order as *elements_with_candidates*.
    """
    if not (settings.gemini_api_key and settings.gemini_rerank_enabled):
        return [
            (candidates[0] if candidates else None, None)
            for _, candidates in elements_with_candidates
        ]

    provider = GeminiRerankProvider(
        api_key=settings.gemini_api_key or "",
        model=settings.gemini_rerank_model,
    )

    # --- Phase 1: parallel HTTP calls (no DB) ---
    def _call(idx: int, target: "TargetElement", candidates: list[dict]):
        if not candidates:
            return idx, None, None, None
        prompt = _build_prompt(target, candidates)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        started = time.perf_counter()
        try:
            decision = provider.rerank(target, candidates)
            selected = next(
                c for c in candidates if c["rule"].id == decision.selected_profile_rule_id
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            rerank_info = {
                "provider": provider.provider,
                "model": provider.model,
                "selectedProfileRuleId": decision.selected_profile_rule_id,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "riskFlags": decision.risk_flags,
            }
            output_hash = hashlib.sha256(
                json.dumps(decision.raw, sort_keys=True).encode()
            ).hexdigest()
            return idx, selected, rerank_info, {
                "prompt_hash": prompt_hash,
                "output_hash": output_hash,
                "duration_ms": duration_ms,
                "error_message": None,
            }
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            fallback = candidates[0] if candidates else None
            return idx, fallback, {"error": str(exc), "fallback": "hybrid_top1"}, {
                "prompt_hash": prompt_hash,
                "output_hash": None,
                "duration_ms": duration_ms,
                "error_message": str(exc),
            }

    results: list[tuple[dict | None, dict | None]] = [(None, None)] * len(elements_with_candidates)
    call_records: list[dict] = []

    with ThreadPoolExecutor(max_workers=_RERANK_CONCURRENCY) as pool:
        futures = {
            pool.submit(_call, idx, target, candidates): idx
            for idx, (target, candidates) in enumerate(elements_with_candidates)
            if candidates
        }
        for future in as_completed(futures):
            idx, selected, rerank_info, call_meta = future.result()
            results[idx] = (selected, rerank_info)
            if call_meta is not None:
                call_records.append({"idx": idx, **call_meta})

    # --- Phase 2: write ProviderCall records in main thread ---
    for rec in call_records:
        db.add(
            ProviderCall(
                document_version_id=document_version_id,
                provider=provider.provider,
                model=provider.model,
                purpose=provider.purpose,
                prompt_hash=rec["prompt_hash"],
                output_hash=rec.get("output_hash"),
                duration_ms=rec["duration_ms"],
                error_message=rec.get("error_message"),
            )
        )

    return results


def _rerank_available(candidates: list[dict]) -> bool:
    return bool(settings.gemini_api_key and settings.gemini_rerank_enabled and candidates)


def _build_prompt(target: TargetElement, candidates: list[dict]) -> str:
    payload = {
        "task": "Choose the best single template formatting rule for the target DOCX element. Prefer visual-format category and structure over semantic content. Do not invent rule ids.",
        "target_element": {
            "id": target.id,
            "element_type": target.element_type,
            "element_category": target.element_category,
            "style_id": target.style_id,
            "numbering_id": target.numbering_id,
            "part_name": target.part_name,
            "xml_path": target.xml_path,
            "text_summary": (target.text_summary or "")[:500],
            "normalized": _compact(target.normalized),
        },
        "candidates": [_candidate_payload(candidate) for candidate in candidates[:10]],
        "output_contract": {
            "selected_profile_rule_id": "must be one candidate profile_rule_id",
            "confidence": "integer 0-100",
            "rationale": "brief reason based on structure/category/format",
            "risk_flags": "short list, empty when low risk",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _candidate_payload(candidate: dict) -> dict:
    rule = candidate["rule"]
    return {
        "profile_rule_id": rule.id,
        "rule_type": rule.rule_type,
        "element_category": rule.element_category,
        "name": rule.name,
        "priority": rule.priority,
        "selector": rule.selector,
        "properties": _compact(rule.properties),
        "hybrid_score": candidate["score"],
        "structural_score": candidate["structuralScore"],
        "vector_score": candidate["vectorScore"],
        "keyword_score": candidate["keywordScore"],
        "reasons": candidate["reasons"],
    }


def _compact(value: Any) -> Any:
    text = json.dumps(value or {}, ensure_ascii=False, default=str)
    if len(text) <= 2000:
        return value or {}
    return {"truncated_json": text[:2000]}


def _extract_json(response: dict) -> dict:
    parts = response.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts)
    text = _strip_fences(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RerankError("Gemini rerank response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise RerankError("Gemini rerank response was not a JSON object")
    return data


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped
