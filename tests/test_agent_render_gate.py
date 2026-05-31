from dataclasses import dataclass

from app.tasks.template_tasks import _render_gate_result


@dataclass
class Snapshot:
    status: str
    page_count: int | None


def test_render_gate_passes_when_done_and_page_drift_is_small():
    result = _render_gate_result(Snapshot("done", 10), Snapshot("done", 11))

    assert result["passed"] is True
    assert result["stopReason"] == "render_precheck_passed"
    assert result["pageCountDrift"] == 1


def test_render_gate_blocks_when_render_was_skipped():
    result = _render_gate_result(Snapshot("skipped", None), Snapshot("done", 10))

    assert result["passed"] is False
    assert result["stopReason"] == "render_precheck_unavailable"
    assert result["baselineRenderStatus"] == "skipped"


def test_render_gate_blocks_when_page_count_is_missing():
    result = _render_gate_result(Snapshot("done", None), Snapshot("done", 10))

    assert result["passed"] is False
    assert result["stopReason"] == "page_count_unavailable"


def test_render_gate_blocks_when_page_drift_is_too_large():
    result = _render_gate_result(Snapshot("done", 10), Snapshot("done", 12))

    assert result["passed"] is False
    assert result["stopReason"] == "layout_drift_too_large"
    assert result["pageCountDrift"] == 2
