# DOCX Visual Consistency Backend MVP

Current package version: `0.1.1`.

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

Run a repeatable local smoke test against the Docker API:

```bash
GEMINI_API_KEY= docker compose up -d --force-recreate api worker
./scripts/smoke_agent.sh
```

The script uploads the bundled fixture DOCX files, waits for template and target ingestion,
runs the agent, verifies output downloads, and writes smoke artifacts to `/tmp/doxwenju-smoke`.
It treats both `done` and `needs_human` as valid agent terminal states because visual drift or
unsupported operations can require manual review after the deterministic pipeline completes.

## Agent Run Modes

The production agent has three execution layers:

- Deterministic OOXML patching runs inside Docker and is the portable baseline.
- Open XML layout postprocess is the default production path. It directly updates the DOCX
  package for page setup, cover table spacing, header/footer formatting, TOC field insertion,
  and `w:updateFields`.
- LibreOffice headless refresh stores a DOCX that has been reopened/resaved by a server-side
  layout engine, then render precheck exports PDF.

This path runs on Linux servers and does not require Microsoft Word:

```bash
LAYOUT_POSTPROCESS_ENGINE=ooxml
docker compose up --build
```

`POST /targets/latest/agent-run?max_rounds=3` then runs:

```text
patch -> Open XML layout postprocess -> LibreOffice DOCX refresh -> LibreOffice PDF render
```

Word COM is retained only as a debug fallback. To force the old Word path on a Windows worker:

```powershell
pip install pywin32
$env:LAYOUT_POSTPROCESS_ENGINE = "word_com"
$env:WORD_POSTPROCESS_TIMEOUT_SECONDS = "120"
celery -A app.core.celery_app worker --loglevel=info --pool=solo
```

If you want the app server itself to do the whole run synchronously, run the API on Windows
instead of Docker and enable inline agent execution:

```powershell
pip install pywin32
$env:DATABASE_URL = "postgresql+psycopg://doxwenju:doxwenju@localhost:15432/doxwenju"
$env:CELERY_BROKER_URL = "redis://localhost:6379/0"
$env:CELERY_RESULT_BACKEND = "redis://localhost:6379/1"
$env:AGENT_RUN_INLINE = "true"
$env:LAYOUT_POSTPROCESS_ENGINE = "ooxml"
$env:WORD_POSTPROCESS_TIMEOUT_SECONDS = "120"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In inline mode `POST /targets/latest/agent-run?max_rounds=3` blocks until the full agent run
finishes. The tradeoff is that one request occupies an API worker for the full patch,
postprocess, DOCX refresh, and render pass, so this mode is for local MVP verification or
single-user operation, not concurrent production traffic.

## CI

The default GitHub Actions workflow runs the portable checks:

- install Python dependencies
- `ruff check .`
- `alembic upgrade head` against PostgreSQL with pgvector
- `pytest -q`
- Docker image build

Word COM layout postprocess is intentionally not part of the hosted Linux CI path. The default
CI path should validate the Open XML postprocess and LibreOffice render path. Microsoft Word
automation requires a Windows desktop/server process with Office installed, so Word fallback
verification needs a Windows runner or a local smoke run:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://doxwenju:doxwenju@localhost:15432/doxwenju"
$env:AGENT_RUN_INLINE = "true"
$env:LAYOUT_POSTPROCESS_ENGINE = "word_com"
$env:WORD_POSTPROCESS_TIMEOUT_SECONDS = "120"
python -m pytest -q
python -m app.services.word_postprocess input.docx output.docx summary.json
```

For a production-grade CI gate, add a `self-hosted` Windows runner with Microsoft Word installed
and run one fixture DOCX through `python -m app.services.word_postprocess`. Keep that job separate
from the normal Linux checks because Word COM is slow, stateful, and tied to the Windows desktop
automation boundary.

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
- Default Open XML layout postprocess for page setup, cover table spacing, header/footer
  formatting, TOC field insertion, and `w:updateFields`, followed by LibreOffice headless
  DOCX refresh.
- Optional Word COM layout postprocess fallback for local debugging on Windows with pywin32
  and Microsoft Word installed.
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
