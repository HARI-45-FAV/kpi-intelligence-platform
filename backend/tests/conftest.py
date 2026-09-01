"""Test harness.

Each test module gets an isolated platform database and an isolated source
fixture, so nothing leaks between tests and the golden test's assertions about
counts and statuses are stable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Settings are read at import time, so the environment must be set before any
# application module loads.
_TMP = Path(__file__).resolve().parent / "_tmp"
_TMP.mkdir(exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP / 'test_platform.db').as_posix()}")
os.environ.setdefault("DOCUMENT_STORAGE_DIR", str(_TMP / "documents"))
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-0123456789")
os.environ.setdefault("ENVIRONMENT", "test")

# The Copilot suite's premise is a deployment with no model configured, and the
# tests that need one enable it per-test by patching ``settings`` directly (see
# the ``scripted`` fixture). Pinned rather than defaulted: a developer's local
# .env pointing at a real endpoint (Ollama, vLLM) would otherwise make the
# suite assert against whatever model happens to be running, and an
# LLM_ENABLED=true there silently inverts the "no model configured" tests.
# Environment variables outrank the dotenv file in pydantic-settings, so this
# wins over .env while still leaving CI free to set it before pytest starts.
os.environ["LLM_ENABLED"] = "false"

# The transport is pinned for the same reason. The tests that enable a model
# script the *provider* and patch the ``llm_*`` settings around it, so a .env
# selecting the hosted transport -- which reads ``GEMINI_*`` instead -- would
# leave those patches unread and the suite asserting against a configuration no
# test wrote. Which transport a deployment uses is covered on its own, in
# ``test_gemini_transport.py`` and ``test_copilot.py``'s replay tests.
os.environ["LLM_PROVIDER"] = "openai_compatible"

# Tool calling is pinned on for the same reason, in the other direction. It is a
# capability of the governed tool layer rather than of any one model, so the
# suite asserts the layer's behaviour -- which tools a role is offered, which
# ones refuse -- independently of whether a developer has turned tool calling off
# locally to save round-trips against a small local model.
os.environ["LLM_TOOL_CALLING_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.bootstrap import sync_reference_data  # noqa: E402
from tests.fixture_source import build_fixture_database  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _platform_database() -> Iterator[None]:
    db_path = Path(settings.database_url.replace("sqlite:///", ""))
    if db_path.exists():
        db_path.unlink()
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        sync_reference_data(session)
        session.commit()
    finally:
        session.close()
    yield
    engine.dispose()


@pytest.fixture(scope="session")
def source_fixture() -> dict:
    """Build the NovaMart-shaped source database once per test session."""
    return build_fixture_database(_TMP / "novamart_source.db")


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


class ApiActor:
    """A signed-in user, with its bearer token applied to every request."""

    def __init__(self, client: TestClient, token: str, email: str) -> None:
        self.client = client
        self.token = token
        self.email = email

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, url: str, **kwargs):
        return self.client.get(url, headers=self.headers, **kwargs)

    def post(self, url: str, **kwargs):
        return self.client.post(url, headers=self.headers, **kwargs)

    def patch(self, url: str, **kwargs):
        return self.client.patch(url, headers=self.headers, **kwargs)

    def put(self, url: str, **kwargs):
        return self.client.put(url, headers=self.headers, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.client.delete(url, headers=self.headers, **kwargs)


def register(client: TestClient, email: str, password: str, full_name: str) -> ApiActor:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )
    assert response.status_code == 201, response.text
    return ApiActor(client, response.json()["access_token"], email)


def login(client: TestClient, email: str, password: str, company_id: str | None = None) -> ApiActor:
    body: dict[str, object] = {"email": email, "password": password}
    if company_id:
        body["company_id"] = company_id
    response = client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 200, response.text
    return ApiActor(client, response.json()["access_token"], email)


API = "/api/v1"
