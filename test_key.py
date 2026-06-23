"""Smoke test for ATLAS: prove that we can talk to Claude end-to-end.

Run from the `atlas/` directory:

    python hello.py

A successful run prints Claude's introduction plus token usage and cost.
A failure prints a clear, actionable error and exits with code 1.
"""

from __future__ import annotations

import sys

from src.config import Config, ConfigError
from src.llm.claude_client import ClaudeClient, ClaudeError
from src.utils.console import ensure_utf8_stdout

_INTRO_PROMPT = (
    "In one sentence, introduce yourself as ATLAS, "
    "Galaxy Pharmaceuticals' AI workspace assistant."
)


def main() -> int:
    """Run the smoke test. Returns a shell exit code (0=ok, 1=failure)."""
    ensure_utf8_stdout()

    try:
        config = Config.load()
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 1

    client = ClaudeClient(
        api_key=config.anthropic_api_key,
        log_level=config.log_level,
    )

    try:
        response = client.complete(prompt=_INTRO_PROMPT, max_tokens=200)
    except ClaudeError as exc:
        print(f"[CLAUDE ERROR] {exc}", file=sys.stderr)
        return 1

    _print_result(response)
    return 0


def _print_result(response) -> None:
    """Pretty-print Claude's response and usage info to stdout."""
    bar = "=" * 60
    print(bar)
    print("ATLAS Hello World - SUCCESS")
    print(bar)
    print(response.text)
    print(bar)
    print(f"Model:     {response.model}")
    print(f"Tokens:    {response.input_tokens} in, {response.output_tokens} out")
    print(f"Est. cost: ${response.estimated_cost_usd:.6f}")
    print(bar)


if __name__ == "__main__":
    sys.exit(main())
