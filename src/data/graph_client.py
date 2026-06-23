"""Microsoft Graph API client for ATLAS.

Wraps the OAuth2 `client_credentials` flow plus a couple of generic HTTP
helpers so the SharePoint / Outlook / Teams-chat data modules don't each
re-implement token management.

The same `GraphClient` instance is created once at `bot_server` startup
and shared across all Graph-backed tools. Tokens are cached in memory
until shortly before their expiry, then refreshed transparently on the
next request.

Requires three values from `.env`:
  - `BOT_APP_ID`        (the app registration's Application/Client ID)
  - `BOT_APP_PASSWORD`  (the client secret VALUE — not its ID)
  - `BOT_TENANT_ID`     (your Azure AD tenant ID; required for Single-Tenant)

The app must have the corresponding Microsoft Graph *Application* permissions
granted with admin consent — e.g. `Sites.Read.All`, `Files.Read.All`,
`Mail.Read`, `Chat.Read.All`.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

from src.utils.logger import get_logger

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Refresh the token this many seconds before its real expiry to avoid
# races where a request is in flight when the token expires.
_TOKEN_REFRESH_BUFFER_SEC = 60

# Request-side timeouts. Token endpoint should be fast; API endpoints
# may legitimately take a few seconds for search-style operations.
_TOKEN_TIMEOUT_SEC = 15
_API_TIMEOUT_SEC = 30


class GraphError(Exception):
    """Raised when a Microsoft Graph API call fails."""


class GraphClient:
    """Thin Microsoft Graph client with OAuth2 client_credentials auth."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        log_level: str = "INFO",
    ) -> None:
        """Construct the client.

        Args:
            tenant_id: Azure AD tenant GUID.
            client_id: App registration's Application (client) ID.
            client_secret: Client secret VALUE (not its display name / ID).
            log_level: Logger verbosity for token + request events.

        Raises:
            ValueError: If any required credential is missing.
        """
        if not tenant_id or not client_id or not client_secret:
            raise ValueError(
                "GraphClient requires non-empty tenant_id, client_id, and client_secret. "
                "Check BOT_APP_ID / BOT_APP_PASSWORD / BOT_TENANT_ID in .env."
            )

        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._log = get_logger(__name__, log_level)

    # ------------------------------------------------------------------
    # Public HTTP helpers
    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Issue a GET against Graph and return the parsed JSON body.

        `extra_headers` lets callers add Graph-specific headers such as
        `ConsistencyLevel: eventual` which is required for `$search` on
        the messages endpoint.
        """
        return self._request("GET", path, params=params, extra_headers=extra_headers)

    def post(
        self,
        path: str,
        json_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Issue a POST against Graph and return the parsed JSON body."""
        return self._request("POST", path, json_body=json_body, extra_headers=extra_headers)

    def search(
        self,
        query_string: str,
        entity_types: list[str],
        size: int = 25,
        region: str = "NAM",
    ) -> dict[str, Any]:
        """Run a Microsoft Search query across the given entity types.

        Args:
            query_string: KQL-style query (e.g. `"cleaning SOP"`).
            entity_types: Entities to search, e.g. `["driveItem"]` for files,
                `["message"]` for emails, `["chatMessage"]` for Teams chats,
                `["listItem"]` for SharePoint list items.
            size: Max results per entity type.
            region: Geographic region of the M365 tenant - REQUIRED when
                using Application (app-only) permissions. Valid values:
                NAM (North America), EUR (Europe), APC (Asia-Pacific),
                AUS (Australia), GBR (United Kingdom), CAN (Canada),
                IND (India), JPN (Japan), CHE (Switzerland).
                For delegated auth this field is ignored.

        Returns:
            The Graph `/search/query` response body.
        """
        body = {
            "requests": [
                {
                    "entityTypes": entity_types,
                    "query": {"queryString": query_string},
                    "region": region,
                    "from": 0,
                    "size": size,
                }
            ]
        }
        return self.post("/search/query", json_body=body)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a valid bearer token, refreshing if it's near expiry."""
        now = time.time()
        if self._token and now < self._token_expires_at - _TOKEN_REFRESH_BUFFER_SEC:
            return self._token

        url = _TOKEN_URL_TEMPLATE.format(tenant=self._tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": _GRAPH_DEFAULT_SCOPE,
        }

        try:
            response = requests.post(url, data=data, timeout=_TOKEN_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise GraphError(f"Token endpoint unreachable: {exc}") from exc

        if not response.ok:
            # AAD returns helpful error JSON; surface it for fast debugging.
            raise GraphError(
                f"Token fetch failed ({response.status_code}): {response.text[:400]}"
            )

        payload = response.json()
        self._token = payload["access_token"]
        # AAD `expires_in` is in seconds; default to 1h if missing.
        self._token_expires_at = now + int(payload.get("expires_in", 3600))
        self._log.info(
            "Graph token acquired (expires in %ds)", int(payload.get("expires_in", 3600))
        )
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Execute one HTTP call, retrying once on 401 (token race)."""
        url = path if path.startswith("https://") else f"{_GRAPH_BASE}{path}"

        def _do_request(token: str) -> requests.Response:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)
            return requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=_API_TIMEOUT_SEC,
            )

        try:
            response = _do_request(self._get_token())
            # Token can theoretically expire between fetch and request;
            # one transparent retry covers that edge case.
            if response.status_code == 401:
                self._log.info("Graph returned 401; refreshing token and retrying")
                self._token = None
                response = _do_request(self._get_token())
        except requests.RequestException as exc:
            raise GraphError(f"Graph {method} {path} failed: {exc}") from exc

        if not response.ok:
            raise GraphError(
                f"Graph {method} {path} -> {response.status_code}: {response.text[:400]}"
            )

        if not response.content:
            return {}
        return response.json()
