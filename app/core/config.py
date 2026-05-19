from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    database_url: str = "postgresql+psycopg://doxwenju:doxwenju@localhost:5432/doxwenju"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    max_docx_bytes: int = Field(default=50 * 1024 * 1024)
    max_zip_uncompressed_bytes: int = Field(default=200 * 1024 * 1024)
    max_zip_compression_ratio: int = 100

    gemini_api_key: str | None = None
    gemini_embedding_model: str = "gemini-embedding-001"
    gemini_embedding_dimensions: int = 1536
    gemini_embedding_timeout_seconds: int = 30
    gemini_embedding_max_atoms_per_document: int = 200
    gemini_rerank_enabled: bool = True
    gemini_rerank_model: str = "gemini-2.5-flash"
    gemini_rerank_timeout_seconds: int = 45
    gemini_rerank_max_elements_per_run: int = 50
    gemini_repair_enabled: bool = True
    gemini_repair_model: str = "gemini-2.5-flash"
    gemini_repair_timeout_seconds: int = 45
    gemini_repair_max_operations_per_run: int = 30
    libreoffice_path: str | None = None
    libreoffice_timeout_seconds: int = 120
    moonshot_api_key: str | None = None
    microsoft_graph_tenant_id: str | None = None
    microsoft_graph_client_id: str | None = None
    microsoft_graph_client_secret: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
