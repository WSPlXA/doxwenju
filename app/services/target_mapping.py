import math
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import (
    FormatAtom,
    MappingCandidate,
    MappingResult,
    ProfileRule,
    TargetElement,
)
from app.services.rerank import rerank_candidates_parallel

# Numbered heading depth: matches "1 X", "1.2 X", "1.2.3 X" etc.
_NUMBERED_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+\S")


def rebuild_target_elements(db: Session, document_version_id: str) -> list[TargetElement]:
    existing_ids = select(TargetElement.id).where(
        TargetElement.document_version_id == document_version_id
    )
    db.execute(delete(MappingCandidate).where(MappingCandidate.target_element_id.in_(existing_ids)))
    db.execute(delete(MappingResult).where(MappingResult.target_element_id.in_(existing_ids)))
    db.execute(
        delete(TargetElement).where(TargetElement.document_version_id == document_version_id)
    )
    db.flush()

    atoms = list(
        db.scalars(
            select(FormatAtom)
            .where(FormatAtom.document_version_id == document_version_id)
            .where(
                FormatAtom.atom_type.in_(
                    [
                        "heading",
                        "paragraph",
                        "list",
                        "table",
                        "image",
                        "footnote",
                        "endnote",
                        "header_footer",
                    ]
                )
            )
            .order_by(FormatAtom.created_at, FormatAtom.id)
        )
    )
    elements = []
    for atom in atoms:
        element = TargetElement(
            document_version_id=document_version_id,
            source_atom_id=atom.id,
            element_type=atom.atom_type,
            element_category=atom.element_category,
            part_name=atom.part_name,
            xml_path=atom.xml_path,
            text_summary=atom.text_summary,
            style_id=atom.style_id,
            numbering_id=atom.numbering_id,
            normalized=atom.normalized,
            classification={
                "source": "deterministic_v0",
                "category": atom.element_category,
                "confidence": 90 if atom.element_category else 70,
            },
        )
        db.add(element)
        elements.append(element)
    db.commit()
    for element in elements:
        db.refresh(element)
    return elements


