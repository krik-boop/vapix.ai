"""hh.ru employer API client (read-only).

Spec: https://api.hh.ru/openapi/specification/public (OpenAPI), docs: https://api.hh.ru/openapi/redoc
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import ConfigError, Settings, require
from app.integrations.hh import oauth
from app.integrations.http import ApiError, ReadOnlyClient

logger = logging.getLogger(__name__)

MAX_PAGES = 10_000
ACTIVE_VACANCIES_PER_PAGE = 50  # documented maximum
ARCHIVED_VACANCIES_PER_PAGE = 100  # documented maximum is 1000
NEGOTIATIONS_PER_PAGE = 50  # documented maximum


class HhClient:
    def __init__(
        self,
        settings: Settings,
        *,
        session: Session | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        require(settings, "hh_user_agent")
        self.settings = settings
        self._session = session
        self._transport = transport
        self._tokens = oauth.load_tokens(settings, session)
        kwargs = {"sleep": sleep} if sleep else {}
        self._http = ReadOnlyClient(
            settings.hh_api_base_url,
            {
                "Authorization": f"Bearer {self._tokens.access_token}",
                # hh.ru requires User-Agent (or HH-User-Agent) "AppName/1.0 (email)".
                "User-Agent": settings.hh_user_agent,
                "HH-User-Agent": settings.hh_user_agent,
                "Accept": "application/json",
            },
            rps=settings.http_rps,
            max_retries=settings.http_max_retries,
            backoff_base=settings.http_backoff_base,
            backoff_max=settings.http_backoff_max,
            timeout=settings.http_timeout,
            transport=transport,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HhClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.get(url, params=params)
        except ApiError as exc:
            if not (self.settings.hh_token_auto_refresh and oauth.is_token_expired_error(exc.status_code, exc.body)):
                raise
            self._tokens = oauth.refresh_tokens(self.settings, self._session, self._tokens, transport=self._transport)
            self._http.set_header("Authorization", f"Bearer {self._tokens.access_token}")
            response = self._http.get(url, params=params)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def iter_pages(self, url: str, params: dict[str, Any] | None = None, per_page: int = 50) -> Iterator[list[dict]]:
        """Standard hh.ru pagination: page from 0, stop at `pages`."""
        for page in range(MAX_PAGES):
            data = self._get_json(url, {**(params or {}), "page": page, "per_page": per_page})
            items = data.get("items") or []
            if items:
                yield items
            pages = data.get("pages")
            if not items or not isinstance(pages, int) or page + 1 >= pages:
                return
        logger.error("Stopped paging %s after %d pages", httpx.URL(url).path, MAX_PAGES)

    # --- endpoints -----------------------------------------------------------------

    def get_employer_id(self) -> str:
        if self.settings.hh_employer_id:
            return self.settings.hh_employer_id
        # TODO(hh-docs): in the OpenAPI spec the `employer` object of GET /me (MeManagerProfile)
        # is marked deprecated and its properties are not described. We read employer.id from it,
        # but set HH_EMPLOYER_ID explicitly to avoid relying on it.
        me = self._get_json("/me")
        employer_id = (me.get("employer") or {}).get("id")
        if not employer_id:
            raise ConfigError("Could not determine employer id from GET /me; set HH_EMPLOYER_ID")
        return str(employer_id)

    def iter_active_vacancies(self, employer_id: str) -> Iterator[list[dict]]:
        # GET /employers/{employer_id}/vacancies/active
        params = {"all_accessible": "true"} if self.settings.hh_vacancies_all_accessible else {}
        return self.iter_pages(f"/employers/{employer_id}/vacancies/active", params, ACTIVE_VACANCIES_PER_PAGE)

    def iter_archived_vacancies(self, employer_id: str) -> Iterator[list[dict]]:
        # GET /employers/{employer_id}/vacancies/archived (current manager's vacancies)
        return self.iter_pages(f"/employers/{employer_id}/vacancies/archived", None, ARCHIVED_VACANCIES_PER_PAGE)

    def get_negotiation_collections(self, vacancy_id: str) -> list[dict]:
        # GET /negotiations?vacancy_id=... -> collections[] with url to each collection
        data = self._get_json("/negotiations", {"vacancy_id": vacancy_id})
        return [c for c in data.get("collections") or [] if isinstance(c, dict)]

    def iter_negotiations(self, collection: dict, vacancy_id: str) -> Iterator[list[dict]]:
        # Per docs the URL must be taken from collections[].url, e.g. /negotiations/response?vacancy_id=..
        url = collection.get("url") or f"/negotiations/{collection.get('id')}"
        params = {} if "vacancy_id=" in url else {"vacancy_id": vacancy_id}
        return self.iter_pages(url, params, NEGOTIATIONS_PER_PAGE)
