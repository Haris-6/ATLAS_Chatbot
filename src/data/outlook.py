"""Outlook mailbox search via Microsoft Graph.

Uses the per-user `/users/{upn}/messages` endpoint with `$search` so we
can scope results to one mailbox cleanly. The alternative -
`/search/query` with `entityTypes=["message"]` - searches *across* the
tenant which is usually noisier than callers want.

Requires the `Mail.Read` Application permission with admin consent. With
that permission, ATLAS can read any mailbox in the tenant - the choice
of WHICH mailbox to read is the caller's responsibility (typically the
asking user's own UPN, per the system prompt rule).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.data.graph_client import GraphClient, GraphError
from src.utils.logger import get_logger

# Fields we pull back per message. Body preview is the first ~255 chars
# of the email - enough to show context without blowing token budgets.
_MESSAGE_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,webLink,bodyPreview,hasAttachments,importance,isRead"
)

# Graph caps `$top` at 25 for `$search` requests on messages.
_MAX_PAGE_SIZE = 25

# Strip Graph's match-highlight markup and ellipsis tokens, same as we
# do for SharePoint snippets.
_NOISE_TAG_RE = re.compile(r"</?c\d+>|<ddd/>")

# `$search` on the messages endpoint requires this header. Sending an
# explicit `eventual` consistency level tells Graph to use its search
# index rather than relational filtering.
_CONSISTENCY_HEADER = {"ConsistencyLevel": "eventual"}


class OutlookStore:
    """Outlook mailbox search backed by `GraphClient`."""

    def __init__(self, client: GraphClient, log_level: str = "INFO") -> None:
        self._client = client
        self._log = get_logger(__name__, log_level)

    def search_mailbox(
        self,
        user_upn: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search one user's mailbox for messages matching `query`.

        Args:
            user_upn: The mailbox to search, e.g. `faizan.riaz@galaxypharma.net`.
                Must be a UPN/email - object ID also works.
            query: Plain keyword query (also accepts KQL syntax inside
                quotes, e.g. `'"media fill"'` for exact phrase).
            limit: Max messages to return. Clamped to [1, 25] by Graph.

        Returns:
            Dict with:
                - count: number of messages returned
                - mailbox: the searched UPN (echoed back)
                - results: list of formatted message dicts
                - source: "Outlook"
                - query: echoed back
        """
        if not user_upn:
            return {
                "error": "user_upn is required to search a mailbox.",
                "source": "Outlook",
            }
        if not query or not query.strip():
            return {
                "count": 0,
                "mailbox": user_upn,
                "results": [],
                "source": "Outlook",
                "query": query,
            }

        clamped = max(1, min(_MAX_PAGE_SIZE, limit))
        # Graph's `$search` operator wants the query value double-quoted.
        params = {
            "$search": f'"{query}"',
            "$top": clamped,
            "$select": _MESSAGE_SELECT,
        }
        path = f"/users/{user_upn}/messages"
        self._log.info(
            "Outlook search: mailbox=%s query=%r limit=%d",
            user_upn, query, clamped,
        )

        try:
            response = self._client.get(
                path, params=params, extra_headers=_CONSISTENCY_HEADER,
            )
        except GraphError as exc:
            self._log.error("Outlook search failed: %s", exc)
            return {
                "error": str(exc),
                "mailbox": user_upn,
                "source": "Outlook",
            }

        messages = response.get("value", []) or []
        results = [self._format_message(m) for m in messages]

        self._log.info("Outlook search returned %d messages", len(results))
        return {
            "count": len(results),
            "mailbox": user_upn,
            "results": results,
            "source": "Outlook",
            "query": query,
        }

    @staticmethod
    def _format_message(msg: dict[str, Any]) -> dict[str, Any]:
        """Flatten one Graph message resource into our standard shape."""
        sender = ((msg.get("from") or {}).get("emailAddress") or {})
        sender_name = sender.get("name", "")
        sender_addr = sender.get("address", "")

        to_recipients = msg.get("toRecipients") or []
        to_addresses = [
            (r.get("emailAddress") or {}).get("address", "")
            for r in to_recipients
        ]

        preview = msg.get("bodyPreview", "") or ""
        cleaned_preview = _NOISE_TAG_RE.sub("", preview).strip()

        return {
            "id": msg.get("id", ""),
            "subject": msg.get("subject", "") or "(no subject)",
            "from": f"{sender_name} <{sender_addr}>".strip(),
            "to": to_addresses,
            "received": msg.get("receivedDateTime", ""),
            "preview": cleaned_preview[:500],
            "web_link": msg.get("webLink", ""),
            "has_attachments": bool(msg.get("hasAttachments")),
            "importance": msg.get("importance", "normal"),
            "is_read": bool(msg.get("isRead")),
        }
