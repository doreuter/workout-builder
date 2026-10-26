"""Settings from environment variables (and .env). Read at call time so tests can change them."""

from __future__ import annotations

import os


def hosted() -> bool:
    """APP_MODE=hosted: running on a server, so features that touch this computer's files are off."""
    return os.environ.get("APP_MODE", "local").lower() == "hosted"


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def invites_required() -> bool:
    """Hosted apps are invite-only unless REQUIRE_INVITE=0."""
    default = "1" if hosted() else "0"
    return os.environ.get("REQUIRE_INVITE", default) == "1"


def daily_message_limit() -> int:
    return int(os.environ.get("DAILY_MESSAGE_LIMIT", "30"))


def monthly_budget_usd() -> float:
    """Total Claude spend per calendar month across all users; the chat pauses above it. 0 = no limit."""
    return float(os.environ.get("MONTHLY_BUDGET_USD", "10"))
