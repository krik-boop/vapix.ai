import json

import httpx
import pytest
from sqlalchemy import func, inspect, select

from app.integrations.hh.client import HhClient
from app.integrations.hh.sync import HhSync
from app.models import HhCandidate, HhNegotiation, HhOAuthToken, HhVacancy, SyncState
from tests.conftest import FAKE_HH_TOKEN, json_response, sequence

API = "https://api.hh.ru"
EMPLOYER = "777"
ACTIVE = f"/employers/{EMPLOYER}/vacancies/active"
ARCHIVED = f"/employers/{EMPLOYER}/vacancies/archived"

PII = {
    "first_name": "Ivan", "last_name": "Petrov", "middle_name": "Ivanovich",
    "photo": {"small": "https://img.hh.ru/photo.jpg"},
    "age": 33, "birth_date": "1990-01-01", "gender": {"id": "male", "name": "Male"},
    "phone": "+79990000000", "email": "ivan@example.com",
    "contact": [{"type": {"id": "cell"}, "value": {"formatted": "+7 999 000-00-00"}}],
}


def items_page(items, page=0, pages=1, per_page=50):
    return json_response({"items": items, "found": len(items), "page": page, "pages": pages, "per_page": per_page})


def paged(pages: list[list]):
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", "0"))
        items = pages[n] if n < len(pages) else []
        return items_page(items, n, len(pages))
    return handler


def vacancy(i: str, **extra):
    return {"id": i, "name": f"Vacancy {i}", "area": {"id": "1", "name": "Moscow"},
            "salary": {"from": 100000, "to": None, "currency": "RUR"}, "archived": False,
            "published_at": "2026-01-10T10:00:00+0300", **extra}


def negotiation(i: str, updated: str, resume: dict | None = None):
    n = {"id": i, "state": {"id": "response"}, "created_at": "2026-01-11T10:00:00+0300",
         "updated_at": updated, "source": "hh"}
    if resume is not None:
        n["resume"] = resume
    return n


def resume(rid: str, **extra):
    return {
        "id": rid, "title": "Python dev", "area": {"id": "1", "name": "Moscow"},
        "salary": {"amount": 250000, "currency": "RUR"}, "total_experience": {"months": 60},
        "experience": [{"position": "Developer", "company": "ACME", "start": "2020-01-01", "end": None,
                        "industry": {"id": "7", "name": "IT"}, "description": "secret details",
                        "company_url": "https://acme.example"}],
        "skill_set": ["Python", "SQL"],
        "updated_at": "2026-01-09T10:00:00+0300",
        **PII, **extra,
    }


def collections(vacancy_id: str, ids=("response",)):
    return json_response({"collections": [
        {"id": c, "name": c, "url": f"{API}/negotiations/{c}?vacancy_id={vacancy_id}"} for c in ids]})


@pytest.fixture
def hh_api(api):
    api.on(ACTIVE, lambda r: items_page([]))
    api.on(ARCHIVED, lambda r: items_page([]))
    api.on("/negotiations", lambda r: collections(r.url.params["vacancy_id"]))
    api.on("/negotiations/response", lambda r: items_page([]))
    return api


def make_sync(session, settings, api, fake_sleep):
    client = HhClient(settings, session=session, transport=api.transport, sleep=fake_sleep)
    return HhSync(session, client, settings)


def test_requests_are_get_with_auth_and_user_agent(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, lambda r: items_page([vacancy("1")]))
    make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert hh_api.requests
    for r in hh_api.requests:
        assert r.method == "GET"
        assert r.url.host == "api.hh.ru"
        assert r.headers["Authorization"] == f"Bearer {FAKE_HH_TOKEN}"
        assert r.headers["HH-User-Agent"] == settings.hh_user_agent


def test_empty_responses(session, settings, hh_api, fake_sleep):
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts == {"vacancies": 0, "negotiations": 0}
    assert not hh_api.calls("/negotiations")


def test_pagination_vacancies_and_negotiations(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, paged([[vacancy("1"), vacancy("2")], [vacancy("3")]]))
    hh_api.on(ARCHIVED, paged([[vacancy("4", archived=True)]]))
    hh_api.on("/negotiations/response", lambda r: paged([
        [negotiation(f"{r.url.params['vacancy_id']}-a", "2026-02-01T10:00:00+0300", resume("r1"))],
        [negotiation(f"{r.url.params['vacancy_id']}-b", "2026-02-02T10:00:00+0300", resume("r2"))],
    ])(r))
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["vacancies"] == 4
    assert [r.url.params["page"] for r in hh_api.calls(ACTIVE)] == ["0", "1"]
    assert hh_api.calls(ACTIVE)[0].url.params["per_page"] == "50"
    assert report.counts["negotiations"] == 8  # 4 vacancies x 2 pages
    assert session.get(HhVacancy, "4").archived is True
    assert session.scalar(select(func.count()).select_from(HhCandidate)) == 2


def test_429_is_retried(session, settings, hh_api, fake_sleep, sleeps):
    hh_api.on(ACTIVE, sequence(
        json_response({"errors": [{"type": "too_many_requests"}]}, 429, {"Retry-After": "2"}),
        items_page([vacancy("1")]),
    ))
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["vacancies"] == 1
    assert len(hh_api.calls(ACTIVE)) == 2
    assert 2.0 in sleeps


