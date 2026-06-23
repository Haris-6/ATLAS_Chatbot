"""Claude tool definitions backed by Galaxy's real data lake.

Each tool couples three things in one place:
  1. Schema Claude sees (name, description, input_schema)
  2. Python handler that calls into the data layer
  3. Output shape returned to Claude as JSON

Tools are produced by `build_tools(config)` - a factory that instantiates
the data-lake stores once and threads them through to handler closures.
This keeps caches (e.g. the 645-doc MasterControl cache) shared across
all tool calls in a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.config import Config
from src.data.datalake_client import DataLakeClient
from src.data.dataninja import DataNinjaStore
from src.data.graph_client import GraphClient
from src.data.mastercontrol import MasterControlStore
from src.data.meeting_templates import get_template, list_meeting_types
from src.data.netsuite import NetSuiteError, NetSuiteStore
from src.data.oceaview import OceaviewError, OceaviewStore
from src.data.outlook import OutlookStore
from src.data.sharepoint import SharePointStore
from src.data.teams_chat import TeamsChatStore
from src.utils.logger import get_logger

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    """A tool Claude can choose to call.

    The `description` is the most important field - Claude relies on it
    to decide WHEN to use the tool, so it must clearly state the tool's
    purpose and the kinds of questions it answers.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Return the schema in the shape the Anthropic SDK expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def build_tools(config: Config) -> list[Tool]:
    """Construct the full tool set backed by live data-lake stores.

    Stores are instantiated once here and captured by handler closures so
    in-memory caches (notably MasterControl's 645 documents) are shared
    across every tool invocation.

    Args:
        config: Loaded application configuration.

    Returns:
        List of `Tool` instances ready to register with the orchestrator.
    """
    log = get_logger(__name__, config.log_level)

    client = DataLakeClient(
        connection_string=config.azure_storage_connection_string,
        log_level=config.log_level,
    )
    mastercontrol = MasterControlStore(client, config.log_level)
    netsuite = NetSuiteStore(client, config.log_level)
    oceaview = OceaviewStore(client, config.log_level)
    dataninja = DataNinjaStore(client, config.log_level)

    tools: list[Tool] = [
        _build_mastercontrol_tool(mastercontrol),
        _build_netsuite_tool(netsuite),
        _build_oceaview_tool(oceaview),
        _build_dataninja_tool(dataninja),
        _build_meeting_agenda_tool(),
    ]

    # SharePoint (Microsoft Graph) is only wired in when Bot credentials
    # are present. Locally, scripts like `hello.py` can still run config
    # validation without M365 access; the SharePoint tool just won't be
    # registered for them.
    if config.bot_app_id and config.bot_app_password and config.bot_tenant_id:
        graph = GraphClient(
            tenant_id=config.bot_tenant_id,
            client_id=config.bot_app_id,
            client_secret=config.bot_app_password,
            log_level=config.log_level,
        )
        sharepoint = SharePointStore(graph, log_level=config.log_level)
        outlook = OutlookStore(graph, log_level=config.log_level)
        teams_chat = TeamsChatStore(
            graph,
            bot_app_id=config.bot_app_id,
            log_level=config.log_level,
        )
        tools.append(_build_sharepoint_tool(sharepoint))
        tools.append(_build_outlook_tool(outlook))
        tools.append(_build_teams_chat_tool(teams_chat))
    else:
        log.warning(
            "SharePoint + Outlook + Teams chat tools skipped: BOT_APP_ID / "
            "BOT_APP_PASSWORD / BOT_TENANT_ID missing. Set them in "
            ".env to enable them."
        )

    return tools


# ---------------------------------------------------------------------------
# Per-tool builders
# ---------------------------------------------------------------------------


