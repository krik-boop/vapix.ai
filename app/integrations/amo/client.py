"""amoCRM API v4 client (read-only).

Docs: https://www.amocrm.ru/developers/content/crm_platform/api-reference
"""

import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.integrations.http import ReadOnlyClient

logger = logging.getLogger(__name__)

PAGE_LIMIT = 250  # documented maximum for list methods
MAX_PAGES = 100_000  # safety net against a server that never stops returning "next"


class AmoClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        kwargs = {"sleep": sleep} if sleep else {}
        self._http = ReadOnlyClient(
            settings.amo_base_url(),
            {
                "Authorization": f"Bearer {settings.amo_token.get_secret_value()}",
                "Accept": "application/hal+json",
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

    def __enter__(self) -> "AmoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        response = self._http.get(path, params=params)
        # amoCRM answers 204 No Content when nothing matches.
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def iter_pages(self, path: str, embedded_key: str, params: dict[str, Any] | None = None) -> Iterator[list[dict]]:
        """Yield `_embedded[embedded_key]` page by page until there is no `_links.next`."""
        base_params = dict(params or {})
        base_params["limit"] = PAGE_LIMIT
        for page in range(1, MAX_PAGES + 1):
            data = self._get_json(path, {**base_params, "page": page})
            if not data:
                return
            items = (data.get("_embedded") or {}).get(embedded_key) or []
            if items:
                yield items
            has_next = bool(((data.get("_links") or {}).get("next") or {}).get("href"))
            if not items or not has_next:
                return
        logger.error("Stopped paging %s after %d pages", path, MAX_PAGES)

    # --- endpoints -----------------------------------------------------------------

    def iter_users(self) -> Iterator[list[dict]]:
        # GET /api/v4/users — requires admin rights.
        return self.iter_pages("/api/v4/users", "users")

    def get_pipelines(self) -> list[dict]:
        # GET /api/v4/leads/pipelines — not paginated per docs; statuses are in _embedded.statuses.
        data = self._get_json("/api/v4/leads/pipelines")
        return ((data or {}).get("_embedded") or {}).get("pipelines") or []

    def iter_leads(self, updated_from: datetime | None = None) -> Iterator[list[dict]]:
        # GET /api/v4/leads, filter[updated_at][from] per filters-api docs.
        params: dict[str, Any] = {"with": "loss_reason", "order[updated_at]": "asc"}
        if updated_from is not None:
            params["filter[updated_at][from]"] = int(updated_from.timestamp())
        return self.iter_pages("/api/v4/leads", "leads", params)

    def iter_tasks(self, updated_from: datetime | None = None) -> Iterator[list[dict]]:
        # GET /api/v4/tasks; docs: filter[updated_at] accepts a timestamp ("changed after")
        # or filter[updated_at][from|to]. Sorting by updated_at is NOT available for tasks.
        params: dict[str, Any] = {"order[id]": "asc"}
        if updated_from is not None:
            params["filter[updated_at][from]"] = int(updated_from.timestamp())
        return self.iter_pages("/api/v4/tasks", "tasks", params)

    def iter_notes(self, entity_type: str = "leads", updated_from: datetime | None = None) -> Iterator[list[dict]]:
        # GET /api/v4/{entity_type}/notes; docs: filter[updated_at] = timestamp ("changed after").
        params: dict[str, Any] = {"order[updated_at]": "asc"}
        if updated_from is not None:
            params["filter[updated_at]"] = int(updated_from.timestamp())
        return self.iter_pages(f"/api/v4/{entity_type}/notes", "notes", params)

    # TODO(amo-docs): a list endpoint for loss reasons (e.g. GET /api/v4/leads/loss_reasons)
    # is not described in the public API v4 docs (only a `_links.self` href pointing to
    # /api/v4/leads/loss_reasons/{id} appears in examples). Loss reasons are therefore
    # collected from leads fetched with `with=loss_reason` (documented `_embedded.loss_reason`).
