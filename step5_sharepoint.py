"""Step 5 smoke test: drive SharePoint search via Microsoft Graph directly.

Verifies, in order:
  1. Bot credentials in `.env` produce a valid OAuth2 token
  2. The `Sites.Read.All` + `Files.Read.All` permissions are actually
     granted (not just listed)
  3. The Graph `/search/query` endpoint returns parseable hits
  4. `SharePointStore` reshapes those hits into our standard envelope

If this passes, we're safe to wire the tool into the orchestrator next.
If it fails, the printed error message points at exactly which layer
(token / permissions / search / parsing) is broken.

Run from the `atlas/` directory:

    python step5_sharepoint.py
"""

from __future__ import annotations

import sys

from src.config import Config, ConfigError
from src.data.graph_client import GraphClient, GraphError
from src.data.sharepoint import SharePointStore
from src.utils.console import ensure_utf8_stdout

_TEST_QUERIES: list[str] = [
    "policy",
    "SOP",
    "cleaning",
]


def main() -> int:
    """Run the smoke test. Returns 0 on at least one successful query, 1 otherwise."""
    ensure_utf8_stdout()

    try:
        config = Config.load()
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 1

    if not (config.bot_app_id and config.bot_app_password and config.bot_tenant_id):
        print(
            "[CONFIG ERROR] BOT_APP_ID, BOT_APP_PASSWORD, and BOT_TENANT_ID "
            "are all required for SharePoint. Check your .env.",
            file=sys.stderr,
        )
        return 1

    print(f"Tenant:    {config.bot_tenant_id}")
    print(f"Client ID: {config.bot_app_id}")
    print("Initializing GraphClient + SharePointStore...")

    try:
        graph = GraphClient(
            tenant_id=config.bot_tenant_id,
            client_id=config.bot_app_id,
            client_secret=config.bot_app_password,
            log_level=config.log_level,
        )
        store = SharePointStore(graph, log_level=config.log_level)
    except (ValueError, GraphError) as exc:
        print(f"[INIT ERROR] {exc}", file=sys.stderr)
        return 1

    failures = 0
    successes = 0

    for query in _TEST_QUERIES:
        print(f"\n{'=' * 78}\nQUERY: {query!r}\n{'=' * 78}")
        try:
            result = store.search(query, limit=5)
        except GraphError as exc:
            print(f"[GRAPH ERROR] {exc}", file=sys.stderr)
            failures += 1
            continue

        print(f"Hits returned: {result['count']}")
        if result["count"] == 0:
            print("(No matching content in tenant SharePoint - may be empty.)")
        for i, hit in enumerate(result["results"], start=1):
            print(f"\n[{i}] {hit['title']}")
            print(f"    Type:     {hit.get('type', '?')}")
            print(f"    Modified: {hit.get('last_modified', '?')}")
            print(f"    URL:      {hit.get('url', '')[:120]}")
            snippet = hit.get("snippet", "").replace("\n", " ").strip()
            if snippet:
                print(f"    Snippet:  {snippet[:200]}")
        successes += 1

    print(f"\n{'=' * 78}")
    print(f"Done: {successes}/{len(_TEST_QUERIES)} queries succeeded ({failures} failed)")
    print("=" * 78)
    return 0 if successes > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
