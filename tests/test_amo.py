from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.integrations.amo.client import AmoClient
from app.integrations.amo.sync import AmoSync
from app.integrations.http import ApiError
from app.models import AmoLead, AmoLossReason, AmoNote, AmoPipeline, AmoStatus, AmoTask, AmoUser, SyncState
from tests.conftest import FAKE_AMO_TOKEN, json_response, sequence

BASE = "https://test-account.amocrm.ru"


def page(key: str, items: list, path: str, page_no: int = 1, has_next: bool = False) -> httpx.Response:
    links = {"self": {"href": f"{BASE}{path}?page={page_no}"}}
    if has_next:
        links["next"] = {"href": f"{BASE}{path}?page={page_no + 1}"}
    return json_response({"_page": page_no, "_links": links, "_embedded": {key: items}})


def paged(key: str, path: str, pages: list[list]):
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", "1"))
        if n > len(pages):
            return httpx.Response(204)
        return page(key, pages[n - 1], path, n, has_next=n < len(pages))
    return handler


def no_content(_request):
    return httpx.Response(204)


def lead(i: int, updated: int, **extra) -> dict:
    return {"id": i, "name": f"Lead {i}", "price": 100 * i, "pipeline_id": 1, "status_id": 10,
            "responsible_user_id": 5, "created_at": 1_700_000_000, "updated_at": updated, **extra}


@pytest.fixture
def mock_all_empty(api):
    for path in ("/api/v4/users", "/api/v4/leads/pipelines", "/api/v4/leads", "/api/v4/tasks", "/api/v4/leads/notes"):
        api.on(path, no_content)
    return api


def make_sync(session, settings, api, fake_sleep):
    client = AmoClient(settings, transport=api.transport, sleep=fake_sleep)
    return AmoSync(session, client, settings)


def test_requests_are_get_with_bearer_token(session, settings, mock_all_empty, fake_sleep):
    make_sync(session, settings, mock_all_empty, fake_sleep).run("full")
    assert mock_all_empty.requests
    for r in mock_all_empty.requests:
        assert r.method == "GET"
        assert r.url.host == "test-account.amocrm.ru"
        assert r.headers["Authorization"] == f"Bearer {FAKE_AMO_TOKEN}"


