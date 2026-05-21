# DOCX Visual Consistency Backend MVP

Python backend MVP for deterministic DOCX ingestion and OOXML parsing. The first milestone
stores DOCX package parts, relationships, media, effective-format records, and format atoms in
PostgreSQL with pgvector enabled for later retrieval.

## Run Locally

```bash
cp .env.example .env
docker compose up --build
```

In another shell, initialize the database:

```bash
docker compose exec api alembic upgrade head
```

Upload a template:

```bash
curl -F "file=@template.docx" http://localhost:8000/templates/current
curl http://localhost:8000/templates/current/status
curl http://localhost:8000/templates/current/atoms
curl http://localhost:8000/templates/current/profile
curl http://localhost:8000/templates/current/rules
```

Upload a target document after a template profile exists:

```bash
curl -F "file=@target.docx" http://localhost:8000/targets
curl http://localhost:8000/targets/latest/status
curl http://localhost:8000/targets/latest/elements
curl http://localhost:8000/targets/latest/mappings
curl http://localhost:8000/targets/latest/mappings/{mapping_id}/candidates
curl http://localhost:8000/targets/latest/patch-plan
curl http://localhost:8000/targets/latest/patch-plan/operations
curl -X POST http://localhost:8000/targets/latest/patch-plan/execute
curl http://localhost:8000/targets/latest/patch-plan/execution
curl -OJ http://localhost:8000/targets/latest/output.docx
curl -X POST http://localhost:8000/targets/latest/render-precheck
curl http://localhost:8000/targets/latest/render-snapshot
curl -OJ http://localhost:8000/targets/latest/render.pdf
curl -X POST "http://localhost:8000/targets/latest/agent-run?max_rounds=3"
curl http://localhost:8000/targets/latest/agent-run/status
```

## Scope

Implemented in this MVP:

- FastAPI upload/status/query endpoints.
- Celery `template_ingestion` task.
- DOCX safety checks, package part ingestion, relationship graph extraction.
- Core OOXML parsing for document, styles, numbering, settings, theme, font table,
  headers/footers, footnotes/endnotes, and media.
- Initial effective formatting and format atom generation.
- Deterministic single-template `format_profiles` and `profile_rules` v0.
- Target DOCX ingestion, `target_elements`, and deterministic `mapping_results` skeleton.
- Optional Gemini embeddings for `format_atoms` when `GEMINI_API_KEY` is configured.
- Hybrid mapping v0 with structural scoring, keyword boosts, vector atom recall, and
  persisted `mapping_candidates`.
- Optional Gemini structured-output rerank for top mapping candidates, with deterministic
  fallback when disabled or unavailable.
- Draft-only deterministic patch plans and patch operations generated from mapping results.
- Conservative OOXML patch execution v0 for paragraph, heading, table, note, and
  header/footer paragraph operations, producing a new output DOCX while skipping
  unsupported high-risk edits.
- Optional Word COM layout postprocess for agent outputs. On Windows with pywin32 and
  Microsoft Word installed, the agent can normalize page setup, cover table spacing,
  header/footer text, and a static TOC in an isolated subprocess. In Docker/Linux or
  when Word automation times out, this stage is recorded as skipped and the agent keeps
  the deterministic OOXML output.
- LibreOffice headless render precheck for patched outputs, storing PDF bytes when
  LibreOffice is available and a skipped snapshot when it is not installed.
- Autonomous deterministic format agent run loop with persisted run/step records,
  batched patch execution, Word postprocess, render precheck, and bounded internal
  repair rounds.

Reserved for later phases:

- Gemini profile generation and visual review calls.
- Specialized OOXML patch execution for images and complex section/header/footer layout.
- Microsoft Graph / Word rendering as final visual truth.
- Multi-agent review and auto-repair.
