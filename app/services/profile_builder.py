from collections import Counter, defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.document import FormatAtom, FormatProfile, ProfileRule

PROFILE_SOURCE = "deterministic_v0"


def rebuild_deterministic_profile(db: Session, document_version_id: str, name: str) -> FormatProfile:
    atoms = list(
        db.scalars(
            select(FormatAtom)
            .where(FormatAtom.document_version_id == document_version_id)
            .order_by(FormatAtom.created_at, FormatAtom.id)
        )
    )
    db.execute(delete(ProfileRule).where(ProfileRule.document_version_id == document_version_id))
    db.execute(delete(FormatProfile).where(FormatProfile.document_version_id == document_version_id))
    db.flush()

    profile = FormatProfile(
        document_version_id=document_version_id,
        name=name,
        version=1,
        status="active",
        source=PROFILE_SOURCE,
        summary=_profile_summary(atoms),
    )
    db.add(profile)
    db.flush()

    for rule in _build_rules(atoms):
        db.add(
            ProfileRule(
                profile_id=profile.id,
                document_version_id=document_version_id,
                rule_type=rule["rule_type"],
                element_category=rule["element_category"],
                name=rule["name"],
                priority=rule["priority"],
                selector=rule["selector"],
                properties=rule["properties"],
                source_atom_ids=rule["source_atom_ids"],
                confidence=rule["confidence"],
            )
        )
    db.commit()
    db.refresh(profile)
    return profile


def _profile_summary(atoms: list[FormatAtom]) -> dict:
    by_type = Counter(atom.atom_type for atom in atoms)
    by_category = Counter(atom.element_category or "unknown" for atom in atoms)
    styles = sorted({atom.style_id for atom in atoms if atom.style_id})
    return {
        "source": PROFILE_SOURCE,
        "atomCount": len(atoms),
        "atomTypes": dict(by_type),
        "elementCategories": dict(by_category),
        "styleIds": styles,
    }


def _build_rules(atoms: list[FormatAtom]) -> list[dict]:
    rules: list[dict] = []
    rules.extend(_document_rules(atoms))
    rules.extend(_style_category_rules(atoms))
    rules.extend(_single_category_rules(atoms))
    return rules


def _document_rules(atoms: list[FormatAtom]) -> list[dict]:
    rules = []
    for atom in atoms:
        if atom.atom_type != "document_setup":
            continue
        rules.append(
            {
                "rule_type": "document",
                "element_category": "document_setup",
                "name": "Document setup",
                "priority": 10,
                "selector": {"atomType": "document_setup"},
                "properties": atom.normalized,
                "source_atom_ids": [atom.id],
                "confidence": 100,
            }
        )
        break
    return rules


def _style_category_rules(atoms: list[FormatAtom]) -> list[dict]:
    grouped: dict[tuple[str, str], list[FormatAtom]] = defaultdict(list)
    eligible = {"heading", "paragraph", "list", "caption", "citation", "header_footer"}
    for atom in atoms:
        category = atom.element_category or atom.atom_type
        if category not in eligible:
            continue
        if atom.atom_type not in {"heading", "paragraph", "list", "header_footer"}:
            continue
        key = (category, atom.style_id or "__no_style__")
        grouped[key].append(atom)

    rules: list[dict] = []
    for (category, style_id), group in sorted(grouped.items()):
        representative = _choose_representative(group)
        effective = representative.normalized.get("effective", {})
        selector = {"elementCategory": category}
        if style_id != "__no_style__":
            selector["styleId"] = style_id
        if representative.numbering_id:
            selector["numberingId"] = representative.numbering_id
        rules.append(
            {
                "rule_type": _rule_type_for_category(category),
                "element_category": category,
                "name": _rule_name(category, None if style_id == "__no_style__" else style_id),
                "priority": _priority_for_category(category),
                "selector": selector,
                "properties": {
                    "effective": effective,
                    "textSamples": _text_samples(group),
                    "occurrenceCount": len(group),
                },
                "source_atom_ids": [atom.id for atom in group[:10]],
                "confidence": 95 if style_id != "__no_style__" else 80,
            }
        )
    return rules


def _single_category_rules(atoms: list[FormatAtom]) -> list[dict]:
    rules: list[dict] = []
    categories = {
        "table": ("table", "Table layout", 40),
        "image": ("image", "Image layout", 45),
        "footnote": ("footnote", "Footnote format", 35),
        "endnote": ("endnote", "Endnote format", 35),
    }
    for category, (rule_type, name, priority) in categories.items():
        group = [
            atom
            for atom in atoms
            if atom.atom_type == category or atom.element_category == category
        ]
        if not group:
            continue
        representative = _choose_representative(group)
        rules.append(
            {
                "rule_type": rule_type,
                "element_category": category,
                "name": name,
                "priority": priority,
                "selector": {"elementCategory": category, "atomType": category},
                "properties": {
                    "representative": representative.normalized,
                    "textSamples": _text_samples(group),
                    "occurrenceCount": len(group),
                },
                "source_atom_ids": [atom.id for atom in group[:10]],
                "confidence": 90,
            }
        )
    return rules


def _choose_representative(atoms: list[FormatAtom]) -> FormatAtom:
    return max(atoms, key=lambda atom: len(atom.text_summary or ""))


def _text_samples(atoms: list[FormatAtom]) -> list[str]:
    samples = []
    seen = set()
    for atom in atoms:
        text = (atom.text_summary or "").strip()
        if not text or text in seen:
            continue
        samples.append(text[:160])
        seen.add(text)
        if len(samples) == 3:
            break
    return samples


def _rule_type_for_category(category: str) -> str:
    return {
        "heading": "heading",
        "list": "list",
        "header_footer": "header_footer",
        "caption": "caption",
        "citation": "citation",
    }.get(category, "paragraph")


def _priority_for_category(category: str) -> int:
    return {
        "document_setup": 10,
        "heading": 20,
        "paragraph": 30,
        "list": 32,
        "caption": 34,
        "citation": 34,
        "header_footer": 25,
    }.get(category, 50)


def _rule_name(category: str, style_id: str | None) -> str:
    label = category.replace("_", " ").title()
    return f"{label} ({style_id})" if style_id else label
