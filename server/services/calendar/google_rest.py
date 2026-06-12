"""Direct Google Calendar REST API client.

Follows the Zero Calendar pattern: plain HTTP calls to googleapis.com using
the OAuth access token extracted from the Composio connected account.
Composio is still used for OAuth / connection management; this module owns
only the data-plane operations.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_BASE = "https://www.googleapis.com/calendar/v3"


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

def _call(
    method: str,
    url: str,
    token: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw_err = exc.read()
        try:
            err_body = json.loads(raw_err)
        except Exception:
            err_body = {"raw": raw_err.decode("utf-8", errors="replace")}
        raise RuntimeError(
            f"Google Calendar API error {exc.code}: {err_body}"
        ) from exc


# ---------------------------------------------------------------------------
# Public API — mirrors the execution agent tool surface
# ---------------------------------------------------------------------------

def list_events(
    token: str,
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    query: Optional[str] = None,
    max_results: Optional[int] = None,
    single_events: bool = True,
    order_by: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, str] = {}
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    if query:
        params["q"] = query
    if max_results:
        params["maxResults"] = str(max_results)
    if single_events:
        params["singleEvents"] = "true"
    if order_by:
        params["orderBy"] = order_by
    url = f"{_BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    return _call("GET", url, token)


def get_event(
    token: str,
    event_id: str,
    calendar_id: str = "primary",
) -> Dict[str, Any]:
    url = (
        f"{_BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/"
        f"events/{urllib.parse.quote(event_id, safe='')}"
    )
    return _call("GET", url, token)


def create_event(
    token: str,
    summary: str,
    start_datetime: str,
    end_datetime: str,
    calendar_id: str = "primary",
    description: Optional[str] = None,
    location: Optional[str] = None,
    timezone: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    tz = timezone or "UTC"
    body: Dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_datetime, "timeZone": tz},
        "end": {"dateTime": end_datetime, "timeZone": tz},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

    url = f"{_BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
    if send_updates:
        url += f"?{urllib.parse.urlencode({'sendUpdates': send_updates})}"
    return _call("POST", url, token, body)


def update_event(
    token: str,
    event_id: str,
    calendar_id: str = "primary",
    summary: Optional[str] = None,
    description: Optional[str] = None,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    current = get_event(token, event_id, calendar_id)
    tz = timezone or "UTC"

    if summary is not None:
        current["summary"] = summary
    if description is not None:
        current["description"] = description
    if start_datetime is not None:
        current["start"] = {"dateTime": start_datetime, "timeZone": tz}
    if end_datetime is not None:
        current["end"] = {"dateTime": end_datetime, "timeZone": tz}
    if location is not None:
        current["location"] = location
    if attendees is not None:
        current["attendees"] = [{"email": e} for e in attendees]

    url = (
        f"{_BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/"
        f"events/{urllib.parse.quote(event_id, safe='')}"
    )
    if send_updates:
        url += f"?{urllib.parse.urlencode({'sendUpdates': send_updates})}"
    return _call("PUT", url, token, current)


def delete_event(
    token: str,
    event_id: str,
    calendar_id: str = "primary",
    send_updates: Optional[str] = None,
) -> Dict[str, Any]:
    url = (
        f"{_BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/"
        f"events/{urllib.parse.quote(event_id, safe='')}"
    )
    if send_updates:
        url += f"?{urllib.parse.urlencode({'sendUpdates': send_updates})}"
    _call("DELETE", url, token)
    return {"deleted": True, "event_id": event_id}


def list_calendars(
    token: str,
    min_access_role: Optional[str] = None,
    show_hidden: Optional[bool] = None,
) -> Dict[str, Any]:
    params: Dict[str, str] = {}
    if min_access_role:
        params["minAccessRole"] = min_access_role
    if show_hidden is not None:
        params["showHidden"] = "true" if show_hidden else "false"
    url = f"{_BASE}/users/me/calendarList"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    return _call("GET", url, token)


def find_free_slots(
    token: str,
    time_min: str,
    time_max: str,
    calendar_ids: Optional[List[str]] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    ids = calendar_ids or ["primary"]
    body: Dict[str, Any] = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": cid} for cid in ids],
    }
    if timezone:
        body["timeZone"] = timezone
    return _call("POST", f"{_BASE}/freeBusy", token, body)


__all__ = [
    "list_events",
    "get_event",
    "create_event",
    "update_event",
    "delete_event",
    "list_calendars",
    "find_free_slots",
]
