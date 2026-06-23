"""MasterControl document store.

Loads all 645 documents under `mastercontrol/document-detail/` once
(in parallel for ~5s cold start) and caches them in memory. Subsequent
searches are instant in-memory filters.

Each document JSON has these fields:
  infocardId, infocardNumber, title, lifecycleStatus, lifecycleName,
  infocardTypeName (e.g. "Log", "SOP", "CAPA"), infocardType, vaultId,
  vaultName, revision, owner, createDate
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from src.data.datalake_client import DataLakeClient
from src.utils.logger import get_logger

_DOC_PREFIX = "mastercontrol/document-detail"
_DOWNLOAD_WORKERS = 16


class MasterControlStore:
    """In-memory document index over MasterControl exports."""

    def __init__(self, client: DataLakeClient, log_level: str = "INFO") -> None:
        self._client = client
        self._log = get_logger(__name__, log_level)
        self._cache: Optional[list[dict[str, Any]]] = None

    def warmup(self) -> None:
        """Force the document cache to load. Safe to call multiple times."""
        self._ensure_loaded()

    def search(
        self,
        query: Optional[str] = None,
        doc_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search the document cache by free-text and/or facets.

        Args:
            query: Case-insensitive substring matched against title +
                infocardNumber.
            doc_type: Exact (case-insensitive) match against `infocardTypeName`,
                e.g. "SOP", "Log", "CAPA", "Form".
            status: Exact (case-insensitive) match against `lifecycleStatus`,
                e.g. "Draft", "Effective".
            limit: Maximum number of results to return.

        Returns:
            A list of trimmed document dicts (one per match).
        """
        docs = self._ensure_loaded()
        q = query.lower() if query else None
        t = doc_type.lower() if doc_type else None
        s = status.lower() if status else None

        results: list[dict[str, Any]] = []
        for doc in docs:
            if q is not None:
                haystack = (
                    f"{doc.get('title', '')} {doc.get('infocardNumber', '')}".lower()
                )
                if q not in haystack:
                    continue
            if t is not None and str(doc.get("infocardTypeName", "")).lower() != t:
                continue
            if s is not None and str(doc.get("lifecycleStatus", "")).lower() != s:
                continue

            results.append(_trim_for_output(doc))
            if len(results) >= limit:
                break

        return results

    def stats(self) -> dict[str, Any]:
        """Return counts grouped by document type and lifecycle status."""
        docs = self._ensure_loaded()
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for doc in docs:
            t = str(doc.get("infocardTypeName", "Unknown"))
            s = str(doc.get("lifecycleStatus", "Unknown"))
            by_type[t] = by_type.get(t, 0) + 1
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "total_documents": len(docs),
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> list[dict[str, Any]]:
        """Lazy-load the document cache the first time it's needed."""
        if self._cache is not None:
            return self._cache

        blobs = self._client.list_blobs(_DOC_PREFIX)
        self._log.info("Loading %d MasterControl docs in parallel...", len(blobs))

        docs: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as pool:
            futures = {
                pool.submit(self._download_doc, blob["name"]): blob["name"]
                for blob in blobs
            }
            for future in as_completed(futures):
                doc = future.result()
                if doc is not None:
                    docs.append(doc)

        self._log.info("MasterControl cache ready: %d documents", len(docs))
        self._cache = docs
        return docs

    def _download_doc(self, name: str) -> Optional[dict[str, Any]]:
        """Download one document JSON. Logs and skips on failure."""
        try:
            doc = self._client.download_json(name)
            if isinstance(doc, dict):
                doc["_blob_name"] = name
                return doc
            self._log.warning("Unexpected non-dict document at %s", name)
            return None
        except Exception as exc:  # noqa: BLE001 - keep one bad doc from killing the batch
            self._log.warning("Failed to load %s: %s", name, exc)
            return None


def _trim_for_output(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a compact subset of fields suitable for sending back to Claude."""
    return {
        "id": doc.get("infocardId"),
        "number": doc.get("infocardNumber"),
        "title": doc.get("title"),
        "type": doc.get("infocardTypeName"),
        "status": doc.get("lifecycleStatus"),
        "lifecycle": doc.get("lifecycleName"),
        "revision": doc.get("revision"),
        "owner": doc.get("owner"),
        "createDate": doc.get("createDate"),
    }
