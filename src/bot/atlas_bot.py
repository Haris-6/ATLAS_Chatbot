"""ATLAS chat bot.

Receives messages from the Bot Framework (Emulator locally, real Teams
in production) and routes each one through the tool-use orchestrator.

The bot itself is channel-agnostic - the same class serves the local
Emulator and Microsoft Teams. Channel-specific concerns (cards, auth)
live in `bot_server.py`.
"""

from __future__ import annotations

import asyncio
from typing import List

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount

from src.llm.claude_client import ClaudeError
from src.llm.orchestrator import ToolOrchestrator
from src.utils.logger import get_logger

_WELCOME_MESSAGE = (
    "Hi - I'm ATLAS, Galaxy Pharmaceuticals' AI workspace assistant. "
    "Ask me about SOPs, CAPAs, media fills, deviations, or other operational data. "
    "Every answer comes with a source citation."
)

_GENERIC_ERROR_MESSAGE = (
    "Sorry - I couldn't complete that request. Please try again or rephrase."
)

_EMPTY_MESSAGE_REPLY = "I didn't catch that - could you rephrase your question?"


class AtlasBot(ActivityHandler):
    """Bot Framework handler that delegates messages to the orchestrator.

    Args:
        orchestrator: A ready-to-use `ToolOrchestrator` instance.
        log_level: Logger verbosity for the bot's own messages.
    """

    def __init__(
        self,
        orchestrator: ToolOrchestrator,
        log_level: str = "INFO",
    ) -> None:
        self._orchestrator = orchestrator
        self._log = get_logger(__name__, log_level)

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        """Process a single chat message and reply with Claude's answer."""
        user_text = (turn_context.activity.text or "").strip()
        if not user_text:
            await turn_context.send_activity(_EMPTY_MESSAGE_REPLY)
            return

        self._log.info("User said: %s", user_text)

        # Show a typing indicator so the user knows we're working on it.
        # The orchestrator can take several seconds for tool calls.
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        # Bot Framework gives every conversation a stable id. Passing it
        # to the orchestrator lets memory thread prior turns into context
        # so "the last one" / "show me more" follow-ups work naturally.
        conversation_id = (
            turn_context.activity.conversation.id
            if turn_context.activity.conversation
            else None
        )

        # Teams stamps every activity with the sender's AAD object ID and
        # display name. Forwarding them lets the orchestrator resolve "my
        # OneDrive" / "my inbox" queries against the right identity.
        from_property = turn_context.activity.from_property
        requesting_user = None
        if from_property is not None:
            requesting_user = {
                "aad_id": from_property.aad_object_id or "",
                "name": from_property.name or "",
            }

        # The orchestrator is synchronous (uses the sync Anthropic client),
        # so we run it in a worker thread to avoid blocking the event loop
        # and keep the bot responsive to other concurrent users.
        try:
            result = await asyncio.to_thread(
                self._orchestrator.run,
                prompt=user_text,
                conversation_id=conversation_id,
                requesting_user=requesting_user,
            )
        except ClaudeError as exc:
            self._log.error("Orchestrator failed: %s", exc)
            await turn_context.send_activity(_GENERIC_ERROR_MESSAGE)
            return

        self._log.info(
            "Replied: %d in, %d out tokens, tools=%s, ~$%.6f",
            result.total_input_tokens,
            result.total_output_tokens,
            result.tool_calls or "(none)",
            result.estimated_cost_usd,
        )
        await turn_context.send_activity(result.text)

    async def on_members_added_activity(
        self,
        members_added: List[ChannelAccount],
        turn_context: TurnContext,
    ) -> None:
        """Greet new members joining the conversation.

        In the Bot Framework Emulator this fires on connection. In Teams
        it fires when ATLAS is added to a chat or channel.
        """
        bot_id = turn_context.activity.recipient.id
        for member in members_added:
            if member.id != bot_id:
                await turn_context.send_activity(_WELCOME_MESSAGE)
