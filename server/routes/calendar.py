from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..models import CalendarConnectPayload, CalendarDisconnectPayload, CalendarStatusPayload
from ..services.calendar import disconnect_account, fetch_status, initiate_connect

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.post("/connect")
async def calendar_connect(payload: CalendarConnectPayload, settings: Settings = Depends(get_settings)) -> JSONResponse:
    return initiate_connect(payload, settings)


@router.post("/status")
async def calendar_status(payload: CalendarStatusPayload) -> JSONResponse:
    return fetch_status(payload)


@router.post("/disconnect")
async def calendar_disconnect(payload: CalendarDisconnectPayload) -> JSONResponse:
    return disconnect_account(payload)
