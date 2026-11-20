"""授权热力共享路由：许可管理、共享发布/撤回、旧链接按当前授权读取。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import heatmap_share
from app.services.heatmap_share import ShareAccessError
from app.store import store

router = APIRouter(prefix="/api/heatmap-share", tags=["heatmap_share"])


# ========== 请求契约 ==========
class GrantPayload(BaseModel):
    reader_id: str = Field(..., description="读者账号")
    reader_name: str = Field("", description="读者名称")
    grain: str = Field(..., description="许可粒度：store/region")
    scope_id: str = Field(..., description="门店或区域编号")
    event_id: str = Field("EV-002", description="关联事件")


class PublishPayload(BaseModel):
    share_name: str
    config_id: str = "BH-001"
    event_id: str = "EV-002"
    grain: str
    scope_id: str
    color_scale: str = "viridis"
    time_range: str
    issued_to: str


class AccessPayload(BaseModel):
    reader_id: str


def _raise_access(error: ShareAccessError) -> HTTPException:
    detail: dict[str, Any] = {
        "code": error.code,
        "message": str(error),
    }
    detail.update(error.extra)
    return HTTPException(status_code=error.status_code, detail=detail)


# ========== 组织归属与空间事件 ==========
@router.get("/stores")
def stores() -> dict[str, Any]:
    rows = heatmap_share.list_stores()
    return {"items": rows, "total": len(rows)}


@router.get("/events/{event_id}/details")
def event_details(event_id: str) -> dict[str, Any]:
    """空间事件明细：带门店/区域归属的获准明细，供审计与聚合核对。"""
    events = heatmap_share.generate_spatial_events(event_id)
    return {"items": events, "total": len(events), "event_id": event_id}


@router.get("/events/{event_id}/aggregate")
def event_aggregate(event_id: str) -> dict[str, Any]:
    """按门店/区域粒度重新分组后的冻结聚合（全量，仅供已认证管理端核对）。"""
    return heatmap_share.aggregate_spatial_events(event_id)


# ========== 许可管理 ==========
@router.get("/grants")
def grants(reader_id: str | None = None) -> dict[str, Any]:
    rows = heatmap_share.list_grants(reader_id)
    return {"items": rows, "total": len(rows)}


@router.post("/grants")
def create_grant(payload: GrantPayload) -> ActionResult:
    try:
        row = heatmap_share.grant_permission(
            payload.reader_id, payload.reader_name, payload.grain,
            payload.scope_id, payload.event_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ActionResult(ok=True, message="Granted", entry=row)


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str) -> ActionResult:
    try:
        row = heatmap_share.revoke_grant(grant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ActionResult(ok=True, message="Revoked", entry=row)


# ========== 授权热力（行为热力按当前许可读取的数据源） ==========
@router.get("/authorized-heatmap")
def authorized_heatmap(reader_id: str, event_id: str, grain: str, scope_id: str,
                       color_scale: str = "viridis") -> dict[str, Any]:
    try:
        return heatmap_share.build_authorized_heatmap(
            reader_id, event_id, grain, scope_id, color_scale,
        )
    except ShareAccessError as exc:
        raise _raise_access(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ========== 共享看板 ==========
@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("heatmap_share")
    start = (page - 1) * size
    return {"items": rows[start:start + size], "total": len(rows), "page": page, "size": size}


@router.get("/{share_id}")
def detail(share_id: str) -> dict[str, Any]:
    row = heatmap_share.get_share(share_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("")
def publish(payload: PublishPayload) -> ActionResult:
    try:
        row = heatmap_share.publish_share(
            payload.share_name, payload.config_id, payload.event_id, payload.grain,
            payload.scope_id, payload.color_scale, payload.time_range, payload.issued_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ActionResult(ok=True, message="Published", entry=row)


@router.post("/{share_id}/revoke")
def revoke(share_id: str) -> ActionResult:
    try:
        row = heatmap_share.revoke_share(share_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ActionResult(ok=True, message="Revoked", entry=row)


@router.post("/access/{token}")
def access(token: str, payload: AccessPayload) -> dict[str, Any]:
    """旧链接读取入口：每次访问按当前授权重新校验并投影可见格值。"""
    try:
        return heatmap_share.access_share(token, payload.reader_id)
    except ShareAccessError as exc:
        raise _raise_access(exc)
