"""SharePoint search via Microsoft Graph Search API.

No indexing or provisioning required - Microsoft already indexes every
SharePoint site / document library in the tenant. We just hit
`/search/query` and surface the top hits with title, URL, snippet, and
last-modified date.

Returns a clean dict ready to hand back to Claude as a tool result. The
caller (the tool handler in `src/llm/tools.py`) is responsible for any
shape Claude expects beyond what we return here.
"""

from __future__ import annotations

import re
from typing import Any

from src.data.graph_client import GraphClient
from src.utils.logger import get_logger

# Entity types we ask Graph to search:
#   - driveItem: files in SharePoint document libraries + OneDrive
#   - listItem: rows in SharePoint lists (e.g. trackers, registers)
_DEFAULT_ENTITY_TYPES = ["driveItem", "listItem"]

# Microsoft Graph caps search size per entity type at 25 per request.
_MAX_PAGE_SIZE = 25

# Graph snippets contain two kinds of inline markers we strip for clean
# chat output:
#   - `<c0>...</c0>` (any digit) wraps matched query terms
#   - `<ddd/>` is a "did you mean / truncated" ellipsis marker
_NOISE_TAG_RE = re.compile(r"</?c\d+>|<ddd/>")

# Cap each snippet's length so we don't blow Claude's context on a few
# big files. 500 chars is enough to show the matched passage in context.
_SNIPPET_MAX_CHARS = 500


class SharePointStore:
    """SharePoint search wrapper backed by `GraphClient`."""

    def __init__(self, client: GraphClient, log_level: str = "INFO") -> None:
        self._client = client
        self._log = get_logger(__name__, log_level)

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search SharePoint for files/items matching `query`.

        Args:
            query: Keyword query. Accepts KQL syntax too (e.g. quoted
                phrases, field filters like `filename:*.pdf`).
            limit: Max results to return. Clamped to [1, 25] because
                Graph's `/search/query` caps page size at 25 per entity.

        Returns:
            Dict with:
                - count: number of results returned
                - results: list of {title, url, last_modified, snippet, type}
                - source: literal "SharePoint" for citation
                - query: echoed back for debugging visibility
        """
        if not query or not query.strip():
            return {
                "count": 0,
                "results": [],
                "source": "SharePoint",
                "query": query,
            }

        clamped = max(1, min(_MAX_PAGE_SIZE, limit))
        self._log.info("SharePoint search: %r (limit=%d)", query, clamped)

        response = self._client.search(
            query_string=query,
            entity_types=_DEFAULT_ENTITY_TYPES,
            size=clamped,
        )

        results: list[dict[str, Any]] = []
        for outer in response.get("value", []):
            for container in outer.get("hitsContainers", []):
                for hit in container.get("hits", []):
                    results.append(self._format_hit(hit))
                    if len(results) >= clamped:
                        break
                if len(results) >= clamped:
                    break

        self._log.info("SharePoint search returned %d hits", len(results))
        return {
            "count": len(results),
            "results": results[:clamped],
            "source": "SharePoint",
            "query": query,
        }

    @staticmethod
    def _format_hit(hit: dict[str, Any]) -> dict[str, Any]:
        """Flatten a Graph search hit into the shape we hand back to Claude."""
        resource = hit.get("resource", {}) or {}
        raw_summary = hit.get("summary", "") or ""
        cleaned_summary = _NOISE_TAG_RE.sub("", raw_summary).strip()

        # `name` is set on driveItem; listItem typically exposes `title`.
        title = (
            resource.get("name")
            or resource.get("title")
            or "(no title)"
        )
        url = resource.get("webUrl", "")
        last_modified = resource.get("lastModifiedDateTime", "")

        # `@odata.type` looks like "#microsoft.graph.driveItem"; strip the
        # prefix for a friendlier label that Claude can cite directly.
        odata_type = resource.get("@odata.type", "")
        resource_type = odata_type.replace("#microsoft.graph.", "") if odata_type else ""

        return {
            "title": title,
            "url": url,
            "last_modified": last_modified,
            "snippet": cleaned_summary[:_SNIPPET_MAX_CHARS],
            "type": resource_type,
        }
