import json
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.db import Base

# Fake values only. No real accounts are used in tests.
FAKE_AMO_TOKEN = "amo-fake-token-123456"
FAKE_HH_TOKEN = "hh-fake-access-123456"
FAKE_HH_REFRESH = "hh-fake-refresh-123456"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        amo_domain="test-account.amocrm.ru",
        amo_token=FAKE_AMO_TOKEN,
        hh_client_id="fake-client-id",
        hh_client_secret="fake-client-secret",
        hh_access_token=FAKE_HH_TOKEN,
        hh_refresh_token=FAKE_HH_REFRESH,
        hh_user_agent="ai-business-os-tests/1.0 (tests@example.com)",
        hh_employer_id="777",
        http_max_retries=3,
        http_backoff_base=0.01,
        sync_overlap_seconds=60,
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s
    engine.dispose()


class FakeApi:
    """Routes requests to handlers and records every request made."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, path: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[path] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"error": "not mocked", "path": request.url.path})
        return handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def json_response(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={
        "Content-Type": "application/json", **(headers or {})})


def sequence(*responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
    """Return responses in order; the last one repeats."""
    items = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = items.pop(0) if len(items) > 1 else items[0]
        return item(request) if callable(item) else item

    return handler


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def fake_sleep(sleeps):
    return sleeps.append
