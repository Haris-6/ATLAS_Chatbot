"""Tool-use orchestrator: the loop that lets Claude call tools and answer.

Flow per query:
  1. Send the user's prompt + tool schemas to Claude.
  2. If Claude responds with `tool_use`, run each tool, return results,
    and loop back to Claude.
  3. If Claude responds with `end_turn`, return the final text answer.
  4. If we exceed `max_iterations`, raise - protects against infinite loops.

Tool errors are caught and returned to Claude as structured error results
(per Anthropic's recommended pattern), letting Claude recover or apologize
gracefully instead of crashing the conversation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIError,
    RateLimitError,
)

from src.llm.claude_client import (
    MODEL_SONNET,
    ClaudeError,
    _PRICE_INPUT_PER_MTOK,
    _PRICE_OUTPUT_PER_MTOK,
)
from src.data.user_resolver import UserInfo, UserResolver
from src.llm.conversation_memory import ConversationMemory
from src.llm.tools import Tool
from src.utils.logger import get_logger

# Default system prompt - tells Claude which tools to reach for and how.
DEFAULT_SYSTEM_PROMPT = (
    "You are ATLAS, the AI workspace assistant for Galaxy Pharmaceuticals.\n"
    "You help employees retrieve information from Galaxy's internal systems.\n"
    "\n"
    "You have seven data sources available via tools:\n"
    "  1. MasterControl quality system (search_mastercontrol_documents) - "
    "authoritative metadata for SOPs, CAPAs, Logs, Forms, and deviations: "
    "document number, revision, lifecycle status (Draft/Effective), owner. "
    "Use for 'what is the current revision', 'which CAPAs are open', or any "
    "question about formal quality-record status.\n"
    "  2. SharePoint live (search_sharepoint) - actual files with content "
    "and clickable URLs across ALL SharePoint sites + OneDrive. Use for "
    "'find the file about X', 'show me the cleaning SOP', or any request "
    "that needs the document itself rather than just status metadata.\n"
    "  3. Outlook mailbox search (search_outlook) - emails by keyword, "
    "sender, or date in a specific user's mailbox. For 'my emails' or "
    "'emails about X' queries, pass the CURRENT user's email (from your "
    "system context) as the `mailbox` arg. For 'what did Rosa send' "
    "queries, use Rosa's UPN.\n"
    "  4. Teams chat messages (search_teams_messages) - messages from "
    "Teams 1:1 / group chats the asking user is in. ALWAYS pass the "
    "current user's email as `user_upn` - results are filtered to that "
    "user's chats for privacy. Use for 'what did Rosa say in our chat', "
    "'find my Teams messages about X', or 'who pinged me about Y'.\n"
    "  5. NetSuite ERP (get_netsuite_data) - customers, sales orders, purchase orders.\n"
    "  6. Oceaview environmental sensors (query_environmental_sensors) - humidity, "
    "pressure, and temperature readings for facility/cleanroom monitoring.\n"
    "  7. DataNinja (query_dataninja_parts) - parts, inventory, and manufacturing data.\n"
    "  8. Meeting agenda template (prepare_meeting_agenda_template) - returns "
    "the section structure for L10 / standup / retro / 1on1 meetings. Call "
    "this FIRST whenever the user asks for a meeting agenda (especially "
    "with the '/agenda' shortcut), then use the data tools to fill each "
    "section with real context.\n"
    "\n"
    "Rules:\n"
    "- Pick the right tool for the question. SharePoint and MasterControl "
    "overlap on quality docs - prefer MasterControl for status/revision, "
    "SharePoint for content/links. If unsure, call both.\n"
    "- Always cite the source and include the URL when SharePoint returns "
    "one (e.g. '[SOP-085](https://...)'). For MasterControl, cite the "
    "document number (e.g. 'from MasterControl document LOG-037').\n"
    "- Answer the question and stop. Do NOT append 'Next Steps', "
    "'Suggested Actions', 'Recommendations', or any unsolicited "
    "to-do/action-item list. Only add such a section if the user "
    "explicitly asks for next steps or action items.\n"
    "- If a tool returns no matching results, say so honestly. Do not fabricate data.\n"
    "- Use markdown tables for lists; use one to three sentences for single facts.\n"
    "- If you're unsure which system has the answer, try the most likely tool first or "
    "ask the user to clarify."
)


@dataclass
class OrchestratorResult:
    """Aggregated result from a full orchestrator run (across iterations)."""

    text: str
    iterations: int
    tool_calls: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        """Sum-of-iterations rough cost in USD."""
        return (
            self.total_input_tokens * _PRICE_INPUT_PER_MTOK
            + self.total_output_tokens * _PRICE_OUTPUT_PER_MTOK
        ) / 1_000_000


class ToolOrchestrator:
    """Drives the tool-use loop between Claude and registered tools."""

    def __init__(
        self,
        api_key: str,
        tools: list[Tool],
        log_level: str = "INFO",
        max_iterations: int = 5,
        memory: Optional[ConversationMemory] = None,
        user_resolver: Optional[UserResolver] = None,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            api_key: Anthropic API key.
            tools: Tools Claude is allowed to call.
            log_level: Logger verbosity.
            max_iterations: Safety cap on the agent loop. Five is plenty
                for any single-question flow we expect; raise it if Claude
                legitimately needs more steps for chained queries.
            memory: Optional conversation memory. When provided AND a
                `conversation_id` is passed to `run`, prior turns are
                included in the messages array so Claude has context.
        """
        self._client = Anthropic(api_key=api_key)
        self._tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        self._tool_schemas = [t.to_anthropic_schema() for t in tools]
        self._max_iterations = max_iterations
        self._memory = memory
        self._user_resolver = user_resolver
        self._log = get_logger(__name__, log_level)

    def run(
        self,
        prompt: str,
        model: str = MODEL_SONNET,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        conversation_id: Optional[str] = None,
        requesting_user: Optional[dict[str, Any]] = None,
    ) -> OrchestratorResult:
        """Run the tool-use loop until Claude returns a final answer.

        Args:
            prompt: The user's question.
            model: Claude model ID.
            max_tokens: Per-iteration token cap.
            system: Optional override of the default system prompt.
            conversation_id: When set AND memory is wired, prior turns
                from this conversation are included in the messages
                array so Claude has context. After the run completes,
                the new exchange is appended to memory.

        Returns:
            An `OrchestratorResult` with the final text and usage totals.

        Raises:
            ClaudeError: On API failure or if max_iterations is exceeded.
        """
        base_system = system if system is not None else DEFAULT_SYSTEM_PROMPT
        # Split the system prompt into a STABLE part (cacheable) and an
        # optional DYNAMIC user-identity suffix. Anthropic prompt caching
        # gives ~90% input-token discount on cache hits; the cache lives
        # 5 minutes, so back-to-back queries reuse the cached prefix.
        identity_suffix = self._build_identity_suffix(requesting_user)
        system_blocks = [
            {
                "type": "text",
                "text": base_system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if identity_suffix:
            # User identity is NOT cached - varies per requester.
            system_blocks.append({"type": "text", "text": identity_suffix})

        # Mark the last tool schema with cache_control so the entire
        # tool definition list participates in the same cache breakpoint.
        # We rebuild the list lazily per call to avoid mutating the shared
        # `self._tool_schemas` (would break other concurrent runs).
        if self._tool_schemas:
            cached_tools = [dict(t) for t in self._tool_schemas]
            cached_tools[-1] = {
                **cached_tools[-1],
                "cache_control": {"type": "ephemeral"},
            }
        else:
            cached_tools = self._tool_schemas

        if conversation_id and self._memory is not None:
            messages: list[dict[str, Any]] = self._memory.build_messages(
                conversation_id, prompt
            )
        else:
            messages = [{"role": "user", "content": prompt}]

        tool_calls_made: list[str] = []
        total_in = 0
        total_out = 0
        total_cache_read = 0
        total_cache_write = 0

        for iteration in range(1, self._max_iterations + 1):
            self._log.debug("Iteration %d - calling Claude", iteration)

            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    tools=cached_tools,
                    messages=messages,
                )
            except RateLimitError as exc:
                raise ClaudeError("Anthropic rate limit hit - back off and retry.") from exc
            except APIConnectionError as exc:
                raise ClaudeError("Could not reach Anthropic API - check your network.") from exc
            except APIError as exc:
                raise ClaudeError(f"Anthropic API error: {exc}") from exc

            total_in += response.usage.input_tokens
            total_out += response.usage.output_tokens
            # `cache_*_input_tokens` count tokens that came from / went into
            # the cache. They're billed at 0.1x / 1.25x of normal input
            # respectively, so logging them helps verify the cache hits.
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            total_cache_read += cache_read
            total_cache_write += cache_write
            if cache_read or cache_write:
                self._log.info(
                    "Iter %d cache: read=%d write=%d (input=%d, output=%d)",
                    iteration, cache_read, cache_write,
                    response.usage.input_tokens, response.usage.output_tokens,
                )

            if response.stop_reason == "tool_use":
                # Append Claude's request (including any text + tool_use blocks)
                # then run each tool and reply with tool_results.
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_calls_made.append(block.name)
                        tool_results.append(self._execute_tool_block(block))

                messages.append({"role": "user", "content": tool_results})
                continue

            # stop_reason is "end_turn" (or "max_tokens"/"stop_sequence"):
            # extract the assistant's text and return.
            text = self._extract_text(response.content)
            self._log.info(
                "Run complete: %d iters, %d tool calls, %d/%d tokens "
                "(cache: %d read, %d write)",
                iteration, len(tool_calls_made), total_in, total_out,
                total_cache_read, total_cache_write,
            )

            # Record the exchange in memory so the next turn has context.
            # Stores only the original prompt + final answer (not the
            # intermediate tool_use/tool_result noise).
            if conversation_id and self._memory is not None:
                self._memory.append_turn(conversation_id, prompt, text)

            return OrchestratorResult(
                text=text,
                iterations=iteration,
                tool_calls=tool_calls_made,
                total_input_tokens=total_in,
                total_output_tokens=total_out,
            )

        raise ClaudeError(
            f"Orchestrator exceeded max_iterations={self._max_iterations}. "
            f"Tools called so far: {tool_calls_made}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _execute_tool_block(self, block: Any) -> dict[str, Any]:
        """Execute a single tool_use block and format the tool_result reply.

        Errors are caught and reported back to Claude with `is_error=True`
        so Claude can recover gracefully instead of crashing the loop.
        """
        name = block.name
        args = block.input
        self._log.info("Tool call: %s(%s)", name, args)

        tool = self._tools_by_name.get(name)
        if tool is None:
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Unknown tool: {name}",
                "is_error": True,
            }

        try:
            result = tool.handler(args)
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            }
        except Exception as exc:  # noqa: BLE001 - we want broad catch for tool isolation
            self._log.error("Tool %s raised: %s", name, exc)
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Tool {name} failed: {exc}",
                "is_error": True,
            }

    def _build_identity_suffix(
        self,
        requesting_user: Optional[dict[str, Any]],
    ) -> str:
        """Return ONLY the dynamic user-identity suffix (without base prompt).

        Returns an empty string when we can't pin down an email for the
        asker. Splitting this out from the stable base prompt lets us
        cache the base while keeping the per-user suffix uncached.

        Accepted dict keys (any subset; we use what we get):
          - aad_id: AAD object ID (we'll look up email + name via Graph)
          - email: pre-resolved email / UPN
          - name:  pre-resolved display name
        """
        if not requesting_user:
            return ""

        name = (requesting_user.get("name") or "").strip()
        email = (requesting_user.get("email") or "").strip()
        aad_id = (requesting_user.get("aad_id") or "").strip()
        info: Optional[UserInfo] = None

        if (not email or not name) and aad_id and self._user_resolver is not None:
            info = self._user_resolver.resolve(aad_id)
            if info:
                name = name or info.display_name
                email = email or info.email

        if not email:
            return ""

        onedrive_segment = (
            info.onedrive_path_segment
            if info
            else email.replace("@", "_").replace(".", "_")
        )
        display_label = f"{name} <{email}>" if name else email

        return (
            "The user currently chatting with you is: "
            f"{display_label}.\n"
            "When they use first-person words ('I', 'me', 'my', 'mine'), "
            "resolve them to this user.\n"
            "Their personal OneDrive lives at SharePoint path "
            f"`personal/{onedrive_segment}` - when they ask about 'my "
            "OneDrive' or 'my files', restrict `search_sharepoint` to "
            f"that path by adding `path:\"personal/{onedrive_segment}\"` "
            "to the query."
        )

    def _with_user_context(
        self,
        base_prompt: str,
        requesting_user: Optional[dict[str, Any]],
    ) -> str:
        """Compatibility shim: base prompt concatenated with identity suffix.

        Some callers (and tests) may still want a single concatenated
        string. New code uses `_build_identity_suffix` directly so the
        base + suffix can ship as separate cache-control blocks.
        """
        suffix = self._build_identity_suffix(requesting_user)
        if not suffix:
            return base_prompt
        return f"{base_prompt}\n\n{suffix}"

    @staticmethod
    def _extract_text(content_blocks: list[Any]) -> str:
        """Join all text blocks from a Claude response into a single string."""
        return "\n".join(
            block.text for block in content_blocks if getattr(block, "type", None) == "text"
        ).strip()
