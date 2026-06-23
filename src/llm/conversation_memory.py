"""Per-conversation memory with rolling summary + recent-turns window.

Each conversation keeps the last N user+assistant pairs verbatim plus a
rolling summary of everything before that. When the verbatim window
overflows, the oldest portion is compressed into the summary using
Claude Haiku (cheap + fast).

State lives in process memory keyed by `conversation_id`. Restarting
the container wipes all conversations - acceptable for the beta.

Concurrency: a single Lock guards the per-process dict so concurrent
users (or the bot adapter handling parallel turns) don't corrupt state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from anthropic import Anthropic

from src.llm.claude_client import MODEL_HAIKU
from src.utils.logger import get_logger

# Last N user+assistant pairs are kept verbatim (no summarization).
_VERBATIM_TURNS = 6

# Each turn = 2 messages (user + assistant) so the verbatim cap is double.
_MAX_VERBATIM_MESSAGES = _VERBATIM_TURNS * 2

# Soft limit on summary length (Haiku is instructed to stay under this).
_SUMMARY_TARGET_WORDS = 150


@dataclass
class _Conversation:
    """One conversation's running state."""

    summary: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)


class ConversationMemory:
    """In-memory conversation store with rolling summary compression."""

    def __init__(self, api_key: str, log_level: str = "INFO") -> None:
        """Initialize the memory store.

        Args:
            api_key: Anthropic API key, used only for summary compression
                calls (everything else is local dict operations).
            log_level: Logger verbosity.
        """
        self._client = Anthropic(api_key=api_key)
        self._convos: dict[str, _Conversation] = {}
        self._lock = Lock()
        self._log = get_logger(__name__, log_level)

    def build_messages(
        self,
        conversation_id: str,
        new_user_text: str,
    ) -> list[dict[str, Any]]:
        """Construct the full `messages` array to send to Claude for this turn.

        Format:
          [
            (optional) user: "[Conversation summary: ...]"
            (optional) assistant: "Got it - I have that context."
            ...recent verbatim turns...
            user: <new user message>
          ]

        Returns a fresh list; safe to mutate by the caller (the orchestrator
        appends tool-use exchanges during its loop).
        """
        with self._lock:
            convo = self._convos.get(conversation_id)
            messages: list[dict[str, Any]] = []

            if convo and convo.summary:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Conversation summary so far: {convo.summary}]"
                    ),
                })
                messages.append({
                    "role": "assistant",
                    "content": "Got it - I have that context.",
                })

            if convo:
                messages.extend(convo.messages)

            messages.append({"role": "user", "content": new_user_text})
            return messages

    def append_turn(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Record one completed user/assistant exchange.

        After appending, checks whether the verbatim window has overflowed
        and compresses the oldest messages into the rolling summary if so.
        Compression failures are logged but never raised - the conversation
        keeps working even if Haiku is unreachable.
        """
        if not assistant_text:
            return

        with self._lock:
            convo = self._convos.setdefault(conversation_id, _Conversation())
            convo.messages.append({"role": "user", "content": user_text})
            convo.messages.append({"role": "assistant", "content": assistant_text})

            if len(convo.messages) > _MAX_VERBATIM_MESSAGES:
                self._compress(convo)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compress(self, convo: _Conversation) -> None:
        """Summarize the oldest messages, keep the most recent verbatim."""
        cutoff = len(convo.messages) - _MAX_VERBATIM_MESSAGES
        to_summarize = convo.messages[:cutoff]
        recent = convo.messages[cutoff:]

        prompt = self._build_summary_prompt(convo.summary, to_summarize)

        try:
            response = self._client.messages.create(
                model=MODEL_HAIKU,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            new_summary = response.content[0].text.strip()
        except Exception as exc:  # noqa: BLE001 - never crash a chat for a summary
            self._log.warning("Summary compression failed: %s", exc)
            return

        self._log.info(
            "Compressed %d messages -> %d-char summary",
            len(to_summarize),
            len(new_summary),
        )
        convo.summary = new_summary
        convo.messages = recent

    @staticmethod
    def _build_summary_prompt(
        existing_summary: str,
        messages_to_fold_in: list[dict[str, Any]],
    ) -> str:
        """Construct the prompt sent to Haiku for summary updates."""
        lines: list[str] = []
        if existing_summary:
            lines.append("Existing conversation summary:")
            lines.append(existing_summary)
            lines.append("")

        lines.append("New exchanges to incorporate into the summary:")
        for msg in messages_to_fold_in:
            speaker = "User" if msg.get("role") == "user" else "ATLAS"
            lines.append(f"{speaker}: {_message_text(msg)}")
        lines.append("")
        lines.append(
            f"Write an updated conversation summary in under "
            f"{_SUMMARY_TARGET_WORDS} words. Capture: entities mentioned "
            f"(CAPA IDs, document numbers, customer names, dates), facts "
            f"established, and what the user is currently working on. Be "
            f"concise and factual. Output the summary text only."
        )
        return "\n".join(lines)


def _message_text(msg: dict[str, Any]) -> str:
    """Flatten a message's `content` into a single string.

    Anthropic messages can have `content` as either a plain string or a
    list of content blocks (e.g. tool_use, text). For the summary prompt
    we only care about text - the rest is operational noise.
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return " ".join(parts)
    return str(content)
