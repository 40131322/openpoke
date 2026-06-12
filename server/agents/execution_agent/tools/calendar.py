"""Google Calendar tool schemas and actions for the execution agent.

Data plane: direct Google Calendar REST API via server.services.calendar.google_rest.
Auth plane: Composio OAuth (connect / disconnect / status) — unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Union

from server.services.calendar import get_active_calendar_user_id, get_calendar_access_token
from server.services.calendar import google_rest

_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calendar_list_events",
            "description": "List or search Google Calendar events within a time range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID to query. Defaults to 'primary'.",
                    },
                    "time_min": {
                        "type": "string",
                        "description": "Lower bound (RFC3339 timestamp) for event start times.",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "Upper bound (RFC3339 timestamp) for event start times.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Free-text search term to filter events.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of events to return.",
                    },
                    "single_events": {
                        "type": "boolean",
                        "description": "Expand recurring events into individual instances when true.",
                    },
                    "order_by": {
                        "type": "string",
                        "description": "Sort order: 'startTime' or 'updated'.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_create_event",
            "description": "Create a new Google Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID to create the event in. Defaults to 'primary'.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Event title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Event description or notes.",
                    },
                    "start_datetime": {
                        "type": "string",
                        "description": "Event start as RFC3339 timestamp (e.g. '2025-06-15T10:00:00-05:00').",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": "Event end as RFC3339 timestamp.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name (e.g. 'America/New_York'). Required for timed events.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Physical or virtual location for the event.",
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of attendee email addresses.",
                    },
                    "send_updates": {
                        "type": "string",
                        "description": "Whether to send email invites: 'all', 'externalOnly', or 'none'.",
                    },
                },
                "required": ["summary", "start_datetime", "end_datetime"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_update_event",
            "description": "Update an existing Google Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID of the event to update.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID containing the event. Defaults to 'primary'.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Updated event title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Updated event description.",
                    },
                    "start_datetime": {
                        "type": "string",
                        "description": "Updated start time as RFC3339 timestamp.",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": "Updated end time as RFC3339 timestamp.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for the updated times.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Updated location.",
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Updated list of attendee email addresses.",
                    },
                    "send_updates": {
                        "type": "string",
                        "description": "Whether to send update notifications: 'all', 'externalOnly', or 'none'.",
                    },
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_delete_event",
            "description": "Delete a Google Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID of the event to delete.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID containing the event. Defaults to 'primary'.",
                    },
                    "send_updates": {
                        "type": "string",
                        "description": "Whether to notify attendees: 'all', 'externalOnly', or 'none'.",
                    },
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_get_event",
            "description": "Retrieve a specific Google Calendar event by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID of the event to retrieve.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID containing the event. Defaults to 'primary'.",
                    },
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_list_calendars",
            "description": "List all Google Calendars accessible to the authenticated user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_access_role": {
                        "type": "string",
                        "description": "Filter by minimum access role: 'freeBusyReader', 'reader', 'writer', or 'owner'.",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "Include hidden calendars when true.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_find_free_slots",
            "description": "Find free time slots across one or more Google Calendars using the freebusy API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_min": {
                        "type": "string",
                        "description": "Start of the interval to check (RFC3339 timestamp).",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "End of the interval to check (RFC3339 timestamp).",
                    },
                    "calendar_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of calendar IDs to check. Defaults to ['primary'].",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for interpreting the times.",
                    },
                },
                "required": ["time_min", "time_max"],
                "additionalProperties": False,
            },
        },
    },
]


def get_schemas() -> List[Dict[str, Any]]:
    return _SCHEMAS


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _token_or_error() -> Union[str, Dict[str, Any]]:
    """Return the live access token or an error dict the runtime will surface as a tool failure."""
    token = get_calendar_access_token()
    if token:
        return token
    if not get_active_calendar_user_id():
        return {"error": "Google Calendar not connected. Please connect Calendar in settings first."}
    return {"error": "Could not retrieve Calendar access token. Try reconnecting Calendar in settings."}


def _run(fn: Callable[..., Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
    """Get a token then call a google_rest function; surface any HTTP errors as error dicts."""
    tok = _token_or_error()
    if isinstance(tok, dict):
        return tok
    try:
        return fn(tok, **kwargs)
    except RuntimeError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool callables
# ---------------------------------------------------------------------------

def calendar_list_events(
    calendar_id: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    query: Optional[str] = None,
    max_results: Optional[int] = None,
    single_events: Optional[bool] = None,
    order_by: Optional[str] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.list_events,
        calendar_id=calendar_id or "primary",
        time_min=time_min,
        time_max=time_max,
        query=query,
        max_results=max_results,
        single_events=True if single_events is None else single_events,
        order_by=order_by,
    )


def calendar_create_event(
    summary: str,
    start_datetime: str,
    end_datetime: str,
    calendar_id: Optional[str] = None,
    description: Optional[str] = None,
    timezone: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.create_event,
        summary=summary,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        calendar_id=calendar_id or "primary",
        description=description,
        timezone=timezone,
        location=location,
        attendees=attendees,
        send_updates=send_updates,
    )


def calendar_update_event(
    event_id: str,
    calendar_id: Optional[str] = None,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.update_event,
        event_id=event_id,
        calendar_id=calendar_id or "primary",
        summary=summary,
        description=description,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        timezone=timezone,
        location=location,
        attendees=attendees,
        send_updates=send_updates,
    )


def calendar_delete_event(
    event_id: str,
    calendar_id: Optional[str] = None,
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.delete_event,
        event_id=event_id,
        calendar_id=calendar_id or "primary",
        send_updates=send_updates,
    )


def calendar_get_event(
    event_id: str,
    calendar_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.get_event,
        event_id=event_id,
        calendar_id=calendar_id or "primary",
    )


def calendar_list_calendars(
    min_access_role: Optional[str] = None,
    show_hidden: Optional[bool] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.list_calendars,
        min_access_role=min_access_role,
        show_hidden=show_hidden,
    )


def calendar_find_free_slots(
    time_min: str,
    time_max: str,
    calendar_ids: Optional[List[str]] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    return _run(
        google_rest.find_free_slots,
        time_min=time_min,
        time_max=time_max,
        calendar_ids=calendar_ids or ["primary"],
        timezone=timezone,
    )


def build_registry(agent_name: str) -> Dict[str, Callable[..., Any]]:  # noqa: ARG001
    return {
        "calendar_list_events": calendar_list_events,
        "calendar_create_event": calendar_create_event,
        "calendar_update_event": calendar_update_event,
        "calendar_delete_event": calendar_delete_event,
        "calendar_get_event": calendar_get_event,
        "calendar_list_calendars": calendar_list_calendars,
        "calendar_find_free_slots": calendar_find_free_slots,
    }


__all__ = [
    "build_registry",
    "get_schemas",
    "calendar_list_events",
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "calendar_get_event",
    "calendar_list_calendars",
    "calendar_find_free_slots",
]
