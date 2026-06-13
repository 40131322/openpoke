"""Aggregate execution agent tool schemas and registries."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from . import calendar, gmail, triggers
from ..tasks import get_task_registry, get_task_schemas

_CALENDAR_KEYWORDS = ("calendar", "schedule", "event", "availability", "meeting")
_EMAIL_KEYWORDS = ("email", "gmail", "mail", "draft", "inbox", "message")


def _is_calendar_only(agent_name: str) -> bool:
    name = agent_name.lower()
    return any(kw in name for kw in _CALENDAR_KEYWORDS) and not any(
        kw in name for kw in _EMAIL_KEYWORDS
    )


def _is_email_only(agent_name: str) -> bool:
    name = agent_name.lower()
    return any(kw in name for kw in _EMAIL_KEYWORDS) and not any(
        kw in name for kw in _CALENDAR_KEYWORDS
    )


# Return OpenAI/OpenRouter-compatible tool schemas filtered by agent purpose
def get_tool_schemas(agent_name: str = "") -> List[Dict[str, Any]]:
    """Return tool schemas relevant to the agent's purpose.

    Calendar-named agents get no email-search tool; email-named agents get no
    calendar tools; mixed/unnamed agents get everything.
    """
    if _is_calendar_only(agent_name):
        return [*calendar.get_schemas(), *triggers.get_schemas()]
    if _is_email_only(agent_name):
        return [*gmail.get_schemas(), *get_task_schemas(), *triggers.get_schemas()]
    return [
        *gmail.get_schemas(),
        *calendar.get_schemas(),
        *get_task_schemas(),
        *triggers.get_schemas(),
    ]


# Return Python callables for executing tools by name, filtered by agent purpose
def get_tool_registry(agent_name: str) -> Dict[str, Callable[..., Any]]:
    """Return callables matching the schemas returned by get_tool_schemas."""
    registry: Dict[str, Callable[..., Any]] = {}
    if _is_calendar_only(agent_name):
        registry.update(calendar.build_registry(agent_name))
        registry.update(triggers.build_registry(agent_name))
        return registry
    if _is_email_only(agent_name):
        registry.update(gmail.build_registry(agent_name))
        registry.update(get_task_registry(agent_name))
        registry.update(triggers.build_registry(agent_name))
        return registry
    registry.update(gmail.build_registry(agent_name))
    registry.update(calendar.build_registry(agent_name))
    registry.update(get_task_registry(agent_name))
    registry.update(triggers.build_registry(agent_name))
    return registry


__all__ = [
    "get_tool_registry",
    "get_tool_schemas",
]
