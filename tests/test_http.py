import httpx
import pytest

from app.integrations.http import ApiError, RateLimiter, ReadOnlyClient, ReadOnlyViolation
from tests.conftest import json_response, sequence


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_spaces_calls_to_5_per_second():
    clock = FakeClock()
    limiter = RateLimiter(5, clock=clock, sleep=clock.sleep)
    start = clock.now
    for _ in range(11):
        limiter.acquire()
    # 11 calls at 5 rps need at least 2 seconds.
    assert clock.now - start == pytest.approx(2.0)
    assert all(s == pytest.approx(0.2) for s in clock.slept)


def test_client_caps_rps_at_5(api, fake_sleep):
    client = ReadOnlyClient("https://x.test", {}, rps=50, transport=api.transport, sleep=fake_sleep)
    assert client._limiter.min_interval == pytest.approx(0.2)


def _client(api, sleeps, **kw):
    return ReadOnlyClient("https://x.test", {}, transport=api.transport, sleep=sleeps.append,
                          backoff_base=1.0, backoff_max=60, **kw)


def test_retries_429_with_exponential_backoff_then_succeeds(api, sleeps):
    api.on("/a", sequence(
        json_response({}, 429), json_response({}, 429), json_response({}, 503), json_response({"ok": 1})))
    resp = _client(api, sleeps, max_retries=5).get("/a")
    assert resp.json() == {"ok": 1}
    assert len(api.calls("/a")) == 4
    backoffs = [s for s in sleeps if s >= 1.0]
    assert backoffs == [1.0, 2.0, 4.0]


def test_retry_after_header_is_respected(api, sleeps):
    api.on("/a", sequence(json_response({}, 429, {"Retry-After": "7"}), json_response({"ok": 1})))
    _client(api, sleeps).get("/a")
    assert 7.0 in sleeps


def test_gives_up_after_max_retries(api, sleeps):
    api.on("/a", sequence(json_response({"e": 1}, 500)))
    with pytest.raises(ApiError) as exc:
        _client(api, sleeps, max_retries=2).get("/a")
    assert exc.value.status_code == 500
    assert len(api.calls("/a")) == 3


def test_4xx_is_not_retried(api, sleeps):
    api.on("/a", sequence(json_response({"e": 1}, 401)))
    with pytest.raises(ApiError):
        _client(api, sleeps).get("/a")
    assert len(api.calls("/a")) == 1


def test_network_errors_are_retried(api, sleeps):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return json_response({"ok": 1})

    api.on("/a", flaky)
    assert _client(api, sleeps).get("/a").json() == {"ok": 1}


def test_only_get_is_possible(api, sleeps):
    client = _client(api, sleeps)
    assert not hasattr(client, "post")
    # Even the underlying httpx client refuses non-GET methods.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ReadOnlyViolation):
            client._http.request(method, "/a")
    assert api.requests == []


def test_foreign_absolute_url_is_refused(api, sleeps):
    with pytest.raises(ReadOnlyViolation):
        _client(api, sleeps).get("https://evil.test/steal")
    assert api.requests == []
