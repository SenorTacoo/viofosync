"""Camera control routes — read and (safely) adjust dashcam settings.

Backed by :mod:`viofosync_lib._control`, which decodes the camera's settings
against the vendor command map and enforces the destructive-command denylist
plus per-value validation. The camera address comes from the ``ADDRESS``
setting (same one the sync worker uses).

All blocking camera I/O runs in a threadpool so the event loop is never held.
Reads require a session; the single mutating route additionally requires CSRF.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..auth import require_csrf, require_session
from viofosync_lib import _control as control

router = APIRouter(prefix="/api/camera", tags=["camera"],
                   dependencies=[Depends(require_session)])


def _address(request: Request) -> str:
    """Resolve the configured camera address or 409 if unset."""
    snap = request.app.state.settings_provider.get()
    if not snap.address:
        raise HTTPException(
            status_code=409,
            detail="No camera ADDRESS configured (set it in Settings).")
    return snap.address


def _run(fn, *args):
    """Invoke a blocking control call, mapping control errors to HTTP."""
    try:
        return fn(*args)
    except control.DestructiveCommandError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except control.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except control.CameraUnreachable as e:
        raise HTTPException(
            status_code=502,
            detail=f"Camera unreachable: {e}") from e
    except control.ControlError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/info")
async def get_info(request: Request) -> dict:
    """Firmware, free space, card status and lens/sensor count."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.read_info, addr)


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    """Live decoded settings (cmd=3014), labelled against the command map."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.read_settings, addr)


@router.get("/settings/catalog")
async def get_catalog(request: Request) -> dict:
    """Safely-writable settings with their option lists and current values —
    the data the Camera UI renders its controls from."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.writable_catalog, addr)


class _SetRequest(BaseModel):
    value: str | int


@router.post(
    "/settings/{key}",
    dependencies=[Depends(require_csrf)],
)
async def set_setting(key: str, body: _SetRequest, request: Request) -> dict:
    """Validate and apply one setting (``key`` = CMD_KEY or numeric cmd),
    then read back to verify. Destructive ids are refused with 403."""
    addr = _address(request)
    return await run_in_threadpool(
        _run, control.set_setting, addr, key, body.value)
