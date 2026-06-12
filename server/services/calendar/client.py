from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import status
from fastapi.responses import JSONResponse

from ...config import Settings, get_settings
from ...logging_config import logger
from ...models import CalendarConnectPayload, CalendarDisconnectPayload, CalendarStatusPayload
from ...utils import error_response

_TOOLKIT_SLUG = "GOOGLECALENDAR"

_DATA_FILE = (
    __import__("pathlib").Path(__file__).resolve().parent.parent.parent.parent
    / "data" / "calendar" / "user.json"
)

_CLIENT_LOCK = threading.Lock()
_CLIENT: Optional[Any] = None

_ACTIVE_USER_ID_LOCK = threading.Lock()


def _normalized(value: Optional[str]) -> str:
    return (value or "").strip()


def _load_user_id() -> Optional[str]:
    try:
        if _DATA_FILE.exists():
            return json.loads(_DATA_FILE.read_text(encoding="utf-8")).get("user_id") or None
    except Exception:
        pass
    return None


def _load_connection() -> tuple[Optional[str], Optional[str]]:
    """Return (user_id, email) from disk, or (None, None) if missing."""
    try:
        if _DATA_FILE.exists():
            data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            return data.get("user_id") or None, data.get("email") or None
    except Exception:
        pass
    return None, None


def _save_connection(user_id: Optional[str], email: Optional[str]) -> None:
    try:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DATA_FILE.write_text(
            json.dumps({"user_id": user_id or "", "email": email or ""}),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"Failed to persist calendar connection: {exc}")


def _extract_email(obj: Any) -> Optional[str]:
    if obj is None:
        return None

    # Composio stores the account email in account.deprecated.labels (List[str])
    try:
        deprecated = getattr(obj, "deprecated", None)
        if deprecated is not None:
            labels = getattr(deprecated, "labels", None) or []
            for label in labels:
                if isinstance(label, str) and "@" in label:
                    return label
    except Exception:
        pass

    # Fallback: common direct attributes and dict keys
    for key in ("email", "email_address", "emailAddress", "user_email", "account_email"):
        try:
            val = getattr(obj, key, None)
            if isinstance(val, str) and "@" in val:
                return val
        except Exception:
            pass
        if isinstance(obj, dict):
            val = obj.get(key)
            if isinstance(val, str) and "@" in val:
                return val

    # Fallback: nested dict paths (data.email, params.email, profile.email, …)
    if isinstance(obj, dict):
        for path in (("profile", "email"), ("data", "email"), ("params", "email"), ("user", "email")):
            cur: Any = obj
            for seg in path:
                cur = cur.get(seg) if isinstance(cur, dict) else None
            if isinstance(cur, str) and "@" in cur:
                return cur

    return None


# Initialise from disk so the connection survives server restarts.
_disk_user_id, _disk_email = _load_connection()
_ACTIVE_USER_ID: Optional[str] = _disk_user_id
_ACTIVE_EMAIL: Optional[str] = _disk_email
_ACTIVE_EMAIL_LOCK = threading.Lock()


def _set_active_calendar_connection(user_id: Optional[str], email: Optional[str] = None) -> None:
    uid = _normalized(user_id) or None
    eml = _normalized(email) or None
    with _ACTIVE_USER_ID_LOCK:
        global _ACTIVE_USER_ID
        _ACTIVE_USER_ID = uid
    with _ACTIVE_EMAIL_LOCK:
        global _ACTIVE_EMAIL
        _ACTIVE_EMAIL = eml
    if uid:
        _save_connection(uid, eml)


# Keep old name as alias so existing callers in initiate_connect/disconnect still work.
def _set_active_calendar_user_id(user_id: Optional[str]) -> None:
    _set_active_calendar_connection(user_id)


def get_active_calendar_user_id() -> Optional[str]:
    with _ACTIVE_USER_ID_LOCK:
        return _ACTIVE_USER_ID


def get_active_calendar_email() -> Optional[str]:
    with _ACTIVE_EMAIL_LOCK:
        return _ACTIVE_EMAIL


def _get_composio_client(settings: Optional[Settings] = None):
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    with _CLIENT_LOCK:
        if _CLIENT is None:
            from composio import Composio  # type: ignore

            resolved_settings = settings or get_settings()
            api_key = resolved_settings.composio_api_key
            try:
                _CLIENT = Composio(api_key=api_key) if api_key else Composio()
            except TypeError as exc:
                if api_key:
                    raise RuntimeError(
                        "Installed Composio SDK does not accept the api_key argument; upgrade the SDK."
                    ) from exc
                _CLIENT = Composio()
    return _CLIENT


