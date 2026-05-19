from app.core.config import settings


def pytest_configure():
    settings.gemini_api_key = None
