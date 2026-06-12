"""LLM-powered detector for meeting/scheduling intent in incoming emails."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...config import get_settings
from ...logging_config import logger
from ...openrouter_client import OpenRouterError, request_chat_completion


@dataclass
class SchedulingContext:
    """Structured scheduling intent extracted from an email."""

    proposed_times: List[str]
    meeting_topic: str
    is_proposing: bool  # True = sender proposes times; False = sender asks for your availability


_TOOL_NAME = "extract_scheduling_intent"
_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": "Detect whether an email is trying to schedule a meeting and extract the relevant details.",
        "parameters": {
            "type": "object",
            "properties": {
                "has_scheduling_intent": {
                    "type": "boolean",
                    "description": (
                        "True only when the email explicitly proposes specific meeting times, "
                        "asks the recipient to share availability, or requests to schedule a call/meeting. "
                        "False for already-confirmed meeting reminders, calendar notifications, "
                        "and general scheduling-adjacent mentions."
                    ),
                },
                "proposed_times": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Time expressions mentioned by the sender, quoted exactly as written "
                        "(e.g. 'Thursday at 2 pm', 'next Monday morning', 'anytime this week'). "
                        "Empty when the sender is only asking for the recipient's availability."
                    ),
                },
                "meeting_topic": {
                    "type": "string",
                    "description": "One short phrase describing what the meeting is about.",
                },
                "is_proposing": {
                    "type": "boolean",
                    "description": (
                        "True if the sender is proposing specific times. "
                        "False if the sender is asking the recipient to name times."
                    ),
                },
            },
            "required": ["has_scheduling_intent"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = (
    "You analyze email messages to detect meeting scheduling intent. "
    "Set has_scheduling_intent=true only when the email is actively trying to set up "
    "a meeting, call, or appointment — either by proposing specific times or by asking "
    "the recipient when they are available. "
    "Ignore reminders for already-booked events, automated calendar notifications, "
    "and passing references to schedules. "
    "Extract proposed times verbatim as the sender wrote them."
)


async def detect_scheduling_intent(
    email_body: str,
    email_subject: str = "",
) -> Optional[SchedulingContext]:
    """Return SchedulingContext when email has actionable scheduling intent, else None."""
    settings = get_settings()
    api_key = settings.openrouter_api_key
    model = settings.email_classifier_model

    if not api_key:
        return None

    user_content = f"Subject: {email_subject}\n\n{email_body}"
    try:
        response = await request_chat_completion(
            model=model,
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM_PROMPT,
            api_key=api_key,
            tools=[_TOOL_SCHEMA],
        )
    except (OpenRouterError, Exception) as exc:
        logger.warning("Scheduling intent detection failed", extra={"error": str(exc)})
        return None

    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []

    for tool_call in tool_calls:
        fn = tool_call.get("function") or {}
        if fn.get("name") != _TOOL_NAME:
            continue
        args = _parse_args(fn.get("arguments"))
        if args is None or not args.get("has_scheduling_intent"):
            return None
        return SchedulingContext(
            proposed_times=[t for t in (args.get("proposed_times") or []) if isinstance(t, str)],
            meeting_topic=(args.get("meeting_topic") or "").strip(),
            is_proposing=bool(args.get("is_proposing", True)),
        )

    return None


def _parse_args(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None
    return None


__all__ = ["SchedulingContext", "detect_scheduling_intent"]