def _build_mastercontrol_tool(store: MasterControlStore) -> Tool:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        doc_type = args.get("doc_type")
        status = args.get("status")
        limit = _coerce_int(args.get("limit"), default=20, minimum=1, maximum=100)

        results = store.search(
            query=query,
            doc_type=doc_type,
            status=status,
            limit=limit,
        )
        return {
            "count": len(results),
            "results": results,
            "filters": {"query": query, "doc_type": doc_type, "status": status},
            "source": "MasterControl",
        }

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Case-insensitive substring searched against the "
                    "document title and number (e.g. 'cleaning', 'SOP-064')."
                ),
            },
            "doc_type": {
                "type": "string",
                "description": (
                    "Filter by document type. Common values: 'SOP', 'CAPA', "
                    "'Log', 'Form'."
                ),
            },
            "status": {
                "type": "string",
                "description": (
                    "Filter by lifecycle status, e.g. 'Draft', 'Effective', "
                    "'Obsolete'."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 20, max 100).",
            },
        },
    }

    return Tool(
        name="search_mastercontrol_documents",
        description=(
            "Search Galaxy's MasterControl quality system for documents "
            "(SOPs, CAPAs, Logs, Forms, etc.). Returns matching documents "
            "with title, number, type, status, revision, and owner. "
            "Use this whenever the user asks about procedures, deviations, "
            "training documents, or any formal quality record."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_netsuite_tool(store: NetSuiteStore) -> Tool:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        entity = str(args.get("entity", "")).strip()
        try:
            return store.get_current(entity)
        except NetSuiteError as exc:
            return {"error": str(exc)}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "enum": ["customers", "sales_orders", "purchase_orders"],
                "description": "Which NetSuite entity to fetch.",
            },
        },
        "required": ["entity"],
    }

    return Tool(
        name="get_netsuite_data",
        description=(
            "Get the latest snapshot of one entity from Galaxy's NetSuite ERP: "
            "customers (with balances and contact info), sales_orders, or "
            "purchase_orders. Use for questions about customers, vendors, "
            "orders, invoices, or financial transactions."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_oceaview_tool(store: OceaviewStore) -> Tool:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        sensor = str(args.get("sensor", "")).strip()
        date_from = args.get("date_from") or None
        date_to = args.get("date_to") or None
        sample_limit = _coerce_int(
            args.get("sample_limit"), default=100, minimum=0, maximum=1000
        )
        try:
            return store.get_readings(
                sensor=sensor,
                date_from=date_from,
                date_to=date_to,
                sample_limit=sample_limit,
            )
        except OceaviewError as exc:
            return {"error": str(exc)}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "sensor": {
                "type": "string",
                "enum": ["humidity", "pressure", "temperature"],
                "description": "Which environmental sensor to query.",
            },
            "date_from": {
                "type": "string",
                "description": (
                    "Inclusive lower bound (YYYY-MM-DD or ISO 8601). Omit "
                    "to read from the beginning of the available data."
                ),
            },
            "date_to": {
                "type": "string",
                "description": "Inclusive upper bound (YYYY-MM-DD or ISO 8601).",
            },
            "sample_limit": {
                "type": "integer",
                "description": (
                    "Max raw readings to include in the response (default 100). "
                    "Stats (min/max/mean) are computed over ALL matching readings."
                ),
            },
        },
        "required": ["sensor"],
    }

    return Tool(
        name="query_environmental_sensors",
        description=(
            "Query Oceaview environmental sensor readings (humidity, pressure, "
            "temperature) from Galaxy's facility. Returns total reading count, "
            "min/max/mean/stdev statistics, and a sample of raw readings. "
            "Use for cleanroom monitoring, environmental excursion analysis, "
            "or any facility-conditions question."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_sharepoint_tool(store: SharePointStore) -> Tool:
    """Build the live SharePoint search tool (Microsoft Graph backed)."""

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=25)
        if not query:
            return {"error": "'query' is required for SharePoint search."}
        try:
            return store.search(query=query, limit=limit)
        except Exception as exc:  # noqa: BLE001 - report cleanly to Claude
            return {"error": f"SharePoint search failed: {exc}", "source": "SharePoint"}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword query. Plain text works; KQL syntax is also "
                    "accepted (quoted phrases like '\"media fill\"', or "
                    "field filters like 'filename:*.pdf')."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 10, max 25).",
            },
        },
        "required": ["query"],
    }

    return Tool(
        name="search_sharepoint",
        description=(
            "Search Galaxy's live Microsoft SharePoint and OneDrive for "
            "any document: SOPs, policies, templates, logs, project files, "
            "personal documents, etc. Returns title, clickable URL, "
            "last-modified date, and a content snippet with match context. "
            "Use this whenever the user needs the actual file, a URL, or "
            "to read document content. For authoritative quality-system "
            "metadata (current revision, lifecycle status of a CAPA), "
            "prefer `search_mastercontrol_documents` instead. When the "
            "user asks vaguely about a 'document about X', try both."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_outlook_tool(store: OutlookStore) -> Tool:
    """Build the Outlook mailbox search tool (Microsoft Graph backed)."""

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        mailbox = str(args.get("mailbox") or "").strip()
        limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=25)
        if not query:
            return {"error": "'query' is required for Outlook search."}
        if not mailbox:
            return {
                "error": (
                    "'mailbox' is required (the user UPN/email whose mailbox "
                    "to search). For 'my emails' queries, use the current "
                    "user's email from your context."
                ),
                "source": "Outlook",
            }
        try:
            return store.search_mailbox(user_upn=mailbox, query=query, limit=limit)
        except Exception as exc:  # noqa: BLE001 - report cleanly to Claude
            return {"error": f"Outlook search failed: {exc}", "source": "Outlook"}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword query. Plain text or quoted phrase for exact "
                    "match (e.g. '\"media fill\"'). Outlook search "
                    "supports KQL operators too (from:, subject:, hasAttachments:true)."
                ),
            },
            "mailbox": {
                "type": "string",
                "description": (
                    "UPN/email of the mailbox to search (e.g. "
                    "'faizan.riaz@galaxypharma.net'). REQUIRED. For 'my "
                    "emails' queries, use the current user's email which "
                    "appears in your system context. For 'what did X "
                    "send' queries, use X's email."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default 10, max 25).",
            },
        },
        "required": ["query", "mailbox"],
    }

    return Tool(
        name="search_outlook",
        description=(
            "Search a specific user's Outlook mailbox for emails matching "
            "the query. Returns subject, sender, received date, body "
            "preview, attachment flag, and a webLink to open the email. "
            "Default behavior: when the user says 'my emails' or 'emails "
            "about X', search the asking user's own mailbox. To search "
            "someone else's mailbox, pass their UPN/email explicitly."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_teams_chat_tool(store: TeamsChatStore) -> Tool:
    """Build the Teams chat-message search tool.

    Strict per-user filtering: results only include messages from chats
    the requesting user is already a participant in.
    """

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        user_upn = str(args.get("user_upn") or "").strip()
        limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=25)
        if not query:
            return {"error": "'query' is required for Teams chat search."}
        if not user_upn:
            return {
                "error": (
                    "'user_upn' is required (the asking user's UPN/email). "
                    "This scopes results to chats that user is in - never "
                    "leak other users' chats. Use the email from your "
                    "system context."
                ),
                "source": "Teams",
            }
        try:
            return store.search_user_chats(
                user_upn=user_upn, query=query, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 - report cleanly to Claude
            return {"error": f"Teams chat search failed: {exc}", "source": "Teams"}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword query for Teams chat messages. Plain text or "
                    "KQL (e.g. 'cleaning SOP', 'from:rosa cleaning')."
                ),
            },
            "user_upn": {
                "type": "string",
                "description": (
                    "REQUIRED. UPN/email of the asking user. Results are "
                    "filtered to ONLY chats this user is in (privacy). "
                    "Use the current user's email from your system context."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default 10, max 25).",
            },
        },
        "required": ["query", "user_upn"],
    }

    return Tool(
        name="search_teams_messages",
        description=(
            "Search the asking user's Microsoft Teams chat history for "
            "messages matching the query. Returns sender, created date, "
            "message preview, and a webUrl to open the chat in Teams. "
            "Strict per-user scoping: only chats the asking user is in "
            "are searched. Use for 'what did X say in our chat about Y' "
            "or 'find my Teams messages about Z' style questions."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_meeting_agenda_tool() -> Tool:
    """Return the section / time structure for a meeting type.

    Doesn't need any store - just looks up a static template. Claude is
    expected to call this FIRST when the user invokes the `/agenda`
    shortcut (or any "prepare meeting agenda" request), then call the
    data tools (Outlook + Teams + SharePoint) to fill each section, and
    finally synthesize the final markdown agenda.
    """

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        meeting_type = str(args.get("meeting_type") or "l10").strip()
        return get_template(meeting_type)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "meeting_type": {
                "type": "string",
                "enum": list_meeting_types(),
                "description": (
                    "Meeting format. `l10` = EOS Level 10 weekly leadership "
                    "(75 min). `standup` = daily 15-min sync. `retro` = "
                    "weekly retrospective (30 min). `1on1` = personal sync "
                    "between two people (30 min). Default to `l10` if "
                    "unspecified."
                ),
            },
        },
        "required": ["meeting_type"],
    }

    return Tool(
        name="prepare_meeting_agenda_template",
        description=(
            "Return the section / time structure for a meeting agenda. "
            "Call this FIRST whenever the user asks to generate or prep "
            "a meeting agenda (e.g. '/agenda Friday', 'L10 prep for "
            "tomorrow', 'standup notes for today'). The response gives "
            "you the section names, time allocations, and a `fill_with` "
            "hint per section explaining what context to pull. Then call "
            "the data tools (search_outlook, search_teams_messages, "
            "search_sharepoint) to fill each section with real content, "
            "and format the final agenda as markdown with section headers "
            "and the time in parentheses."
        ),
        input_schema=schema,
        handler=handler,
    )


def _build_dataninja_tool(store: DataNinjaStore) -> Tool:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        limit = _coerce_int(args.get("limit"), default=20, minimum=1, maximum=100)
        if not query:
            return store.get_summary()
        return store.search(query=str(query), limit=limit)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text search across all DataNinja record fields. "
                    "Omit to get a top-level summary of available records."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max records to return (default 20, max 100).",
            },
        },
    }

    return Tool(
        name="query_dataninja_parts",
        description=(
            "Search DataNinja parts and inventory data. Returns matching parts "
            "with details such as partNumber, partType, name, manufacturing "
            "info, and lifecycle. Use for inventory, raw materials, or "
            "part-revision questions."
        ),
        input_schema=schema,
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Coerce a possibly-missing or string-typed value to an int with bounds."""
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(minimum, min(maximum, n))
