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
```

## Scope

Implemented in this MVP:

- FastAPI upload/status/query endpoints.
- Celery `template_ingestion` task.
- DOCX safety checks, package part ingestion, relationship graph extraction.
- Core OOXML parsing for document, styles, numbering, settings, theme, font table,
  headers/footers, footnotes/endnotes, and media.
- Initial effective formatting and format atom generation.

Reserved for later phases:

- Gemini profile/rerank/review calls.
- OOXML patch engine.
- Word/Graph and LibreOffice rendering.
- Multi-agent review and auto-repair.