def rebuild_mapping_results(
    db: Session, target_document_version_id: str, template_document_version_id: str
) -> list[MappingResult]:
    elements = list(
        db.scalars(
            select(TargetElement)
            .where(TargetElement.document_version_id == target_document_version_id)
            .order_by(TargetElement.created_at, TargetElement.id)
        )
    )
    rules = list(
        db.scalars(
            select(ProfileRule)
            .where(ProfileRule.document_version_id == template_document_version_id)
            .order_by(ProfileRule.priority, ProfileRule.created_at, ProfileRule.id)
        )
    )
    element_ids = [element.id for element in elements]
    if element_ids:
        db.execute(
            delete(MappingCandidate).where(MappingCandidate.target_element_id.in_(element_ids))
        )
        db.execute(delete(MappingResult).where(MappingResult.target_element_id.in_(element_ids)))
    db.flush()

    # --- Phase 1: compute structural/vector candidates (fast, no API calls) ---
    elements_with_candidates = [
        (
            element,
            _candidate_rules_for_element(
                db=db,
                element=element,
                rules=rules,
                template_document_version_id=template_document_version_id,
            ),
        )
        for element in elements
    ]

    # --- Phase 2: parallel Gemini rerank for top candidates ---
    max_rerank = settings.gemini_rerank_max_elements_per_run
    rerank_pairs = elements_with_candidates[:max_rerank]
    non_rerank_pairs = elements_with_candidates[max_rerank:]

    rerank_decisions = rerank_candidates_parallel(
        db=db,
        document_version_id=target_document_version_id,
        elements_with_candidates=rerank_pairs,
    )
    # Elements beyond the rerank limit fall back to top hybrid candidate
    fallback_decisions = [
        (candidates[0] if candidates else None, None)
        for _, candidates in non_rerank_pairs
    ]
    all_decisions = rerank_decisions + fallback_decisions

    # --- Phase 2.5: normalize heading depth consistency ---
    # Within headings that share the same numbered-text depth ("1 X" = depth 1, "1.2 X" = depth 2 …),
    # apply the majority-voted rule so that inconsistent Gemini choices are corrected.
    # E.g. if 4 out of 5 depth-2 headings chose Heading (17) but one chose Heading (16), the
    # outlier is corrected to Heading (17).
    all_decisions = _normalize_heading_decisions(
        elements=[e for e, _ in elements_with_candidates],
        decisions=all_decisions,
        candidates_per_element=[c for _, c in elements_with_candidates],
    )

    # --- Phase 3: write MappingResult + MappingCandidate rows ---
    results = []
    for (element, candidates), (selected_candidate, rerank_info) in zip(
        elements_with_candidates, all_decisions, strict=True
    ):
        rule = selected_candidate["rule"] if selected_candidate else None
        score = selected_candidate["score"] if selected_candidate else 0
        rationale = (
            {
                "strategy": "gemini_rerank_v0"
                if rerank_info and "error" not in rerank_info
                else "hybrid_v0",
                "winner": _candidate_rationale(selected_candidate),
                "candidateCount": len(candidates),
                "rerank": rerank_info,
            }
            if selected_candidate
            else {"strategy": "hybrid_v0", "reasons": ["no template rules available"]}
        )
        result = MappingResult(
            target_element_id=element.id,
            profile_rule_id=rule.id if rule else None,
            score=score,
            strategy="gemini_rerank_v0"
            if rerank_info and "error" not in rerank_info
            else "hybrid_v0",
            rationale=rationale,
        )
        db.add(result)
        results.append(result)
        for rank, candidate in enumerate(candidates[:10], start=1):
            db.add(
                MappingCandidate(
                    target_element_id=element.id,
                    profile_rule_id=candidate["rule"].id,
                    rank=rank,
                    score=candidate["score"],
                    strategy="hybrid_v0",
                    rationale=_candidate_rationale(candidate),
                )
            )
    db.commit()
    for result in results:
        db.refresh(result)
    return results


def _normalize_heading_decisions(
    elements: list[TargetElement],
    decisions: list[tuple[dict | None, dict | None]],
    candidates_per_element: list[list[dict]],
) -> list[tuple[dict | None, dict | None]]:
    """Normalize Gemini heading choices so all headings at the same numbered depth
    get the same profile rule (majority vote across the depth group).

    Only applies when there are >= 2 headings at that depth AND a clear majority
    (strictly more than half) agree on a single rule.
    """
    from collections import Counter

    # Collect indices of heading elements with numbered text depth
    depth_groups: dict[int, list[int]] = {}  # depth → list of indices into decisions
    for idx, element in enumerate(elements):
        if element.element_category != "heading":
            continue
        depth = _numbered_heading_depth(element.text_summary)
        if depth is None:
            continue
        depth_groups.setdefault(depth, []).append(idx)

    # For each depth group, find majority rule and fix outliers
    decisions = list(decisions)  # make mutable copy
    for depth, indices in depth_groups.items():
        if len(indices) < 2:
            continue
        # Count rule IDs chosen by Gemini for this depth
        rule_id_counter: Counter[str] = Counter()
        for idx in indices:
            sel, _ = decisions[idx]
            if sel:
                rule_id_counter[sel["rule"].id] += 1
        if not rule_id_counter:
            continue
        majority_rule_id, majority_count = rule_id_counter.most_common(1)[0]
        if majority_count <= len(indices) // 2:
            continue  # no clear majority
        # Apply majority rule to all outliers in this depth group
        for idx in indices:
            sel, rerank_info = decisions[idx]
            if sel and sel["rule"].id == majority_rule_id:
                continue
            # Find the majority rule candidate in this element's candidate list
            majority_candidate = next(
                (c for c in candidates_per_element[idx] if c["rule"].id == majority_rule_id),
                None,
            )
            if majority_candidate is None:
                continue  # majority rule wasn't even a candidate — skip
            # Replace the decision, tag rationale with normalization info
            patched_rerank = {
                **(rerank_info or {}),
                "normalizedFromDepth": depth,
                "overriddenRuleId": sel["rule"].id if sel else None,
            }
            decisions[idx] = (majority_candidate, patched_rerank)
    return decisions


