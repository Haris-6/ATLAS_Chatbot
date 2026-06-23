"""Resolve Bot Framework AAD object IDs to Microsoft 365 user info.

Teams gives us a stable `aadObjectId` on every activity but not always
the user's email. To make "my OneDrive" / "my inbox" queries possible
we look up `/users/{aad_id}` via Graph and cache the result.

The cache is in-process and bounded (~500 entries) so a long-running
container can't grow unbounded.

A failed lookup returns `None` so callers can degrade gracefully (they
just fall back to behaviour where ATLAS doesn't know who's asking).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Optional

from src.data.graph_client import GraphClient, GraphError
from src.utils.logger import get_logger

_MAX_CACHE_ENTRIES = 500

# Graph fields we care about per user. Keep small to limit token bytes
# the cache holds in memory.
_USER_SELECT = "id,displayName,mail,userPrincipalName"


@dataclass(frozen=True)
class UserInfo:
    """Resolved Microsoft 365 user identity."""

    aad_id: str
    display_name: str
    email: str
    upn: str  # user principal name, e.g. faizan.riaz@galaxypharma.net

    @property
    def onedrive_path_segment(self) -> str:
        """Return the OneDrive personal-site path segment for this user.

        SharePoint encodes user identity in URLs by replacing both `@`
        and `.` with `_`. e.g. `faizan.riaz@galaxypharma.net` maps to
        `faizan_riaz_galaxypharma_net` under `/personal/`.
        """
        anchor = self.upn or self.email
        return anchor.replace("@", "_").replace(".", "_")


class UserResolver:
    """Resolve AAD object IDs to user info via Graph, with LRU caching."""

    def __init__(self, graph: GraphClient, log_level: str = "INFO") -> None:
        self._graph = graph
        self._cache: OrderedDict[str, UserInfo] = OrderedDict()
        self._lock = Lock()
        self._log = get_logger(__name__, log_level)

    def resolve(self, aad_id: Optional[str]) -> Optional[UserInfo]:
        """Look up a user by AAD object ID. Returns None on miss/error."""
        if not aad_id:
            return None

        with self._lock:
            cached = self._cache.get(aad_id)
            if cached is not None:
                self._cache.move_to_end(aad_id)
                return cached

        try:
            data = self._graph.get(
                f"/users/{aad_id}",
                params={"$select": _USER_SELECT},
            )
        except GraphError as exc:
            self._log.warning("User lookup failed for AAD %s: %s", aad_id, exc)
            return None

        info = UserInfo(
            aad_id=data.get("id") or aad_id,
            display_name=data.get("displayName") or "",
            # `mail` is sometimes null for cloud-only users; fall back to UPN.
            email=(data.get("mail") or data.get("userPrincipalName") or ""),
            upn=data.get("userPrincipalName") or "",
        )

        with self._lock:
            self._cache[aad_id] = info
            self._cache.move_to_end(aad_id)
            while len(self._cache) > _MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)

        self._log.info(
            "Resolved AAD %s -> %s <%s>",
            aad_id, info.display_name, info.email,
        )
        return info
