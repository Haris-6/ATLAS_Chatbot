"""Meeting agenda templates for the `/agenda` shortcut.

When a user types `/agenda <meeting_type> <when>`, ATLAS calls
`prepare_meeting_agenda_template` to get the structure for that meeting
type (section names, time allocations, what each section should contain).
The orchestrator then calls the data tools (Outlook, Teams chat,
SharePoint) to fill each section with real context and assembles the
final markdown agenda.

Keeping the templates here (data, not logic) makes it trivial to add new
meeting types - just append to `_TEMPLATES`.
"""

from __future__ import annotations

from typing import Any

# Canonical meeting templates. Each entry describes the structure of a
# meeting type; the LLM is responsible for FILLING each section using
# the other tools. The `fill_with` text is a hint Claude reads to know
# what context to pull from search_outlook / search_teams_messages /
# search_sharepoint for each slot.
_TEMPLATES: dict[str, dict[str, Any]] = {
    "l10": {
        "name": "Level 10 (L10) Meeting",
        "description": (
            "EOS-style weekly leadership meeting. 75-90 minutes total. "
            "Used by Galaxy leadership and most ops teams."
        ),
        "sections": [
            {
                "name": "AI Pre-Read",
                "duration_min": 5,
                "fill_with": (
                    "Brief preview of what's coming. Top 3 highlights from "
                    "this week's activity across Outlook + Teams + SharePoint."
                ),
            },
            {
                "name": "Check-in",
                "duration_min": 5,
                "fill_with": (
                    "Personal best + business best from the past week for "
                    "each participant. Pull from their recent Teams / Outlook "
                    "activity if mentioned."
                ),
            },
            {
                "name": "Team Pulse",
                "duration_min": 10,
                "fill_with": (
                    "Open items, blockers, and recent feedback. Pull from "
                    "Teams chat messages and reply threads in Outlook."
                ),
            },
            {
                "name": "Scorecard",
                "duration_min": 5,
                "fill_with": (
                    "Key metrics for the week. List any visible from data "
                    "tools (NetSuite counts, sensor stats, MasterControl "
                    "status counts) or note 'manual entry' if not derivable."
                ),
            },
            {
                "name": "Goal Review",
                "duration_min": 5,
                "fill_with": (
                    "Progress on quarterly goals. Pull from SharePoint goal "
                    "documents if available; otherwise list known active "
                    "projects from recent emails."
                ),
            },
            {
                "name": "Headlines",
                "duration_min": 5,
                "fill_with": (
                    "Big news items from the week - releases, customer wins, "
                    "key external emails. Pull from Outlook + recent "
                    "SharePoint changes."
                ),
            },
            {
                "name": "To-Dos",
                "duration_min": 5,
                "fill_with": (
                    "Outstanding action items. Extract from recent Teams "
                    "chats + emails (look for 'pls', 'need', 'should', "
                    "'TODO', deadlines). Format as a table with owner."
                ),
            },
            {
                "name": "Issues (IDS)",
                "duration_min": 30,
                "fill_with": (
                    "Top issues to Identify, Discuss, Solve. Pull from "
                    "feedback / bug-report messages in Teams + escalation "
                    "threads in Outlook. Rank by impact."
                ),
            },
            {
                "name": "Conclude",
                "duration_min": 5,
                "fill_with": (
                    "Wrap-up: rating (1-10), cascading messages, next "
                    "meeting date."
                ),
            },
        ],
        "total_duration_min": 75,
    },
    "standup": {
        "name": "Daily Standup",
        "description": "Quick daily sync. 15 minutes total. Three sections per person.",
        "sections": [
            {
                "name": "Yesterday",
                "duration_min": 5,
                "fill_with": (
                    "What did the user accomplish yesterday? Pull from "
                    "their Outlook + Teams activity from the prior day."
                ),
            },
            {
                "name": "Today",
                "duration_min": 5,
                "fill_with": (
                    "What is the user focused on today? Pull from recent "
                    "Teams commitments and calendar mentions in chat."
                ),
            },
            {
                "name": "Blockers",
                "duration_min": 5,
                "fill_with": (
                    "Any blockers or asks for help? Pull from recent Teams "
                    "messages flagged as questions or escalations."
                ),
            },
        ],
        "total_duration_min": 15,
    },
    "retro": {
        "name": "Weekly Retrospective",
        "description": (
            "What went well, what didn't, what to improve. 30 minutes."
        ),
        "sections": [
            {
                "name": "What went well",
                "duration_min": 10,
                "fill_with": (
                    "Shipped items, wins, positive feedback. Pull from "
                    "recent SharePoint changes + Teams chat highlights + "
                    "celebratory emails."
                ),
            },
            {
                "name": "What didn't go well",
                "duration_min": 10,
                "fill_with": (
                    "Misses, bugs, frustrations. Pull from Teams "
                    "bug-report messages + escalation email threads."
                ),
            },
            {
                "name": "What to improve",
                "duration_min": 10,
                "fill_with": (
                    "Concrete action items derived from the issues. "
                    "Format as a table with owner + by-when."
                ),
            },
        ],
        "total_duration_min": 30,
    },
    "1on1": {
        "name": "1-on-1 Sync",
        "description": (
            "Personal check-in between the asking user and one other "
            "person. 30 minutes."
        ),
        "sections": [
            {
                "name": "Check-in",
                "duration_min": 5,
                "fill_with": (
                    "How is the other person doing? Personal + professional "
                    "vibes from recent shared chats and emails."
                ),
            },
            {
                "name": "What's working",
                "duration_min": 10,
                "fill_with": (
                    "Recent wins and momentum from the other person's work, "
                    "based on shared communications."
                ),
            },
            {
                "name": "What's blocked or frustrating",
                "duration_min": 10,
                "fill_with": (
                    "Recent issues, asks, or frustrations from the other "
                    "person's Teams + Outlook messages."
                ),
            },
            {
                "name": "Action items",
                "duration_min": 5,
                "fill_with": (
                    "Concrete next steps for both parties, with owner."
                ),
            },
        ],
        "total_duration_min": 30,
    },
}

# Aliases mapping user-friendly names to canonical keys.
_ALIASES: dict[str, str] = {
    "l10": "l10",
    "levelten": "l10",
    "level10": "l10",
    "leadership": "l10",
    "standup": "standup",
    "daily": "standup",
    "retro": "retro",
    "retrospective": "retro",
    "1on1": "1on1",
    "121": "1on1",
    "onetoone": "1on1",
    "oneonone": "1on1",
}


def get_template(meeting_type: str) -> dict[str, Any]:
    """Return the template dict for a meeting type, or an error dict.

    Args:
        meeting_type: User-supplied type (e.g. "L10", "standup", "retro").

    Returns:
        Dict with `name`, `description`, `sections`, `total_duration_min` OR
        a dict with an `error` key and a list of valid types.
    """
    key = meeting_type.strip().lower().replace("-", "").replace("_", "")
    canonical = _ALIASES.get(key, key)
    if canonical not in _TEMPLATES:
        return {
            "error": (
                f"Unknown meeting type: {meeting_type!r}. "
                f"Valid types: {sorted(_TEMPLATES)}"
            ),
        }
    return _TEMPLATES[canonical]


def list_meeting_types() -> list[str]:
    """Return all supported meeting type keys."""
    return sorted(_TEMPLATES)
