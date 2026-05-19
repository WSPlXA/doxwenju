from fastapi import FastAPI

from app.api.targets import router as targets_router
from app.api.templates import router as templates_router

app = FastAPI(title="DOCX Visual Consistency Backend", version="0.1.0")
app.include_router(templates_router)
app.include_router(targets_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
