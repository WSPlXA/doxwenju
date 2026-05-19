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

Reserved for later phases:

- Gemini profile generation and visual review calls.
- OOXML patch execution.
- OOXML patch engine.
- Word/Graph and LibreOffice rendering.
- Multi-agent review and auto-repair.
