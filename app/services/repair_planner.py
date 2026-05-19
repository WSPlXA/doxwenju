import hashlib
import json
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import PatchOperation, PatchPlan, ProviderCall
from app.services.patch_engine import SUPPORTED_OPERATIONS
from app.services.rerank import GEMINI_GENERATE_ENDPOINT

REPAIR_PLAN_SOURCE = "internal_repair_v0"
REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_operation_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["retry", "skip"]},
                    "operation_type": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "source_operation_id",
                    "decision",
                    "operation_type",
                    "rationale",
                ],
            },
        }
    },
    "required": ["operations"],
}
RETRYABLE_SKIP_REASONS = {
    "unsupported_in_patch_engine_v0",
    "header_footer_paragraph_not_found",
    "paragraph_not_found_or_not_top_level",
}


def build_internal_repair_plan(db: Session, previous_plan_id: str) -> PatchPlan | None:
    previous_plan = db.get(PatchPlan, previous_plan_id)
    if previous_plan is None:
        raise ValueError(f"PatchPlan not found: {previous_plan_id}")
    if previous_plan.output_document_version_id is None:
        raise ValueError("Cannot build repair plan before the previous plan has an output")

    skipped_operations = list(
        db.scalars(
            select(PatchOperation)
            .where(PatchOperation.patch_plan_id == previous_plan.id)
            .where(PatchOperation.status == "skipped")
            .order_by(PatchOperation.created_at, PatchOperation.id)
        )
    )
    deterministic_repairable = [
        _repair_operation_data(operation, previous_plan.output_document_version_id)
        for operation in skipped_operations
    ]
    deterministic_repairable = [
        operation for operation in deterministic_repairable if operation is not None
    ]
    repairable, decision_source = _maybe_apply_gemini_repair_decisions(
        db,
        previous_plan.output_document_version_id,
        skipped_operations,
        deterministic_repairable,
    )
    if not repairable:
        return None

    plan = PatchPlan(
        document_version_id=previous_plan.output_document_version_id,
        template_document_version_id=previous_plan.template_document_version_id,
        round_number=previous_plan.round_number + 1,
        status="draft",
        source=REPAIR_PLAN_SOURCE,
        summary={
            "operationCount": len(repairable),
            "sourcePatchPlanId": previous_plan.id,
            "decisionSource": decision_source,
            "status": "internal_repair_draft_not_applied",
            "note": "Internal repair plan only; not a customer-facing review report.",
        },
    )
    db.add(plan)
    db.flush()

    for data in repairable:
        db.add(PatchOperation(patch_plan_id=plan.id, **data))
    db.commit()
    db.refresh(plan)
    return plan


def _maybe_apply_gemini_repair_decisions(
    db: Session,
    document_version_id: str,
    skipped_operations: list[PatchOperation],
    deterministic_repairable: list[dict],
) -> tuple[list[dict], str]:
    if not (
        settings.gemini_api_key
        and settings.gemini_repair_enabled
        and deterministic_repairable
        and skipped_operations
    ):
        return deterministic_repairable, "deterministic_v0"

    prompt = _build_repair_prompt(skipped_operations)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    started = time.perf_counter()
    try:
        response = httpx.post(
            GEMINI_GENERATE_ENDPOINT.format(model=settings.gemini_repair_model),
            headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": REPAIR_SCHEMA,
                },
            },
            timeout=settings.gemini_repair_timeout_seconds,
        )
        response.raise_for_status()
        raw = _extract_json(response.json())
        selected = _validated_repair_operations(raw, deterministic_repairable)
        db.add(
            ProviderCall(
                document_version_id=document_version_id,
                provider="gemini",
                model=settings.gemini_repair_model,
                purpose="internal_repair_plan",
                prompt_hash=prompt_hash,
                output_hash=hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        return selected, "gemini_repair_v0"
    except Exception as exc:
        db.add(
            ProviderCall(
                document_version_id=document_version_id,
                provider="gemini",
                model=settings.gemini_repair_model,
                purpose="internal_repair_plan",
                prompt_hash=prompt_hash,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )
        )
        return deterministic_repairable, "deterministic_fallback_after_gemini_error"


def _build_repair_prompt(skipped_operations: list[PatchOperation]) -> str:
    operations = []
    for operation in skipped_operations[: settings.gemini_repair_max_operations_per_run]:
        operations.append(
            {
                "source_operation_id": operation.id,
                "operation_type": operation.operation_type,
                "risk_level": operation.risk_level,
                "part_name": operation.part_name,
                "xml_path": operation.xml_path,
                "selector": operation.selector,
                "payload": operation.payload,
                "skip_reason": (operation.rationale or {}).get("skipReason"),
                "rationale": operation.rationale,
            }
        )
    return json.dumps(
        {
            "task": "Create an internal DOCX formatting repair plan. Choose only operations that should be retried by the deterministic patch engine. Do not invent XML, do not edit DOCX, and do not select unsupported operation types.",
            "supported_operation_types": sorted(SUPPORTED_OPERATIONS),
            "operations": operations,
            "output_contract": {
                "operations": "array of retry/skip decisions for known source_operation_id values",
                "operation_type": "must be one supported_operation_types value when decision is retry",
            },
        },
        ensure_ascii=False,
        default=str,
    )


def _validated_repair_operations(raw: dict, deterministic_repairable: list[dict]) -> list[dict]:
    by_source_id = {
        operation["rationale"]["repairSourceOperationId"]: operation
        for operation in deterministic_repairable
    }
    selected: list[dict] = []
    for decision in raw.get("operations") or []:
        source_id = str(decision.get("source_operation_id") or "")
        if source_id not in by_source_id or decision.get("decision") != "retry":
            continue
        operation_type = str(decision.get("operation_type") or "")
        if operation_type not in SUPPORTED_OPERATIONS:
            continue
        operation = {**by_source_id[source_id], "operation_type": operation_type}
        operation["payload"] = {**operation["payload"], "action": operation_type}
        operation["rationale"] = {
            **operation["rationale"],
            "geminiRepairRationale": str(decision.get("rationale") or "")[:1000],
        }
        selected.append(operation)
    return selected


def _extract_json(response: dict) -> dict:
    parts = response.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Gemini repair response was not a JSON object")
    return data


def _repair_operation_data(operation: PatchOperation, document_version_id: str) -> dict | None:
    operation_type = operation.operation_type
    if operation_type not in SUPPORTED_OPERATIONS:
        return None

    skip_reason = (operation.rationale or {}).get("skipReason")
    if skip_reason and skip_reason not in RETRYABLE_SKIP_REASONS:
        return None

    return {
        "document_version_id": document_version_id,
        "target_element_id": operation.target_element_id,
        "mapping_result_id": operation.mapping_result_id,
        "profile_rule_id": operation.profile_rule_id,
        "operation_type": operation_type,
        "part_name": operation.part_name,
        "xml_path": operation.xml_path,
        "selector": operation.selector,
        "payload": operation.payload,
        "risk_level": operation.risk_level,
        "status": "planned",
        "rationale": {
            **(operation.rationale or {}),
            "repairSourceOperationId": operation.id,
            "repairStrategy": "retry_after_engine_capability_or_internal_validation",
            "visualOnly": True,
        },
    }
