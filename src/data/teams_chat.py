"""Microsoft Teams chat-message search via Graph.

NOTE on approach: Graph's `/search/query` endpoint does NOT support the
`chatMessage` entity type with Application permissions - Microsoft restricts
search to SharePoint entities (site/list/listItem/drive/driveItem) for
app-only auth. We confirmed this returns 403 with a clear error message.

Instead we use `/users/{upn}/chats/getAllMessages`, which Microsoft does
support with `Chat.Read.All` Application permission. That endpoint returns
every message from every chat the user is a member of, paginated. We then
filter in-code by keyword - simple keyword search rather than Microsoft's
fancier search index, but it works and respects the per-user boundary.

Caveat: `getAllMessages` is a "metered API" (billed by Microsoft above a
generous free tier). For our beta scale this is well within the free
allowance. For production roll-out, switch to delegated auth + per-user
search across that user's own chats.
"""

from __future__ import annotations

import re
from typing import Any

from src.data.graph_client import GraphClient, GraphError
from src.utils.logger import get_logger

# Fields we pull per message. Keep small to limit bytes paged over.
_MESSAGE_SELECT = (
    "id,body,from,createdDateTime,chatId,webUrl,importance,messageType"
)

# How many messages to ask for per page. Graph caps `$top` at 50 here.
_PAGE_SIZE = 50

# How many pages to walk at most before giving up. 4 * 50 = 200 messages
# scanned, which is plenty for "recent chat history" queries and keeps
# latency bounded.
_MAX_PAGES = 4

# Strip HTML tags from message body for a plain-text preview. Teams chat
# messages are often HTML-wrapped even for simple text.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class TeamsChatStore:
    """Teams chat-message search via `getAllMessages` + in-code filtering."""

    def __init__(
        self,
        client: GraphClient,
        bot_app_id: str = "",
        log_level: str = "INFO",
    ) -> None:
        """Build the store.

        Args:
            client: Microsoft Graph client.
            bot_app_id: Optional. If set, messages authored by this app
                (i.e. ATLAS's own replies in Teams) are excluded from
                search results. Stops the search from returning the
                bot's own past responses, which dominate when the user
                has been chatting with ATLAS heavily.
            log_level: Logger verbosity.
        """
        self._client = client
        self._bot_app_id = (bot_app_id or "").strip()
        self._log = get_logger(__name__, log_level)

    def search_user_chats(
        self,
        user_upn: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search recent messages across all of `user_upn`'s chats.

        Args:
            user_upn: UPN/email of the asking user (security boundary).
            query: Keyword to match (case-insensitive substring against
                the plain-text body).
            limit: Max formatted messages to return.

        Returns:
            Dict with count, user_upn, query, results, source.
        """
        if not user_upn:
            return {
                "error": "user_upn is required for Teams chat search.",
                "source": "Teams",
            }
        if not query or not query.strip():
            return {
                "count": 0,
                "user_upn": user_upn,
                "query": query,
                "results": [],
                "source": "Teams",
            }

        needle = query.strip().lower()
        clamped_limit = max(1, min(_PAGE_SIZE, limit))

        results: list[dict[str, Any]] = []
        scanned = 0
        path: str | None = f"/users/{user_upn}/chats/getAllMessages"
        params: dict[str, Any] | None = {
            "$top": _PAGE_SIZE,
            "$select": _MESSAGE_SELECT,
        }

        try:
            for _page in range(_MAX_PAGES):
                if path is None:
                    break
                # First call uses params; pagination links are absolute URLs.
                response = self._client.get(path, params=params)
                for msg in response.get("value", []) or []:
                    scanned += 1
                    formatted = self._format_message(msg)
                    if not formatted:
                        continue
                    if needle in formatted["plain_body"].lower():
                        results.append(formatted)
                        if len(results) >= clamped_limit:
                            break
                if len(results) >= clamped_limit:
                    break
                next_url = response.get("@odata.nextLink")
                if not next_url:
                    break
                path = next_url
                params = None  # nextLink is fully-formed
        except GraphError as exc:
            self._log.error("Teams getAllMessages failed: %s", exc)
            return {"error": str(exc), "source": "Teams"}

        self._log.info(
            "Teams search: upn=%s query=%r scanned=%d matches=%d",
            user_upn, query, scanned, len(results),
        )
        return {
            "count": len(results),
            "user_upn": user_upn,
            "query": query,
            "scanned_messages": scanned,
            "results": results,
            "source": "Teams",
            "note": (
                f"Scanned the {scanned} most-recent chat messages. "
                "If the message you're looking for is older, narrow the "
                "query or ask for a date range."
            ) if scanned >= _PAGE_SIZE * _MAX_PAGES else None,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Flatten a chatMessage into a clean dict, or return None to skip.

        Returns None for system/control messages (e.g. "user added"), which
        carry no useful text content.
        """
        message_type = msg.get("messageType", "message")
        if message_type != "message":
            return None

        from_field = msg.get("from") or {}

        # Skip messages authored by ATLAS itself. Bot messages put the
        # bot's identity under `from.application.id` rather than `from.user`.
        # Without this filter the bot's past replies dominate results.
        if self._bot_app_id:
            from_application = from_field.get("application") or {}
            if from_application.get("id") == self._bot_app_id:
                return None

        body = msg.get("body") or {}
        raw_content = body.get("content", "") or ""
        if not raw_content.strip():
            return None

        plain = self._html_to_plain(raw_content)
        if not plain:
            return None

        from_user = from_field.get("user") or {}
        sender_name = from_user.get("displayName", "") or ""
        sender_id = from_user.get("id", "") or ""

        return {
            "chat_id": msg.get("chatId", ""),
            "sender": sender_name or sender_id or "(unknown)",
            "created_at": msg.get("createdDateTime", ""),
            "plain_body": plain,
            "preview": plain[:500],
            "web_url": msg.get("webUrl", ""),
            "importance": msg.get("importance", "normal"),
        }

    @staticmethod
    def _html_to_plain(html: str) -> str:
        """Crude HTML-to-text: strip tags, collapse whitespace."""
        no_tags = _HTML_TAG_RE.sub(" ", html)
        no_entities = no_tags.replace("&nbsp;", " ").replace("&amp;", "&")
        collapsed = _WHITESPACE_RE.sub(" ", no_entities).strip()
        return collapsed
