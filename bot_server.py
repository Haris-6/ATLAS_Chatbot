"""ATLAS chat server.

Exposes the aiohttp `app` at module level so it can be served by either
the built-in `web.run_app` (local dev) or a WSGI/ASGI host like gunicorn
on Azure App Service.

Run locally:

    python bot_server.py

Run on Azure App Service (startup command):

    gunicorn --bind=0.0.0.0:8000 --timeout 600 \
        --worker-class aiohttp.GunicornWebWorker bot_server:app

Bot App ID + password may be blank for the Bot Framework Emulator. For
real Teams, set BOT_APP_ID / BOT_APP_PASSWORD / BOT_TENANT_ID via env
vars (or `.env` locally).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# When gunicorn imports this file from /tmp/<oryx-extract-path>, the
# directory holding `src/` is NOT on sys.path by default. Add it so the
# `from src...` imports below resolve on Azure App Service.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.schema import Activity

from src.bot.atlas_bot import AtlasBot
from src.config import Config, ConfigError
from src.data.graph_client import GraphClient
from src.data.user_resolver import UserResolver
from src.llm.claude_client import ClaudeError
from src.llm.conversation_memory import ConversationMemory
from src.llm.orchestrator import ToolOrchestrator
from src.llm.tools import build_tools
from src.utils.console import ensure_utf8_stdout
from src.utils.logger import get_logger

# Host/port for the local `web.run_app` path. On Azure, gunicorn is
# launched from the App Service startup command with its own --bind, so
# these defaults are only used during local development.
_HOST = os.getenv("HOST", "localhost")
_PORT = int(os.getenv("PORT", "3978"))


def _build_app(config: Config) -> web.Application:
    """Build the aiohttp app with the bot wired up.

    Returns:
        A fully-configured `web.Application` with `/api/messages` and
        `/health` routes attached.
    """
    log = get_logger("bot_server", config.log_level)

    tools = build_tools(config)
    log.info("Registered %d tools: %s", len(tools), [t.name for t in tools])

    memory = ConversationMemory(
        api_key=config.anthropic_api_key,
        log_level=config.log_level,
    )

    # User resolver only makes sense when bot credentials are present
    # (it talks to Graph). For local dev without bot creds we leave it
    # off and the orchestrator gracefully skips identity injection.
    user_resolver: UserResolver | None = None
    if config.bot_app_id and config.bot_app_password and config.bot_tenant_id:
        resolver_graph = GraphClient(
            tenant_id=config.bot_tenant_id,
            client_id=config.bot_app_id,
            client_secret=config.bot_app_password,
            log_level=config.log_level,
        )
        user_resolver = UserResolver(resolver_graph, log_level=config.log_level)

    orchestrator = ToolOrchestrator(
        api_key=config.anthropic_api_key,
        tools=tools,
        log_level=config.log_level,
        memory=memory,
        user_resolver=user_resolver,
    )
    bot = AtlasBot(orchestrator=orchestrator, log_level=config.log_level)

    # `channel_auth_tenant` is required for Single-Tenant bots registered
    # in Azure AD. None makes the adapter behave the way it did during
    # local Emulator testing (multi-tenant or anonymous).
    adapter = BotFrameworkAdapter(
        BotFrameworkAdapterSettings(
            app_id=config.bot_app_id,
            app_password=config.bot_app_password,
            channel_auth_tenant=config.bot_tenant_id or None,
        )
    )

    async def _on_turn_error(turn_context: TurnContext, error: Exception) -> None:
        """Last-resort handler so framework errors don't crash the process."""
        log.error("Unhandled adapter error: %s", error)
        await turn_context.send_activity(
            "Sorry - I hit an internal error. Please try again."
        )

    adapter.on_turn_error = _on_turn_error

    async def messages_handler(req: web.Request) -> web.Response:
        """Receive a Bot Framework Activity payload and dispatch it to the bot."""
        if "application/json" not in (req.headers.get("Content-Type") or "").lower():
            return web.Response(
                status=415, text="Content-Type must be application/json"
            )

        body = await req.json()
        activity = Activity().deserialize(body)
        auth_header = req.headers.get("Authorization", "")
        await adapter.process_activity(activity, auth_header, bot.on_turn)
        return web.Response(status=200)

    async def health_handler(req: web.Request) -> web.Response:
        """Simple liveness probe useful for monitoring + sanity checks."""
        return web.json_response({"status": "ok", "service": "atlas-bot"})

    async def chat_handler(req: web.Request) -> web.Response:
        """Localhost-only test endpoint for Postman / curl debugging.

        Bypasses Bot Framework auth so you can drive the orchestrator
        directly with `{"message": "...", "conversation_id": "..."}`.
        Same orchestrator and memory backing as the Teams path, so this
        is the simplest way to verify conversation memory end-to-end.

        Rejects non-loopback requests with 403 so an accidentally-public
        Azure deployment can't be abused to burn API credits.
        """
        remote = req.remote or ""
        if not _is_loopback(remote):
            log.warning("Blocked /chat from non-loopback: %s", remote)
            return web.json_response(
                {"error": "Forbidden - /chat is localhost-only"},
                status=403,
            )

        content_type = (req.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            return web.json_response(
                {"error": "Content-Type must be application/json"},
                status=415,
            )

        try:
            body = await req.json()
        except json.JSONDecodeError as exc:
            return web.json_response(
                {"error": f"Invalid JSON: {exc}"}, status=400
            )

        message = (body.get("message") or "").strip()
        conversation_id = (body.get("conversation_id") or "postman-test").strip()
        if not message:
            return web.json_response(
                {"error": "'message' field is required"}, status=400
            )

        # For Postman/curl debugging there's no Bot Framework activity to
        # pull identity from; let callers supply an email + name directly.
        requesting_user = None
        user_email = (body.get("user_email") or "").strip()
        user_name = (body.get("user_name") or "").strip()
        if user_email:
            requesting_user = {"email": user_email, "name": user_name}

        log.info("/chat called: conv=%s msg=%r user=%s",
                 conversation_id, message[:80], user_email or "(none)")

        try:
            result = await asyncio.to_thread(
                orchestrator.run,
                prompt=message,
                conversation_id=conversation_id,
                requesting_user=requesting_user,
            )
        except ClaudeError as exc:
            log.error("/chat orchestrator error: %s", exc)
            return web.json_response({"error": str(exc)}, status=502)

        return web.json_response({
            "text": result.text,
            "tool_calls": result.tool_calls,
            "iterations": result.iterations,
            "tokens_in": result.total_input_tokens,
            "tokens_out": result.total_output_tokens,
            "cost_usd": round(result.estimated_cost_usd, 6),
            "conversation_id": conversation_id,
        })

    application = web.Application()
    application.router.add_post("/api/messages", messages_handler)
    application.router.add_get("/health", health_handler)
    application.router.add_post("/chat", chat_handler)
    return application


def _is_loopback(remote: str) -> bool:
    """Return True if `remote` is a loopback / localhost address."""
    if not remote:
        return False
    addr = remote.split("%", 1)[0].lower()
    return (
        addr.startswith("127.")
        or addr == "::1"
        or addr == "localhost"
        or addr.startswith("[::1]")
    )


# ---------------------------------------------------------------------------
# Module-level `app` so external hosts (gunicorn, hypercorn) can import it.
# Constructed eagerly so config errors surface at import time, not on the
# first request.
# ---------------------------------------------------------------------------

try:
    _config = Config.load()
    app: web.Application = _build_app(_config)
except ConfigError as _exc:
    print(f"[CONFIG ERROR] {_exc}", file=sys.stderr)
    raise


def main() -> int:
    """Local-dev entry point. Returns a shell exit code (0=ok, 1=failure)."""
    ensure_utf8_stdout()
    log = get_logger("bot_server", _config.log_level)
    log.info("ATLAS bot server starting on http://%s:%d/api/messages", _HOST, _PORT)
    log.info("Connect Bot Framework Emulator to that URL (leave App ID/Password blank)")
    log.info("Health check: http://%s:%d/health", _HOST, _PORT)

    web.run_app(app, host=_HOST, port=_PORT, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
