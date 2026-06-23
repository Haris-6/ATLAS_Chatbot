"""Anthropic Claude client wrapper for ATLAS.

Wraps the official Anthropic Python SDK with:
  - centralized model IDs (one place to swap versions)
  - structured logging
  - token / cost tracking
  - clean error mapping (network / rate-limit / API errors -> ClaudeError)

Later, this is the single file we change to switch from Anthropic API direct
to AWS Bedrock - both backends use the same SDK contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIError,
    RateLimitError,
)

from src.utils.logger import get_logger

# Model IDs - one place to update when versions change.
MODEL_OPUS = "claude-opus-4-7"             # synthesis + complex reasoning
MODEL_SONNET = "claude-sonnet-4-6"          # default for routine queries
MODEL_HAIKU = "claude-haiku-4-5-20251001"   # cheap + fast (EOD parsing, extraction)

# Approximate USD pricing per 1M tokens (Sonnet 4.6). Used for dev visibility,
# not billing. Real billing comes from the Anthropic console.
_PRICE_INPUT_PER_MTOK = 3.0
_PRICE_OUTPUT_PER_MTOK = 15.0


class ClaudeError(Exception):
    """Raised when a Claude API call fails in a way callers should handle."""


@dataclass(frozen=True)
class ClaudeResponse:
    """Structured result from a single Claude completion."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def estimated_cost_usd(self) -> float:
        """Rough cost estimate for dev visibility only."""
        return (
            self.input_tokens * _PRICE_INPUT_PER_MTOK
            + self.output_tokens * _PRICE_OUTPUT_PER_MTOK
        ) / 1_000_000


class ClaudeClient:
    """Thin, well-behaved wrapper around the Anthropic SDK."""

    def __init__(self, api_key: str, log_level: str = "INFO") -> None:
        """Initialize the Claude client.

        Args:
            api_key: Anthropic API key (must start with `sk-ant-`).
            log_level: Logger verbosity for this client's messages.
        """
        self._client = Anthropic(api_key=api_key)
        self._log = get_logger(__name__, log_level)

    def complete(
        self,
        prompt: str,
        model: str = MODEL_SONNET,
        max_tokens: int = 1024,
        system: Optional[str] = None,
    ) -> ClaudeResponse:
        """Send a single-turn user prompt to Claude.

        Args:
            prompt: The user message text.
            model: Claude model ID. Use `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU`.
            max_tokens: Maximum tokens Claude may produce.
            system: Optional system prompt to steer Claude's behavior.

        Returns:
            A `ClaudeResponse` with the answer text and token usage.

        Raises:
            ClaudeError: If the request cannot be completed (network, rate-limit,
                or API error). The original exception is chained for debugging.
        """
        self._log.debug(
            "Calling Claude: model=%s max_tokens=%d system=%s",
            model, max_tokens, bool(system),
        )

        request_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            request_kwargs["system"] = system

        try:
            message = self._client.messages.create(**request_kwargs)
        except RateLimitError as exc:
            self._log.error("Rate limited by Anthropic: %s", exc)
            raise ClaudeError("Anthropic rate limit hit - back off and retry.") from exc
        except APIConnectionError as exc:
            self._log.error("Network error reaching Anthropic: %s", exc)
            raise ClaudeError("Could not reach Anthropic API - check your network.") from exc
        except APIError as exc:
            self._log.error("Anthropic API error: %s", exc)
            raise ClaudeError(f"Anthropic API error: {exc}") from exc

        response = ClaudeResponse(
            text=message.content[0].text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=model,
        )
        self._log.info(
            "Claude response: %d in, %d out, ~$%.6f",
            response.input_tokens,
            response.output_tokens,
            response.estimated_cost_usd,
        )
        return response
