from collections import Counter

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.document import (
    MappingResult,
    PatchOperation,
    PatchPlan,
    ProfileRule,
    TargetElement,
)

PATCH_PLAN_SOURCE = "deterministic_v0"


def rebuild_patch_plan(
    db: Session,
    target_document_version_id: str,
    template_document_version_id: str,
) -> PatchPlan:
    existing_plan_ids = select(PatchPlan.id).where(
        PatchPlan.document_version_id == target_document_version_id
    )
    db.execute(delete(PatchOperation).where(PatchOperation.patch_plan_id.in_(existing_plan_ids)))
    db.execute(delete(PatchPlan).where(PatchPlan.document_version_id == target_document_version_id))
    db.flush()

    rows = db.execute(
        select(MappingResult, TargetElement, ProfileRule)
        .join(TargetElement, MappingResult.target_element_id == TargetElement.id)
        .outerjoin(ProfileRule, MappingResult.profile_rule_id == ProfileRule.id)
        .where(TargetElement.document_version_id == target_document_version_id)
        .order_by(TargetElement.created_at, TargetElement.id)
    ).all()

    operations_data = [
        _operation_from_mapping(mapping, element, rule)
        for mapping, element, rule in rows
        if rule is not None
    ]
    summary = _summary(operations_data, skipped_count=len(rows) - len(operations_data))
    plan = PatchPlan(
        document_version_id=target_document_version_id,
        template_document_version_id=template_document_version_id,
        round_number=1,
        status="draft",
        source=PATCH_PLAN_SOURCE,
        summary=summary,
    )
    db.add(plan)
    db.flush()

    for data in operations_data:
        db.add(
            PatchOperation(
                patch_plan_id=plan.id,
                document_version_id=target_document_version_id,
                target_element_id=data["target_element_id"],
                mapping_result_id=data["mapping_result_id"],
                profile_rule_id=data["profile_rule_id"],
                operation_type=data["operation_type"],
                part_name=data["part_name"],
                xml_path=data["xml_path"],
                selector=data["selector"],
                payload=data["payload"],
                risk_level=data["risk_level"],
                status="planned",
                rationale=data["rationale"],
            )
        )
    db.commit()
    db.refresh(plan)
    return plan


def _operation_from_mapping(
    mapping: MappingResult, element: TargetElement, rule: ProfileRule
) -> dict:
    operation_type = _operation_type(element, rule)
    risk_level = _risk_level(element, rule)
    return {
        "target_element_id": element.id,
        "mapping_result_id": mapping.id,
        "profile_rule_id": rule.id,
        "operation_type": operation_type,
        "part_name": element.part_name,
        "xml_path": element.xml_path,
        "selector": {
            "partName": element.part_name,
            "xmlPath": element.xml_path,
            "elementType": element.element_type,
            "elementCategory": element.element_category,
            "styleId": element.style_id,
            "numberingId": element.numbering_id,
        },
        "payload": {
            "action": operation_type,
            "profileRuleId": rule.id,
            "ruleType": rule.rule_type,
            "ruleName": rule.name,
            "ruleSelector": rule.selector,
            "ruleProperties": rule.properties,
            "patchEngineHints": _patch_engine_hints(element, rule),
        },
        "risk_level": risk_level,
        "rationale": {
            "mappingStrategy": mapping.strategy,
            "mappingScore": mapping.score,
            "mappingRationale": mapping.rationale,
            "confidence": rule.confidence,
            "visualOnly": True,
        },
    }


def _operation_type(element: TargetElement, rule: ProfileRule) -> str:
    category = element.element_category or rule.element_category
    if category == "document_setup" or rule.rule_type == "document":
        return "apply_document_setup_rule"
    if category == "heading" or rule.rule_type == "heading":
        return "apply_heading_rule"
    if category == "list" or rule.rule_type == "list":
        return "apply_list_rule"
    if category == "table" or rule.rule_type == "table":
        return "apply_table_rule"
    if category == "image" or rule.rule_type == "image":
        return "apply_image_rule"
    if category in {"footnote", "endnote"}:
        return f"apply_{category}_rule"
    if category == "header_footer" or rule.rule_type == "header_footer":
        return "apply_header_footer_rule"
    if category in {"caption", "citation"}:
        return f"apply_{category}_rule"
    return "apply_paragraph_rule"


def _risk_level(element: TargetElement, rule: ProfileRule) -> str:
    category = element.element_category or rule.element_category
    if category in {"document_setup", "header_footer", "footnote", "endnote"}:
        return "P1"
    if category in {"heading", "table", "image"}:
        return "P2"
    return "P3"


def _patch_engine_hints(element: TargetElement, rule: ProfileRule) -> dict:
    category = element.element_category or rule.element_category
    hints = {
        "preferStyleReference": True,
        "allowDirectFormatting": True,
        "preserveVisibleText": True,
        "doNotExecuteFieldCodes": True,
    }
    if category in {"table", "image", "header_footer", "footnote", "endnote"}:
        hints["requiresSpecializedPatch"] = True
    if rule.selector.get("styleId"):
        hints["templateStyleId"] = rule.selector["styleId"]
    if rule.selector.get("numberingId"):
        hints["templateNumberingId"] = rule.selector["numberingId"]
    return hints


def _summary(operations: list[dict], skipped_count: int) -> dict:
    by_type = Counter(operation["operation_type"] for operation in operations)
    by_risk = Counter(operation["risk_level"] for operation in operations)
    return {
        "operationCount": len(operations),
        "skippedMappings": skipped_count,
        "operationTypes": dict(by_type),
        "riskLevels": dict(by_risk),
        "status": "draft_only_not_applied",
    }
