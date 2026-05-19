from collections import Counter

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.document import (
    PatchExecution,
    PatchOperation,
    PatchPlan,
    RenderSnapshot,
    ReviewFinding,
    ReviewReport,
)

REVIEW_SOURCE = "deterministic_v0"


def rebuild_review_report(
    db: Session,
    output_document_version_id: str,
    patch_plan_id: str | None = None,
    render_snapshot_id: str | None = None,
) -> ReviewReport:
    patch_plan = _patch_plan(db, output_document_version_id, patch_plan_id)
    render_snapshot = _render_snapshot(db, output_document_version_id, render_snapshot_id)

    existing_report_ids = select(ReviewReport.id).where(
        ReviewReport.document_version_id == output_document_version_id
    )
    db.execute(delete(ReviewFinding).where(ReviewFinding.review_report_id.in_(existing_report_ids)))
    db.execute(delete(ReviewReport).where(ReviewReport.document_version_id == output_document_version_id))
    db.flush()

    findings = _build_findings(db, output_document_version_id, patch_plan, render_snapshot)
    summary = _summary(findings, patch_plan, render_snapshot)
    report_json = {
        "source": REVIEW_SOURCE,
        "documentVersionId": output_document_version_id,
        "patchPlanId": patch_plan.id if patch_plan else None,
        "renderSnapshotId": render_snapshot.id if render_snapshot else None,
        "summary": summary,
        "findings": findings,
    }
    report = ReviewReport(
        document_version_id=output_document_version_id,
        patch_plan_id=patch_plan.id if patch_plan else None,
        render_snapshot_id=render_snapshot.id if render_snapshot else None,
        status="done",
        source=REVIEW_SOURCE,
        summary=summary,
        report_json=report_json,
    )
    db.add(report)
    db.flush()
    for finding in findings:
        db.add(
            ReviewFinding(
                review_report_id=report.id,
                document_version_id=output_document_version_id,
                patch_operation_id=finding.get("patchOperationId"),
                severity=finding["severity"],
                category=finding["category"],
                message=finding["message"],
                evidence=finding["evidence"],
                status="open",
            )
        )
    db.commit()
    db.refresh(report)
    return report


def latest_review_report(db: Session, output_document_version_id: str) -> ReviewReport | None:
    return db.scalar(
        select(ReviewReport)
        .where(ReviewReport.document_version_id == output_document_version_id)
        .order_by(ReviewReport.created_at.desc())
        .limit(1)
    )


def _patch_plan(db: Session, output_document_version_id: str, patch_plan_id: str | None) -> PatchPlan | None:
    if patch_plan_id:
        return db.get(PatchPlan, patch_plan_id)
    return db.scalar(
        select(PatchPlan)
        .where(PatchPlan.output_document_version_id == output_document_version_id)
        .order_by(PatchPlan.created_at.desc())
        .limit(1)
    )


def _render_snapshot(
    db: Session, output_document_version_id: str, render_snapshot_id: str | None
) -> RenderSnapshot | None:
    if render_snapshot_id:
        return db.get(RenderSnapshot, render_snapshot_id)
    return db.scalar(
        select(RenderSnapshot)
        .where(RenderSnapshot.document_version_id == output_document_version_id)
        .order_by(RenderSnapshot.created_at.desc())
        .limit(1)
    )


def _build_findings(
    db: Session,
    output_document_version_id: str,
    patch_plan: PatchPlan | None,
    render_snapshot: RenderSnapshot | None,
) -> list[dict]:
    findings: list[dict] = []
    execution = _latest_execution(db, output_document_version_id, patch_plan)
    if execution is None:
        findings.append(
            _finding(
                "P0",
                "patch_execution",
                "No patch execution record was found for this output document.",
                {"outputDocumentVersionId": output_document_version_id},
            )
        )
    elif execution.status != "done":
        findings.append(
            _finding(
                "P0",
                "patch_execution",
                f"Patch execution status is {execution.status}.",
                {"executionId": execution.id, "error": execution.error_message},
            )
        )

    if render_snapshot is None:
        findings.append(
            _finding(
                "P2",
                "render_precheck",
                "No render precheck snapshot exists for the output document.",
                {"outputDocumentVersionId": output_document_version_id},
            )
        )
    elif render_snapshot.status == "failed":
        findings.append(
            _finding(
                "P0",
                "render_precheck",
                "LibreOffice render precheck failed.",
                {"snapshotId": render_snapshot.id, "error": render_snapshot.error_message},
            )
        )
    elif render_snapshot.status == "skipped":
        findings.append(
            _finding(
                "P2",
                "render_precheck",
                "LibreOffice render precheck was skipped.",
                {"snapshotId": render_snapshot.id, "error": render_snapshot.error_message},
            )
        )

    if patch_plan is not None:
        operations = list(
            db.scalars(
                select(PatchOperation)
                .where(PatchOperation.patch_plan_id == patch_plan.id)
                .order_by(PatchOperation.created_at, PatchOperation.id)
            )
        )
        for operation in operations:
            if operation.status != "skipped":
                continue
            severity = operation.risk_level if operation.risk_level in {"P1", "P2"} else "P3"
            findings.append(
                _finding(
                    severity,
                    "patch_operation_skipped",
                    f"{operation.operation_type} was not applied by patch engine v0.",
                    {
                        "patchOperationId": operation.id,
                        "operationType": operation.operation_type,
                        "riskLevel": operation.risk_level,
                        "partName": operation.part_name,
                        "xmlPath": operation.xml_path,
                        "rationale": operation.rationale,
                    },
                    patch_operation_id=operation.id,
                )
            )
    return findings


def _latest_execution(
    db: Session, output_document_version_id: str, patch_plan: PatchPlan | None
) -> PatchExecution | None:
    stmt = select(PatchExecution).where(
        PatchExecution.output_document_version_id == output_document_version_id
    )
    if patch_plan is not None:
        stmt = stmt.where(PatchExecution.patch_plan_id == patch_plan.id)
    return db.scalar(stmt.order_by(PatchExecution.created_at.desc()).limit(1))


def _summary(
    findings: list[dict], patch_plan: PatchPlan | None, render_snapshot: RenderSnapshot | None
) -> dict:
    by_severity = Counter(finding["severity"] for finding in findings)
    by_category = Counter(finding["category"] for finding in findings)
    blocking = by_severity.get("P0", 0) > 0 or by_severity.get("P1", 0) > 0
    return {
        "findingCount": len(findings),
        "bySeverity": dict(by_severity),
        "byCategory": dict(by_category),
        "blocking": blocking,
        "patchPlanStatus": patch_plan.status if patch_plan else None,
        "renderStatus": render_snapshot.status if render_snapshot else None,
        "renderPageCount": render_snapshot.page_count if render_snapshot else None,
    }


def _finding(
    severity: str,
    category: str,
    message: str,
    evidence: dict,
    patch_operation_id: str | None = None,
) -> dict:
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "evidence": evidence,
        "patchOperationId": patch_operation_id,
        "status": "open",
    }