def _candidate_rules_for_element(
    db: Session,
    element: TargetElement,
    rules: list[ProfileRule],
    template_document_version_id: str,
) -> list[dict]:
    vector_scores = _vector_atom_scores(db, element, template_document_version_id)
    heading_rules = [r for r in rules if r.element_category == "heading"] if element.element_category == "heading" else []
    candidates: list[dict] = []
    for rule in rules:
        structural_score, structural_reasons = _score_rule(element, rule, heading_rules)
        vector_score, vector_reasons = _vector_boost(rule, vector_scores)
        keyword_score, keyword_reasons = _keyword_boost(element, rule)
        score = structural_score + vector_score + keyword_score
        if score <= 0:
            continue
        candidates.append(
            {
                "rule": rule,
                "score": score,
                "structuralScore": structural_score,
                "vectorScore": vector_score,
                "keywordScore": keyword_score,
                "reasons": structural_reasons + vector_reasons + keyword_reasons,
            }
        )
    return sorted(candidates, key=lambda item: (-item["score"], item["rule"].priority))[:10]


def _numbered_heading_depth(text: str | None) -> int | None:
    """Return the depth of a numbered section heading (1-based), or None.

    Examples:
        "3 标题"      → 1  (chapter level)
        "3.2 总结"    → 2  (section level)
        "3.2.1 X"     → 3  (subsection level)
    """
    if not text:
        return None
    m = _NUMBERED_PREFIX_RE.match(text.strip())
    if not m:
        return None
    return len(m.group(1).split("."))


def _heading_depth_rank(rule: ProfileRule, all_heading_rules: list[ProfileRule]) -> int | None:
    """Estimate the heading depth rank of *rule* among all heading rules.

    Ranks by effective run font size (larger font = shallower/higher level = rank 1).
    Returns 1-based rank, or None if the rule has no font-size info.
    """
    def _rule_sz(r: ProfileRule) -> float | None:
        props = r.properties or {}
        run_eff = props.get("runEffective") or {}
        sz = run_eff.get("sz")
        if sz is not None:
            try:
                return float(sz)
            except (TypeError, ValueError):
                pass
        return None

    rule_sz = _rule_sz(rule)
    if rule_sz is None:
        return None

    # Collect all unique sizes among heading rules, sorted descending
    sizes = sorted(
        {s for r in all_heading_rules if (s := _rule_sz(r)) is not None},
        reverse=True,
    )
    if not sizes:
        return None
    try:
        return sizes.index(rule_sz) + 1  # 1-based rank
    except ValueError:
        return None


