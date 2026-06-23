"""SQL safety guard for ATLAS.

ATLAS only ever reads data - never writes. This module is the last line
of defense between Claude's generated SQL and any database adapter.

Rules enforced:
  - Statement must start with SELECT or WITH (...) SELECT.
  - Mutation keywords (DROP / DELETE / UPDATE / INSERT / ALTER / etc.)
    are rejected anywhere in the text, even inside comments. We err on
    the side of paranoia: a "harmless" DROP in a comment is still a
    smell we don't want in the system.
  - Multiple statements (a stray semicolon followed by more SQL) are
    rejected to prevent simple "; DROP TABLE ..." chaining.

Defense in depth: the system prompt also tells Claude to write SELECT
only, the tool description repeats this, and (in production) the DB
user will be read-only. This guard is layer 3 of 4.
"""

from __future__ import annotations

import re

# Keywords that perform writes, schema changes, or arbitrary execution.
# Kept as a frozenset for fast membership checks and immutability.
_BLOCKED_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
        "CREATE", "GRANT", "REVOKE", "MERGE", "REPLACE",
        "EXEC", "EXECUTE", "CALL", "ATTACH", "DETACH",
        "COPY", "BULK", "RENAME",
    }
)

# Pre-compiled regex: word-boundary match for any blocked keyword,
# case-insensitive. Word boundaries prevent false positives on names
# like `inserts_per_day` or `created_date`.
_BLOCKED_PATTERN: re.Pattern[str] = re.compile(
    r"\b(" + "|".join(_BLOCKED_KEYWORDS) + r")\b",
    flags=re.IGNORECASE,
)

# Statements we allow at the start of the SQL.
_ALLOWED_STARTS: frozenset[str] = frozenset({"SELECT", "WITH"})


class SqlGuardError(Exception):
    """Raised when SQL fails the safety check."""


def validate_select_only(sql: str) -> str:
    """Validate that `sql` is a single read-only SELECT (or WITH) statement.

    Args:
        sql: Raw SQL produced by Claude.

    Returns:
        The SQL trimmed of whitespace and any trailing semicolon, ready
        to hand to a database adapter.

    Raises:
        SqlGuardError: If the SQL is empty, multi-statement, starts with
            anything other than SELECT/WITH, or contains a blocked keyword.
    """
    cleaned = (sql or "").strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()

    if not cleaned:
        raise SqlGuardError("Empty SQL statement.")

    # Multi-statement check: any semicolon anywhere in the remaining text
    # would mean a second statement was tacked on.
    if ";" in cleaned:
        raise SqlGuardError(
            "Multiple SQL statements are not allowed. "
            "Submit one SELECT at a time."
        )

    first_word = cleaned.split(None, 1)[0].upper()
    if first_word not in _ALLOWED_STARTS:
        raise SqlGuardError(
            f"Only SELECT or WITH queries are allowed; got {first_word!r}."
        )

    blocked = _BLOCKED_PATTERN.search(cleaned)
    if blocked is not None:
        raise SqlGuardError(
            f"Blocked SQL keyword: {blocked.group(1).upper()}. "
            f"Only read-only SELECT statements are allowed."
        )

    return cleaned