def test_empty_responses(session, settings, mock_all_empty, fake_sleep):
    report = make_sync(session, settings, mock_all_empty, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["leads"] == 0
    assert session.scalar(select(func.count()).select_from(AmoLead)) == 0
    states = {s.entity: s.last_status for s in session.scalars(select(SyncState))}
    assert states == {"users": "ok", "pipelines": "ok", "leads": "ok", "tasks": "ok", "notes_leads": "ok"}


def test_empty_embedded_list_with_200(api, settings, fake_sleep):
    api.on("/api/v4/leads", lambda r: json_response({"_embedded": {"leads": []}, "_links": {}}))
    client = AmoClient(settings, transport=api.transport, sleep=fake_sleep)
    assert list(client.iter_leads()) == []


def test_pagination_reads_all_pages(session, settings, mock_all_empty, fake_sleep):
    api = mock_all_empty
    api.on("/api/v4/leads", paged("leads", "/api/v4/leads", [
        [lead(1, 1_700_000_100), lead(2, 1_700_000_200)],
        [lead(3, 1_700_000_300)],
        [lead(4, 1_700_000_400)],
    ]))
    api.on("/api/v4/users", paged("users", "/api/v4/users", [
        [{"id": 5, "name": "Manager", "email": "m@example.com", "rights": {"is_admin": True, "is_active": True}}],
        [{"id": 6, "name": "Second"}],
    ]))
    report = make_sync(session, settings, api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["leads"] == 4
    assert [int(r.url.params["page"]) for r in api.calls("/api/v4/leads")] == [1, 2, 3]
    assert all(r.url.params["limit"] == "250" for r in api.calls("/api/v4/leads"))
    assert session.scalar(select(func.count()).select_from(AmoUser)) == 2
    assert not hasattr(AmoUser, "email")
    cursor = session.scalar(select(SyncState.cursor).where(SyncState.entity == "leads"))
    assert cursor.replace(tzinfo=timezone.utc) == datetime.fromtimestamp(1_700_000_400, timezone.utc)


def test_429_is_retried(session, settings, mock_all_empty, fake_sleep, sleeps):
    api = mock_all_empty
    api.on("/api/v4/tasks", sequence(
        json_response({"title": "Too Many Requests"}, 429),
        json_response({"title": "Too Many Requests"}, 429),
        page("tasks", [{"id": 1, "entity_id": 1, "entity_type": "leads", "updated_at": 1_700_000_000}], "/api/v4/tasks"),
    ))
    report = make_sync(session, settings, api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["tasks"] == 1
    assert len(api.calls("/api/v4/tasks")) == 3
    assert any(s >= settings.http_backoff_base for s in sleeps)


def test_persistent_429_marks_entity_failed_but_others_continue(session, settings, mock_all_empty, fake_sleep):
    api = mock_all_empty
    api.on("/api/v4/leads", sequence(json_response({}, 429)))
    api.on("/api/v4/tasks", lambda r: page("tasks", [{"id": 1, "updated_at": 1_700_000_000}], "/api/v4/tasks"))
    report = make_sync(session, settings, api, fake_sleep).run("full")
    assert "leads" in report.errors
    assert report.counts["tasks"] == 1
    state = session.scalar(select(SyncState).where(SyncState.entity == "leads"))
    assert state.last_status == "error"
    assert FAKE_AMO_TOKEN not in (state.last_error or "")


def test_incomplete_data(session, settings, mock_all_empty, fake_sleep):
    api = mock_all_empty
    api.on("/api/v4/leads", lambda r: page("leads", [
        {"id": 1},  # only id
        {"name": "no id"},  # skipped
        {"id": "2", "price": None, "updated_at": None, "_embedded": None, "custom_fields_values": None},
        "garbage",
    ], "/api/v4/leads"))
    api.on("/api/v4/leads/pipelines", lambda r: json_response({"_embedded": {"pipelines": [
        {"id": 1, "name": "Main"},  # no statuses
        {"id": 2, "_embedded": {"statuses": [{"id": 142, "name": "Won"}, {"name": "no id"}]}},
        {"id": 3, "_embedded": {"statuses": [{"id": 142, "name": "Won"}]}},
        {"name": "no id"},
    ]}}))
    api.on("/api/v4/leads/notes", lambda r: page("notes", [{"id": 9, "entity_id": 1}], "/api/v4/leads/notes"))
    report = make_sync(session, settings, api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["leads"] == 2
    assert report.skipped["leads"] == 2
    assert session.get(AmoLead, 2).price is None
    assert session.scalar(select(func.count()).select_from(AmoPipeline)) == 3
    # Status 142 exists in two pipelines -> two rows.
    assert session.scalar(select(func.count()).select_from(AmoStatus)) == 2
    assert session.get(AmoNote, ("leads", 9)).entity_id == 1
    # No updated_at seen -> cursor stays empty, next incremental is effectively full.
    assert session.scalar(select(SyncState.cursor).where(SyncState.entity == "leads")) is None


def test_loss_reasons_collected_from_leads(session, settings, mock_all_empty, fake_sleep):
    api = mock_all_empty
    api.on("/api/v4/leads", lambda r: page("leads", [
        lead(1, 1_700_000_000, loss_reason_id=7, _embedded={"loss_reason": [{"id": 7, "name": "Expensive"}]}),
        lead(2, 1_700_000_000, loss_reason_id=7, _embedded={"loss_reason": [{"id": 7, "name": "Expensive"}]}),
    ], "/api/v4/leads"))
    make_sync(session, settings, api, fake_sleep).run("full")
    assert api.calls("/api/v4/leads")[0].url.params["with"] == "loss_reason"
    assert session.get(AmoLossReason, 7).name == "Expensive"
    assert session.get(AmoLead, 1).loss_reason_id == 7


def test_incremental_uses_cursor_minus_overlap(session, settings, mock_all_empty, fake_sleep):
    api = mock_all_empty
    api.on("/api/v4/leads", lambda r: page("leads", [lead(1, 1_700_000_500)], "/api/v4/leads"))
    api.on("/api/v4/tasks", lambda r: page("tasks", [{"id": 1, "updated_at": 1_700_000_600}], "/api/v4/tasks"))
    api.on("/api/v4/leads/notes", lambda r: page("notes", [{"id": 1, "updated_at": 1_700_000_700}], "/api/v4/leads/notes"))
    make_sync(session, settings, api, fake_sleep).run("full")
    first = list(api.requests)
    assert all("filter[updated_at][from]" not in r.url.params and "filter[updated_at]" not in r.url.params
               for r in first)

    api.requests.clear()
    report = make_sync(session, settings, api, fake_sleep).run("incremental")
    assert report.ok
    overlap = settings.sync_overlap_seconds
    assert api.calls("/api/v4/leads")[0].url.params["filter[updated_at][from]"] == str(1_700_000_500 - overlap)
    assert api.calls("/api/v4/tasks")[0].url.params["filter[updated_at][from]"] == str(1_700_000_600 - overlap)
    assert api.calls("/api/v4/leads/notes")[0].url.params["filter[updated_at]"] == str(1_700_000_700 - overlap)
    # Re-reading the same lead is idempotent.
    assert session.scalar(select(func.count()).select_from(AmoLead)) == 1


def test_incremental_without_cursor_is_full(session, settings, mock_all_empty, fake_sleep):
    make_sync(session, settings, mock_all_empty, fake_sleep).run("incremental")
    assert "filter[updated_at][from]" not in mock_all_empty.calls("/api/v4/leads")[0].url.params


def test_upsert_updates_existing_rows(session, settings, mock_all_empty, fake_sleep):
    api = mock_all_empty
    api.on("/api/v4/leads", lambda r: page("leads", [lead(1, 1_700_000_000, status_id=10)], "/api/v4/leads"))
    make_sync(session, settings, api, fake_sleep).run("full")
    api.on("/api/v4/leads", lambda r: page("leads", [lead(1, 1_700_000_100, status_id=142)], "/api/v4/leads"))
    make_sync(session, settings, api, fake_sleep).run("incremental")
    session.expire_all()
    assert session.get(AmoLead, 1).status_id == 142


def test_401_is_not_retried_and_reported(api, settings, fake_sleep):
    api.on("/api/v4/users", lambda r: json_response({"title": "Unauthorized"}, 401))
    client = AmoClient(settings, transport=api.transport, sleep=fake_sleep)
    with pytest.raises(ApiError) as exc:
        list(client.iter_users())
    assert exc.value.status_code == 401
    assert len(api.requests) == 1
