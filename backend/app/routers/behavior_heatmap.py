"""行为热力路由：生成热力图并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult
from app.services.behavior_heatmap import (
    generate_authorized_heatmap,
    generate_heatmap,
    get_heatmap,
)
from app.services.heatmap_share import ShareAccessError
from app.store import store

router = APIRouter(prefix="/api/behavior-heatmap", tags=["behavior_heatmap"])


class AuthorizedGeneratePayload(BaseModel):
    reader_id: str = Field(..., description="读者账号，按其当前许可授权")
    grain: str = Field(..., description="统计粒度：store/region")
    scope_id: str = Field(..., description="门店或区域编号")


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("behavior_heatmap")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("behavior_heatmap")
    return {"items": rows, "total": len(rows)}
@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("behavior_heatmap", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/generate")
def generate(config_id: str) -> ActionResult:
    try:
        result = generate_heatmap(config_id)
        return ActionResult(ok=True, message="Generated", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{config_id}/authorized-generate")
def authorized_generate(config_id: str, payload: AuthorizedGeneratePayload) -> ActionResult:
    """用授权热力数据计算格值与色阶：按读者当前许可决定可用统计粒度。"""
    try:
        result = generate_authorized_heatmap(
            config_id, payload.reader_id, payload.grain, payload.scope_id,
        )
        return ActionResult(ok=True, message="AuthorizedGenerated", entry=result)
    except ShareAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail={
            "code": exc.code,
            "message": str(exc),
            **exc.extra,
        })
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_heatmap(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("behavior_heatmap", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
