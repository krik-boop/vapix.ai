"""hh.ru OAuth token handling.

The data client is GET-only. Refreshing an expired access token is the single exception:
hh.ru only supports `POST https://api.hh.ru/token` (grant_type=refresh_token) for that.
It is disabled by default and runs only when HH_TOKEN_AUTO_REFRESH=true.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.config import ConfigError, Settings
from app.integrations.utils import utcnow
from app.models import HhOAuthToken

logger = logging.getLogger(__name__)

TOKEN_ROW_ID = 1


class TokenRefreshError(RuntimeError):
    pass


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str | None


def load_tokens(settings: Settings, session: Session | None) -> TokenPair:
    """DB-stored (refreshed) tokens win over .env, because refresh tokens are single-use."""
    if session is not None:
        row = session.get(HhOAuthToken, TOKEN_ROW_ID)
        if row is not None and row.access_token:
            return TokenPair(row.access_token, row.refresh_token)
    if settings.hh_access_token is None:
        raise ConfigError("Missing required environment variables: HH_ACCESS_TOKEN")
    refresh = settings.hh_refresh_token.get_secret_value() if settings.hh_refresh_token else None
    return TokenPair(settings.hh_access_token.get_secret_value(), refresh)


def is_token_expired_error(status_code: int, body: str) -> bool:
    # Documented error values: oauth_error "token-expired" / errors[].value "token_expired".
    return status_code in (401, 403) and ("token-expired" in body or "token_expired" in body)


def refresh_tokens(
    settings: Settings,
    session: Session | None,
    current: TokenPair,
    *,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], datetime] = utcnow,
) -> TokenPair:
    if not current.refresh_token:
        raise TokenRefreshError("hh.ru access token expired and no refresh token is configured")
    data = {"grant_type": "refresh_token", "refresh_token": current.refresh_token}
    # TODO(hh-docs): the OpenAPI example for grant_type=refresh_token sends only
    # grant_type + refresh_token; client_id/client_secret are sent too, as for the other grants.
    if settings.hh_client_id and settings.hh_client_secret:
        data["client_id"] = settings.hh_client_id.get_secret_value()
        data["client_secret"] = settings.hh_client_secret.get_secret_value()
    headers = {"HH-User-Agent": settings.hh_user_agent or "", "User-Agent": settings.hh_user_agent or ""}
    with httpx.Client(base_url=settings.hh_api_base_url, timeout=settings.http_timeout, transport=transport) as http:
        response = http.post("/token", data=data, headers=headers)
    if response.status_code != 200:
        # Do not include the body: it may echo request parameters.
        raise TokenRefreshError(f"hh.ru token refresh failed with HTTP {response.status_code}")
    payload = response.json()
    pair = TokenPair(payload["access_token"], payload.get("refresh_token"))
    if session is not None:
        expires_in = payload.get("expires_in")
        row = session.get(HhOAuthToken, TOKEN_ROW_ID) or HhOAuthToken(id=TOKEN_ROW_ID)
        row.access_token = pair.access_token
        row.refresh_token = pair.refresh_token
        row.expires_at = now() + timedelta(seconds=int(expires_in)) if expires_in else None
        row.updated_at = now()
        session.merge(row)
        session.commit()
    else:
        logger.warning("hh.ru tokens refreshed but not persisted (no DB session)")
    logger.info("hh.ru access token refreshed")
    return pair