def _score_rule(
    element: TargetElement,
    rule: ProfileRule,
    heading_rules: list[ProfileRule] | None = None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    selector = rule.selector or {}

    if element.element_category and rule.element_category == element.element_category:
        score += 60
        reasons.append("element_category_match")
    elif rule.rule_type == element.element_type:
        score += 40
        reasons.append("element_type_match")

    selector_style = selector.get("styleId")
    if selector_style and selector_style == element.style_id:
        score += 25
        reasons.append("style_id_match")

    selector_numbering = selector.get("numberingId")
    if selector_numbering and selector_numbering == element.numbering_id:
        score += 15
        reasons.append("numbering_id_match")

    if selector.get("atomType") == element.element_type:
        score += 10
        reasons.append("atom_type_match")

    # Heading depth penalty: numbered section text patterns ("1 X" depth=1, "1.2 X" depth=2, …)
    # We penalise rules whose rank is MORE THAN ONE step away from the element's numbered depth.
    # This prevents Gemini from confusing e.g. "3 标题" (chapter, depth=1) with a level-3
    # heading (rank=3) — but we do NOT give bonuses because the depth→rank offset varies by
    # template (e.g. rank-1 may be a thesis-title style, not a numbered chapter heading).
    if (
        element.element_category == "heading"
        and rule.element_category == "heading"
        and heading_rules
    ):
        element_depth = _numbered_heading_depth(element.text_summary)
        rule_rank = _heading_depth_rank(rule, heading_rules)
        if element_depth is not None and rule_rank is not None:
            gap = abs(element_depth - rule_rank)
            if gap >= 2:
                score -= 25  # large depth mismatch, penalise strongly
                reasons.append("heading_depth_mismatch")

    score += max(0, 10 - rule.priority // 10)
    return score, reasons or ["fallback_priority"]


def _vector_atom_scores(
    db: Session, element: TargetElement, template_document_version_id: str
) -> dict[str, int]:
    if element.embedding is None:
        return {}
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return _postgres_vector_atom_scores(db, element, template_document_version_id)
    return _python_vector_atom_scores(db, element, template_document_version_id)


def _postgres_vector_atom_scores(
    db: Session, element: TargetElement, template_document_version_id: str
) -> dict[str, int]:
    distance = FormatAtom.embedding.cosine_distance(element.embedding)
    rows = db.execute(
        select(FormatAtom.id, distance.label("distance"))
        .where(FormatAtom.document_version_id == template_document_version_id)
        .where(FormatAtom.embedding.is_not(None))
        .order_by(distance)
        .limit(30)
    ).all()
    return {
        atom_id: _distance_to_boost(float(distance_value))
        for atom_id, distance_value in rows
        if distance_value is not None
    }


def _python_vector_atom_scores(
    db: Session, element: TargetElement, template_document_version_id: str
) -> dict[str, int]:
    atoms = list(
        db.scalars(
            select(FormatAtom)
            .where(FormatAtom.document_version_id == template_document_version_id)
            .where(FormatAtom.embedding.is_not(None))
        )
    )
    scored = []
    for atom in atoms:
        if atom.embedding is None:
            continue
        distance = _cosine_distance(element.embedding, atom.embedding)
        scored.append((atom.id, _distance_to_boost(distance)))
    return dict(sorted(scored, key=lambda item: -item[1])[:30])


def _vector_boost(rule: ProfileRule, vector_scores: dict[str, int]) -> tuple[int, list[str]]:
    source_atom_ids = rule.source_atom_ids or []
    boosts = [vector_scores[atom_id] for atom_id in source_atom_ids if atom_id in vector_scores]
    if not boosts:
        return 0, []
    return max(boosts), ["vector_source_atom_match"]


def _keyword_boost(element: TargetElement, rule: ProfileRule) -> tuple[int, list[str]]:
    text = (element.text_summary or "").strip().lower()
    if not text:
        return 0, []
    keyword_categories = {
        "caption": ("figure", "fig.", "table", "图", "表"),
        "citation": ("references", "bibliography", "参考文献"),
        "footnote": ("footnote", "脚注"),
        "endnote": ("endnote", "尾注"),
    }
    terms = keyword_categories.get(rule.element_category, ())
    if any(term in text for term in terms):
        return 12, ["keyword_category_match"]
    return 0, []


def _candidate_rationale(candidate: dict) -> dict:
    rule = candidate["rule"]
    return {
        "profileRuleId": rule.id,
        "ruleName": rule.name,
        "ruleType": rule.rule_type,
        "elementCategory": rule.element_category,
        "score": candidate["score"],
        "structuralScore": candidate["structuralScore"],
        "vectorScore": candidate["vectorScore"],
        "keywordScore": candidate["keywordScore"],
        "reasons": candidate["reasons"],
    }


def _distance_to_boost(distance: float) -> int:
    similarity = max(0.0, min(1.0, 1.0 - distance))
    return int(round(similarity * 30))


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 1.0
    return 1.0 - dot / (left_norm * right_norm)
