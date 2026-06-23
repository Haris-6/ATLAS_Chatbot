"""Step 4 smoke test: drive all four real data-lake tools through the orchestrator.

Runs one representative query per tool to confirm:
  - data lake credentials work
  - each store loads correctly
  - Claude picks the right tool
  - citations come back with source info

Run from the `atlas/` directory:

    python step4_real_data.py
"""

from __future__ import annotations

import sys

from src.config import Config, ConfigError
from src.llm.claude_client import ClaudeError
from src.llm.orchestrator import OrchestratorResult, ToolOrchestrator
from src.llm.tools import build_tools
from src.utils.console import ensure_utf8_stdout

_TEST_QUERIES: list[str] = [
    # MasterControl
    "Find all documents whose title contains 'Cleaning'. Limit to 5.",
    # NetSuite
    "Show me Galaxy's current customers from NetSuite.",
    # Oceaview
    "What were our humidity sensor readings around May 22-23, 2026? Give me min/max/mean.",
    # DataNinja
    "Find any DataNinja parts that mention 'vial' or 'amber'.",
]


def main() -> int:
    """Run all four data-tool queries through the orchestrator."""
    ensure_utf8_stdout()

    try:
        config = Config.load()
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 1

    if not config.azure_storage_connection_string:
        print(
            "[CONFIG ERROR] AZURE_STORAGE_CONNECTION_STRING is missing from .env",
            file=sys.stderr,
        )
        return 1

    tools = build_tools(config)
    print(f"Registered {len(tools)} tools: {[t.name for t in tools]}\n")

    orchestrator = ToolOrchestrator(
        api_key=config.anthropic_api_key,
        tools=tools,
        log_level=config.log_level,
    )

    failures = 0
    for index, query in enumerate(_TEST_QUERIES, start=1):
        print(f"\n{'=' * 78}")
        print(f"QUERY {index}: {query}")
        print("=" * 78)

        try:
            result = orchestrator.run(prompt=query)
        except ClaudeError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            failures += 1
            continue

        _print_result(result)

    print(f"\n{'=' * 78}")
    print(f"Done: {len(_TEST_QUERIES) - failures}/{len(_TEST_QUERIES)} queries succeeded")
    print("=" * 78)
    return 0 if failures == 0 else 1


def _print_result(result: OrchestratorResult) -> None:
    """Pretty-print one query's outcome."""
    tools_str = ", ".join(result.tool_calls) if result.tool_calls else "(none)"
    print(f"Tools called: {tools_str}")
    print(f"Iterations:   {result.iterations}")
    print(f"Tokens:       {result.total_input_tokens} in, {result.total_output_tokens} out")
    print(f"Est. cost:    ${result.estimated_cost_usd:.6f}")
    print(f"\nANSWER:\n{result.text}")


if __name__ == "__main__":
    sys.exit(main())
