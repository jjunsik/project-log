from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from project_log.api import create_app
from project_log.service import Pilot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def prohibit_live_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not invoke external AI")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture
def pilot(tmp_path: Path) -> Pilot:
    return Pilot(ROOT, tmp_path / "records")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(ROOT, tmp_path / "api-records"))
