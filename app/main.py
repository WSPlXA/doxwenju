from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.targets import router as targets_router
from app.api.templates import router as templates_router

app = FastAPI(title="DOCX Visual Consistency Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(templates_router)
app.include_router(targets_router)

_STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html", media_type="text/html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
