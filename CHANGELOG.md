# Changelog

## 0.1.1 - Agent Hardening and Smoke Validation

- Reuse existing mapping results and draft patch plans during agent runs to avoid deleting
  mapping rows that are still referenced by patch operations.
- Roll back failed agent transactions before writing the final failed run state.
- Add a render gate to distinguish successful render prechecks from missing renderer output,
  missing page counts, and excessive page-count drift.
- Surface render gate details in agent summaries and the web UI.
- Add a repeatable Docker/API smoke script for template upload, target upload, agent run, and
  DOCX/PDF artifact download.
- Update rerank tests for the current parallel rerank path.

## 0.1.0 - Backend MVP

- FastAPI and Celery backend for DOCX ingestion, OOXML parsing, template profile/rule generation,
  target mapping, patch planning, patch execution, layout postprocess, LibreOffice render precheck,
  and deterministic agent runs.

## Next: 0.2.0 - Reviewable Visual QA

Planned focus:

- Persist a compact human-readable review report for each agent run.
- Convert skipped patch operations into grouped review items with risk, reason, and suggested action.
- Improve unsupported operation handling, especially image operations and complex section/header/footer layout.
- Add an API/UI path to inspect render drift and download all run artifacts from one place.