def initiate_connect(payload: CalendarConnectPayload, settings: Settings) -> JSONResponse:
    auth_config_id = payload.auth_config_id or settings.composio_calendar_auth_config_id or ""
    if not auth_config_id:
        return error_response(
            "Missing auth_config_id. Set COMPOSIO_CALENDAR_AUTH_CONFIG_ID or pass auth_config_id.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_id = payload.user_id or f"web-cal-{os.getpid()}"
    _set_active_calendar_user_id(user_id)
    try:
        client = _get_composio_client(settings)
        req = client.connected_accounts.initiate(user_id=user_id, auth_config_id=auth_config_id, allow_multiple=True)
        data = {
            "ok": True,
            "redirect_url": getattr(req, "redirect_url", None) or getattr(req, "redirectUrl", None),
            "connection_request_id": getattr(req, "id", None),
            "user_id": user_id,
        }
        return JSONResponse(data)
    except Exception as exc:
        logger.exception("calendar connect failed", extra={"user_id": user_id})
        return error_response(
            "Failed to initiate Calendar connect",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


def fetch_status(payload: CalendarStatusPayload) -> JSONResponse:
    connection_request_id = _normalized(payload.connection_request_id)
    user_id = _normalized(payload.user_id)

    if not connection_request_id and not user_id:
        return error_response(
            "Missing connection_request_id or user_id",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        client = _get_composio_client()
        account: Any = None
        if connection_request_id:
            try:
                account = client.connected_accounts.wait_for_connection(connection_request_id, timeout=2.0)
            except Exception:
                try:
                    account = client.connected_accounts.get(connection_request_id)
                except Exception:
                    account = None
        if account is None and user_id:
            try:
                items = client.connected_accounts.list(
                    user_ids=[user_id], toolkit_slugs=[_TOOLKIT_SLUG], statuses=["ACTIVE"]
                )
                data = getattr(items, "data", None)
                if data is None and isinstance(items, dict):
                    data = items.get("data")
                if data:
                    account = data[0]
            except Exception:
                account = None

        status_value = None
        connected = False
        account_user_id = None
        email = None

        if account is not None:
            status_value = getattr(account, "status", None) or (account.get("status") if isinstance(account, dict) else None)
            normalized_status = (status_value or "").upper()
            connected = normalized_status in {"CONNECTED", "SUCCESS", "SUCCESSFUL", "ACTIVE", "COMPLETED"}
            email = _extract_email(account)
            if hasattr(account, "user_id"):
                account_user_id = getattr(account, "user_id", None)
            elif isinstance(account, dict):
                account_user_id = account.get("user_id")

        if not user_id and account_user_id:
            user_id = _normalized(account_user_id)

        if connected:
            _set_active_calendar_connection(user_id, email)
        else:
            _set_active_calendar_user_id(None)

        return JSONResponse(
            {
                "ok": True,
                "connected": bool(connected),
                "status": status_value or "UNKNOWN",
                "email": email,
                "user_id": user_id,
            }
        )
    except Exception as exc:
        logger.exception(
            "calendar status failed",
            extra={"connection_request_id": connection_request_id, "user_id": user_id},
        )
        return error_response(
            "Failed to fetch Calendar connection status",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


def disconnect_account(payload: CalendarDisconnectPayload) -> JSONResponse:
    connection_id = _normalized(payload.connection_id) or _normalized(payload.connection_request_id)
    user_id = _normalized(payload.user_id)

    if not connection_id and not user_id:
        return error_response(
            "Missing connection_id or user_id",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        client = _get_composio_client()
    except Exception as exc:
        logger.exception("calendar disconnect failed: client init", extra={"user_id": user_id})
        return error_response(
            "Failed to disconnect Calendar",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    removed_ids: list[str] = []
    errors: list[str] = []
    affected_user_ids: set[str] = set()

    def _delete_connection(identifier: str) -> None:
        sanitized_id = _normalized(identifier)
        if not sanitized_id:
            return
        try:
            connection = client.connected_accounts.get(sanitized_id)
        except Exception:
            connection = None
        try:
            client.connected_accounts.delete(sanitized_id)
            removed_ids.append(sanitized_id)
            if connection is not None:
                if hasattr(connection, "user_id"):
                    affected_user_ids.add(_normalized(getattr(connection, "user_id", None)))
                elif isinstance(connection, dict):
                    affected_user_ids.add(_normalized(connection.get("user_id")))
        except Exception as exc:
            logger.exception("Failed to remove Calendar connection", extra={"connection_id": sanitized_id})
            errors.append(str(exc))

    if connection_id:
        _delete_connection(connection_id)
    else:
        try:
            items = client.connected_accounts.list(user_ids=[user_id], toolkit_slugs=[_TOOLKIT_SLUG])
            data = getattr(items, "data", None)
            if data is None and isinstance(items, dict):
                data = items.get("data")
        except Exception as exc:
            logger.exception("Failed to list Calendar connections", extra={"user_id": user_id})
            return error_response(
                "Failed to disconnect Calendar",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            )
        if data:
            for entry in data:
                candidate = None
                candidate_user_id = None
                if hasattr(entry, "id"):
                    candidate = getattr(entry, "id", None)
                    candidate_user_id = getattr(entry, "user_id", None)
                if candidate is None and isinstance(entry, dict):
                    candidate = entry.get("id")
                    candidate_user_id = entry.get("user_id")
                if candidate:
                    if candidate_user_id:
                        affected_user_ids.add(_normalized(candidate_user_id))
                    _delete_connection(candidate)

    if user_id:
        affected_user_ids.add(user_id)

    for uid in list(affected_user_ids):
        _invalidate_token_cache(uid or None)
        if uid and get_active_calendar_user_id() == uid:
            _set_active_calendar_user_id(None)

    if errors and not removed_ids:
        return error_response(
            "Failed to disconnect Calendar",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="; ".join(errors),
        )

    result_payload: Dict[str, Any] = {
        "ok": True,
        "disconnected": bool(removed_ids),
        "removed_connection_ids": removed_ids,
    }
    if not removed_ids:
        result_payload["message"] = "No Calendar connection found"
    if errors:
        result_payload["warnings"] = errors
    return JSONResponse(result_payload)


_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}  # user_id -> (access_token, expiry_epoch)
_TOKEN_CACHE_LOCK = threading.Lock()


def get_calendar_access_token() -> Optional[str]:
    """Return a live Google OAuth access token for the active calendar user.

    Extracts the token from the Composio connected account (``state.val.access_token``),
    caching it in memory until 60 s before it expires so repeated tool calls within
    a single agent turn don't each round-trip to Composio.
    """
    user_id = get_active_calendar_user_id()
    if not user_id:
        return None

    now = time.monotonic()
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(user_id)
        if cached and now < cached[1] - 60:
            return cached[0]

    try:
        client = _get_composio_client()
        items = client.connected_accounts.list(
            user_ids=[user_id], toolkit_slugs=[_TOOLKIT_SLUG], statuses=["ACTIVE"]
        )
        data = getattr(items, "data", None)
        if data is None and isinstance(items, dict):
            data = items.get("data")
        if not data:
            return None

        account = data[0]
        state = getattr(account, "state", None)
        val = getattr(state, "val", None) if state is not None else None
        token: Optional[str] = getattr(val, "access_token", None) if val is not None else None
        if not token:
            return None

        try:
            raw_expiry = getattr(val, "expires_in", None)
            expiry_seconds = float(raw_expiry) if raw_expiry else 3600.0
        except (TypeError, ValueError):
            expiry_seconds = 3600.0

        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[user_id] = (token, now + expiry_seconds)

        return token
    except Exception as exc:
        logger.warning("Failed to get calendar access token", extra={"error": str(exc)})
        return None


def _invalidate_token_cache(user_id: Optional[str] = None) -> None:
    with _TOKEN_CACHE_LOCK:
        if user_id:
            _TOKEN_CACHE.pop(user_id, None)
        else:
            _TOKEN_CACHE.clear()


def _normalize_tool_response(result: Any) -> Dict[str, Any]:
    payload_dict: Optional[Dict[str, Any]] = None
    try:
        if hasattr(result, "model_dump"):
            payload_dict = result.model_dump()
        elif hasattr(result, "dict"):
            payload_dict = result.dict()
    except Exception:
        payload_dict = None

    if payload_dict is None:
        try:
            if hasattr(result, "model_dump_json"):
                payload_dict = json.loads(result.model_dump_json())
        except Exception:
            payload_dict = None

    if payload_dict is None:
        if isinstance(result, dict):
            payload_dict = result
        elif isinstance(result, list):
            payload_dict = {"items": result}
        else:
            payload_dict = {"repr": str(result)}

    return payload_dict


def execute_calendar_tool(
    tool_name: str,
    composio_user_id: str,
    *,
    arguments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prepared: Dict[str, Any] = {}
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if value is not None:
                prepared[key] = value

    try:
        client = _get_composio_client()
        result = client.client.tools.execute(
            tool_name,
            user_id=composio_user_id,
            arguments=prepared,
        )
        return _normalize_tool_response(result)
    except Exception as exc:
        logger.exception(
            "calendar tool execution failed",
            extra={"tool": tool_name, "user_id": composio_user_id},
        )
        raise RuntimeError(f"{tool_name} invocation failed: {exc}") from exc
