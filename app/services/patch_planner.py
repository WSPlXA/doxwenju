import re
from collections import Counter

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.document import (
    MappingResult,
    PatchExecution,
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
    executed_plan_ids = select(PatchExecution.patch_plan_id).where(
        PatchExecution.document_version_id == target_document_version_id
    )
    stale_draft_plan_ids = select(PatchPlan.id).where(
        PatchPlan.document_version_id == target_document_version_id,
        PatchPlan.status == "draft",
        PatchPlan.id.not_in(executed_plan_ids),
    )
    db.execute(delete(PatchOperation).where(PatchOperation.patch_plan_id.in_(stale_draft_plan_ids)))
    db.execute(
        delete(PatchPlan).where(
            PatchPlan.document_version_id == target_document_version_id,
            PatchPlan.status == "draft",
            PatchPlan.id.not_in(executed_plan_ids),
        )
    )
    db.flush()

    rows = db.execute(
        select(MappingResult, TargetElement, ProfileRule)
        .join(TargetElement, MappingResult.target_element_id == TargetElement.id)
        .outerjoin(ProfileRule, MappingResult.profile_rule_id == ProfileRule.id)
        .where(TargetElement.document_version_id == target_document_version_id)
    ).all()
    rows = _dedupe_mapping_rows(sorted(rows, key=lambda row: _document_order_key(row[1])))
    rules = list(
        db.scalars(
            select(ProfileRule).where(
                ProfileRule.document_version_id == template_document_version_id
            )
        )
    )
    elements = [element for _, element, _ in rows]
    thesis_mode = _looks_like_thesis_target(elements)
    thesis_context = _thesis_context(elements) if thesis_mode else {}

    operations_data = [
        _operation_from_mapping(
            mapping,
            element,
            _rule_for_element(element, rule, rules, thesis_context) if thesis_mode else rule,
        )
        for mapping, element, rule in rows
        if (not thesis_mode and rule is not None)
        or (thesis_mode and _rule_for_element(element, rule, rules, thesis_context) is not None)
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


def _dedupe_mapping_rows(rows: list[tuple[MappingResult, TargetElement, ProfileRule | None]]):
    seen = set()
    unique = []
    for row in rows:
        element_id = row[1].id
        if element_id in seen:
            continue
        seen.add(element_id)
        unique.append(row)
    return unique


def _looks_like_thesis_target(elements: list[TargetElement]) -> bool:
    texts = {(_text(element)).replace(" ", "") for element in elements}
    return (
        ("目录" in texts or "6、目录" in texts)
        and "参考文献" in texts
        and any("学术承诺" in text for text in texts)
    )


def _thesis_context(elements: list[TargetElement]) -> dict:
    top_level = [element for element in elements if _top_level_paragraph_index(element.xml_path)]
    body_starts = [
        _top_level_paragraph_index(element.xml_path)
        for element in top_level
        if _is_chapter_heading(_text(element))
    ]
    reference_starts = [
        _top_level_paragraph_index(element.xml_path)
        for element in top_level
        if _text(element) == "参考文献"
    ]
    return {
        "bodyStart": min([index for index in body_starts if index is not None], default=1_000_000),
        "referenceStart": min(
            [index for index in reference_starts if index is not None], default=1_000_000
        ),
    }


def _rule_for_element(
    element: TargetElement,
    mapped_rule: ProfileRule | None,
    rules: list[ProfileRule],
    context: dict,
) -> ProfileRule | None:
    if element.element_category in {"footnote", "endnote"}:
        return None
    if element.element_category == "header_footer":
        return None
    if element.element_category in {"document_setup", "image", "table"}:
        return None
    if element.element_category == "caption" or _is_caption(_text(element)):
        if _text(element).startswith("表"):
            return _rule_by_name(rules, "Paragraph (1a)") or _rule_by_name(rules, "Caption")
        return _rule_by_name(rules, "Caption (19)") or _rule_by_name(rules, "Caption")
    if element.part_name != "word/document.xml":
        return mapped_rule

    paragraph_index = _top_level_paragraph_index(element.xml_path)
    text = _text(element)
    if paragraph_index is None or not text:
        return None
    body_start = int(context.get("bodyStart", 1_000_000))
    reference_start = int(context.get("referenceStart", 1_000_000))
    if paragraph_index < body_start:
        return None
    if _is_toc_line(text):
        return _toc_rule(rules, text)
    if text == "参考文献":
        return _rule_by_name(rules, "Paragraph (15)")
    if paragraph_index > reference_start:
        return None
    if _is_chapter_heading(text):
        return _rule_by_name(rules, "Paragraph (15)")
    if _is_second_level_heading(text):
        return _rule_by_name(rules, "Paragraph (16)")
    if _is_section_heading(text):
        return _rule_by_name(rules, "Paragraph (17)")
    return _rule_by_name(rules, "Paragraph")


def _rule_by_name(rules: list[ProfileRule], name: str) -> ProfileRule | None:
    for rule in rules:
        if rule.name == name:
            return rule
    return None


def _toc_rule(rules: list[ProfileRule], text: str) -> ProfileRule | None:
    normalized = text.replace("\t", " ")
    if re.match(r"^\d+\.\d+\s+", normalized):
        return _rule_by_name(rules, "Paragraph (TOC2)")
    if re.match(r"^\d+\.\s+", normalized) or normalized.startswith("参考文献"):
        return _rule_by_name(rules, "Paragraph (TOC1)")
    return None


def _is_toc_line(text: str) -> bool:
    return bool(re.match(r"^\d+(?:\.\d+)?\s+.+\t\d+$", text)) or bool(
        re.match(r"^参考文献\t\d+$", text)
    )


def _is_caption(text: str) -> bool:
    return bool(re.match(r"^[表图]\s*\d+(?:-\d+)?", text))


def _is_chapter_heading(text: str) -> bool:
    return text in {
        "导  论",
        "导论",
        "相关理论基础",
        "湖州乡村文旅全电民宿造价成本分析",
        "湖州全电民宿收益来源与测算",
        "全电民宿造价成本与收益平衡分析",
        "湖州全电民宿典型案例分析",
        "结论与展望",
        "参考文献",
    }


def _is_section_heading(text: str) -> bool:
    if len(text) > 30:
        return False
    if any(mark in text for mark in "，。；：,.;"):
        return False
    if re.match(r"^\d+(?:\.\d+)*\s+", text):
        return True
    return text in _SECOND_LEVEL_HEADINGS or text in {
        "选题背景",
        "选题意义",
        "绿色效益突出，场景适配不足",
        "增量成本可控，全周期核算不足",
        "转型核心载体，三重瓶颈待破",
        "文献述评",
        "全生命周期成本分析法",
        "案例分析法",
        "文献研究法",
        "定量分析法",
        "绿色转型的基本内涵",
        "绿色转型的核心特征",
        "绿色转型在乡村文旅中的应用",
        "全生命周期成本的内涵",
        "分析方法框架",
        "全生命周期成本的应用价值",
        "政策补贴",
        "经营溢价",
        "补贴与溢价的协同作用机制",
        "直接运营收益",
        "政策补贴收益",
        "经营溢价收益",
        "成本 — 收益平衡模型",
        "平衡区间测算",
        "平衡影响因素",
        "莫干山瀛轩民宿概况",
        "造价成本与补贴申领",
        "收益与投资回收期",
    }


_SECOND_LEVEL_HEADINGS = {
        "选题背景与意义",
        "选题背景",
        "选题意义",
        "国内外文献综述",
        "论文的主要内容",
        "论文的研究方法",
        "绿色转型",
        "全生命周期成本的内涵",
        "政策补贴与经营溢价",
        "造价成本核心构成",
        "与传统民宿的成本差异",
        "增量成本特征与门槛值",
        "全生命周期运营成本",
}


def _is_second_level_heading(text: str) -> bool:
    return text in _SECOND_LEVEL_HEADINGS


def _document_order_key(element: TargetElement) -> tuple[int, int, str]:
    part_rank = 0 if element.part_name == "word/document.xml" else 1
    paragraph_index = _paragraph_index(element.xml_path) or 1_000_000
    return (part_rank, paragraph_index, element.id)


def _paragraph_index(xml_path: str | None) -> int | None:
    match = re.search(r"/w:p\[(\d+)\](?:/.*)?$", xml_path or "")
    return int(match.group(1)) if match else None


def _top_level_paragraph_index(xml_path: str | None) -> int | None:
    match = re.fullmatch(r"/w:document/w:body/w:p\[(\d+)\]", xml_path or "")
    return int(match.group(1)) if match else None


def _text(element: TargetElement) -> str:
    return (element.text_summary or "").strip()


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