def test_5xx_exhausted_marks_failed(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, sequence(json_response({}, 502)))
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert "vacancies" in report.errors
    assert len(hh_api.calls(ACTIVE)) == settings.http_max_retries + 1
    assert session.scalar(select(SyncState.last_status).where(SyncState.entity == "vacancies")) == "error"


def test_candidate_pii_is_not_stored(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, lambda r: items_page([vacancy("1")]))
    hh_api.on("/negotiations/response", lambda r: items_page(
        [negotiation("n1", "2026-02-01T10:00:00+0300", resume("r1"))]))
    make_sync(session, settings, hh_api, fake_sleep).run("full")

    c = session.get(HhCandidate, "r1")
    assert c.city == "Moscow"
    assert (c.desired_salary_amount, c.desired_salary_currency) == (250000, "RUR")
    assert c.total_experience_months == 60
    assert c.skills == ["Python", "SQL"]
    assert c.experience == [{"position": "Developer", "company": "ACME", "start": "2020-01-01",
                             "end": None, "industry": "IT"}]

    columns = {col.key for col in inspect(HhCandidate).columns}
    for forbidden in ("first_name", "last_name", "name", "photo", "birth_date", "age", "gender", "phone", "email"):
        assert forbidden not in columns
    dumped = json.dumps([{col: str(getattr(c, col)) for col in columns}], ensure_ascii=False)
    for value in ("Ivan", "Petrov", "photo.jpg", "1990-01-01", "male", "+7999", "ivan@example.com", "secret details"):
        assert value not in dumped


def test_incomplete_data(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, lambda r: items_page([{"id": "1"}, {"name": "no id"}, "garbage"]))
    hh_api.on("/negotiations/response", lambda r: items_page([
        {"id": "n1"},  # no dates, no resume
        {"id": "n2", "updated_at": "not-a-date", "resume": {"id": "r2"}},  # resume without any fields
        {"id": "n3", "resume": {"id": "r3", "salary": None, "area": None, "experience": None,
                                "skill_set": None, "total_experience": None}},
        {"state": {"id": "response"}},  # no id
    ]))
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert report.counts["vacancies"] == 1
    assert report.skipped == {"vacancies": 2, "negotiations": 1}
    assert session.scalar(select(func.count()).select_from(HhNegotiation)) == 3
    assert session.get(HhNegotiation, "n1").resume_id is None
    r2 = session.get(HhCandidate, "r2")
    assert (r2.city, r2.desired_salary_amount, r2.skills, r2.experience) == (None, None, None, None)
    assert session.get(HhCandidate, "r3").total_experience_months is None


def test_incremental_skips_old_negotiations_and_archived(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, lambda r: items_page([vacancy("1")]))
    hh_api.on("/negotiations/response", lambda r: items_page([
        negotiation("old", "2026-02-01T10:00:00+0300", resume("r-old")),
        negotiation("new", "2026-02-10T10:00:00+0300", resume("r-new")),
    ]))
    make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert hh_api.calls(ARCHIVED)

    session.query(HhNegotiation).delete()
    session.query(HhCandidate).delete()
    session.commit()
    hh_api.requests.clear()
    hh_api.on("/negotiations/response", lambda r: items_page([
        negotiation("old", "2026-02-01T10:00:00+0300", resume("r-old")),
        negotiation("new", "2026-02-10T10:00:00+0300", resume("r-new")),
        negotiation("newer", "2026-02-11T10:00:00+0300", resume("r-newer")),
    ]))
    report = make_sync(session, settings, hh_api, fake_sleep).run("incremental")
    assert report.ok, report.errors
    # cursor = 2026-02-10 minus overlap: "old" skipped, "new" re-read (overlap), "newer" written.
    assert set(session.scalars(select(HhNegotiation.id))) == {"new", "newer"}
    assert session.get(HhCandidate, "r-old") is None
    assert not hh_api.calls(ARCHIVED)


def test_employer_id_from_me_when_not_configured(session, settings, hh_api, fake_sleep):
    settings.hh_employer_id = None
    hh_api.on("/me", lambda r: json_response({"auth_type": "employer", "employer": {"id": EMPLOYER}}))
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert report.ok, report.errors
    assert hh_api.calls("/me")


def test_token_refresh_is_off_by_default(session, settings, hh_api, fake_sleep):
    hh_api.on(ACTIVE, lambda r: json_response({"oauth_error": "token-expired"}, 403))
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert "vacancies" in report.errors
    assert all(r.method == "GET" for r in hh_api.requests)


def test_token_refresh_when_enabled(session, settings, hh_api, fake_sleep):
    settings.hh_token_auto_refresh = True

    def active(request):
        if request.headers["Authorization"] == f"Bearer {FAKE_HH_TOKEN}":
            return json_response({"oauth_error": "token-expired"}, 403)
        return items_page([vacancy("1")])

    def token(request):
        assert request.method == "POST"
        body = dict(x.split("=") for x in request.content.decode().split("&"))
        assert body["grant_type"] == "refresh_token"
        return json_response({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1209600})

    hh_api.on(ACTIVE, active)
    hh_api.on("/token", token)
    report = make_sync(session, settings, hh_api, fake_sleep).run("full")
    assert report.ok, report.errors
    row = session.get(HhOAuthToken, 1)
    assert (row.access_token, row.refresh_token) == ("new-access", "new-refresh")
    non_get = [r for r in hh_api.requests if r.method != "GET"]
    assert [r.url.path for r in non_get] == ["/token"]
