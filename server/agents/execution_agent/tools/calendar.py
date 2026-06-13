"""Google Calendar tool schemas and actions for the execution agent.

Execution: Composio tool execution pipeline (handles OAuth token refresh).
Auth: Composio OAuth (connect / disconnect / status) — unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Union

from server.logging_config import logger
from server.services.calendar import get_active_calendar_user_id
from server.services.calendar.client import execute_calendar_tool

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
# Execution helpers
# ---------------------------------------------------------------------------

def _user_id_or_error() -> Union[str, Dict[str, Any]]:
    user_id = get_active_calendar_user_id()
    if not user_id:
        return {"error": "Google Calendar not connected. Please connect Calendar in settings first."}
    return user_id


def _exec(slug: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    uid = _user_id_or_error()
    if isinstance(uid, dict):
        return uid
    try:
        result = execute_calendar_tool(slug, uid, arguments=arguments)
        logger.info("calendar_exec %s OK: %s", slug, str(result)[:200])
        return result
    except RuntimeError as exc:
        logger.error("calendar_exec %s FAILED: %s", slug, exc)
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
    # GOOGLECALENDAR_EVENTS_LIST uses camelCase parameter names
    return _exec("GOOGLECALENDAR_EVENTS_LIST", {
        "calendarId": calendar_id or "primary",
        "timeMin": time_min,
        "timeMax": time_max,
        "q": query,
        "maxResults": max_results,
        "singleEvents": True if single_events is None else single_events,
        "orderBy": order_by,
    })


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
    return _exec("GOOGLECALENDAR_CREATE_EVENT", {
        "summary": summary,
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "calendar_id": calendar_id or "primary",
        "description": description,
        "timezone": timezone,
        "location": location,
        "attendees": attendees,
        "send_updates": send_updates,
    })


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
    # GOOGLECALENDAR_PATCH_EVENT only requires event_id + calendar_id; uses start_time/end_time
    return _exec("GOOGLECALENDAR_PATCH_EVENT", {
        "event_id": event_id,
        "calendar_id": calendar_id or "primary",
        "summary": summary,
        "description": description,
        "start_time": start_datetime,
        "end_time": end_datetime,
        "timezone": timezone,
        "location": location,
        "attendees": attendees,
        "send_updates": send_updates,
    })


def calendar_delete_event(
    event_id: str,
    calendar_id: Optional[str] = None,
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    return _exec("GOOGLECALENDAR_DELETE_EVENT", {
        "event_id": event_id,
        "calendar_id": calendar_id or "primary",
        "send_updates": send_updates,
    })


def calendar_get_event(
    event_id: str,
    calendar_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _exec("GOOGLECALENDAR_EVENTS_GET", {
        "event_id": event_id,
        "calendar_id": calendar_id or "primary",
    })


def calendar_list_calendars(
    min_access_role: Optional[str] = None,
    show_hidden: Optional[bool] = None,
) -> Dict[str, Any]:
    return _exec("GOOGLECALENDAR_LIST_CALENDARS", {
        "min_access_role": min_access_role,
        "show_hidden": show_hidden,
    })


def calendar_find_free_slots(
    time_min: str,
    time_max: str,
    calendar_ids: Optional[List[str]] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    return _exec("GOOGLECALENDAR_FIND_FREE_SLOTS", {
        "time_min": time_min,
        "time_max": time_max,
        "items": calendar_ids or ["primary"],
        "timezone": timezone,
    })


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
