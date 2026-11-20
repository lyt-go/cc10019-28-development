"""授权热力共享服务。

数据流：

    空间事件明细（带门店/区域归属）
        → 按获准的门店或区域粒度重新分组聚合
        → 授权热力数据（格值 + 仅基于可见格值重算的色阶/计数）
        → 行为热力消费授权热力数据计算格值与色阶
        → 共享看板按读者当前许可投影，旧链接在许可变化后即时失效或降级

关键安全约束：
1. 许可在每次访问时按“当前授权”重新校验，门店许可撤回后旧链接读不到门店格子；
2. 区域读者只能拿到区域口径，返回结构不含任何门店标识，色阶 min/max、总量计数
   只基于区域格值计算，无法反推出不允许单独查看的门店格值；
3. 门店级共享与当前授权冲突时（只剩区域许可），链接不静默降级，返回冲突错误，
   区域汇总须走区域级共享交付。
"""
from __future__ import annotations

import secrets
from collections import defaultdict
from typing import Any

from app.store import store

# 热力网格：固定 6 个格子 × 24 小时槽，模拟门店内的空间网格
GRID_CELLS = [f"cell-{i:02d}" for i in range(1, 7)]
TIME_SLOTS = [f"{hour:02d}:00" for hour in range(24)]

GRANT_ACTIVE = "已生效"
GRANT_REVOKED = "已撤回"
SHARE_PUBLISHED = "已发布"
SHARE_WITHDRAWN = "已撤回"

ARTIFACT_SNAPSHOT = "heatmap_share_snapshot"


class ShareAccessError(PermissionError):
    """共享访问被拒绝；携带 HTTP 状态码与机器可读错误码。"""

    def __init__(self, status_code: int, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.extra = extra or {}


# ========== 组织归属 ==========
def list_stores() -> list[dict[str, Any]]:
    return store.rows("heatmap_store")


def get_store(store_id: str) -> dict[str, Any] | None:
    return store.find_by_field("heatmap_store", "store_id", store_id)


def stores_of_region(region_id: str) -> list[dict[str, Any]]:
    return store.find_all_by_field("heatmap_store", "region_id", region_id)


# ========== 空间事件明细 ==========
def _cell_event_count(store_index: int, cell_index: int, hour: int) -> int:
    """确定性的格子事件量，保证同一时间窗内可重复比较。"""
    base = (store_index + 1) * 7 + (cell_index + 1) * 3
    wave = (hour * 5 + cell_index * 2 + store_index) % 11
    return base + wave


def generate_spatial_events(event_id: str) -> list[dict[str, Any]]:
    """生成带门店/区域归属的空间事件明细（授权聚合的唯一来源）。"""
    events: list[dict[str, Any]] = []
    for store_index, store_row in enumerate(list_stores()):
        for cell_index, cell_id in enumerate(GRID_CELLS):
            for hour in range(24):
                count = _cell_event_count(store_index, cell_index, hour)
                for seq in range(count):
                    events.append({
                        "event_id": event_id,
                        "store_id": store_row["store_id"],
                        "store_name": store_row["store_name"],
                        "region_id": store_row["region_id"],
                        "region_name": store_row["region_name"],
                        "cell_id": cell_id,
                        "hour": hour,
                        "time_slot": TIME_SLOTS[hour],
                        "user_id": f"U{store_index:02d}{cell_index:02d}{hour:02d}{seq:03d}",
                    })
    return events


def aggregate_spatial_events(event_id: str) -> dict[str, Any]:
    """把空间事件明细按门店粒度与区域粒度重新分组聚合。

    返回冻结聚合：门店格值与区域格值都在此算出，访问时再按许可投影，
    服务端绝不把读者无权查看的层级写入响应。
    """
    events = generate_spatial_events(event_id)

    store_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    region_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for event in events:
        store_counts[(event["store_id"], event["cell_id"], event["time_slot"])] += 1
        region_counts[(event["region_id"], event["cell_id"], event["time_slot"])] += 1

    store_cells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (store_id, cell_id, time_slot), intensity in sorted(store_counts.items()):
        store_cells[store_id].append({
            "cell_id": cell_id,
            "time_slot": time_slot,
            "intensity": intensity,
        })

    region_cells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (region_id, cell_id, time_slot), intensity in sorted(region_counts.items()):
        region_cells[region_id].append({
            "cell_id": cell_id,
            "time_slot": time_slot,
            "intensity": intensity,
        })

    return {
        "event_id": event_id,
        "store_cells": {k: list(v) for k, v in store_cells.items()},
        "region_cells": {k: list(v) for k, v in region_cells.items()},
        "event_total": len(events),
    }


# ========== 许可管理 ==========
def list_grants(reader_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("heatmap_grant")
    if reader_id:
        rows = [row for row in rows if row["reader_id"] == reader_id]
    return rows


def effective_grants(reader_id: str, event_id: str) -> list[dict[str, Any]]:
    """读者当前对某事件仍有效的许可。"""
    return [
        row for row in store.rows("heatmap_grant")
        if row["reader_id"] == reader_id
        and row["event_id"] == event_id
        and row["status"] == GRANT_ACTIVE
    ]


def grant_permission(reader_id: str, reader_name: str, grain: str, scope_id: str,
                     event_id: str) -> dict[str, Any]:
    """登记一条读取许可（同读者+范围已存在有效许可时直接返回）。"""
    if grain not in ("store", "region"):
        raise ValueError("grain 只能是 store 或 region")
    if grain == "store" and get_store(scope_id) is None:
        raise ValueError(f"门店不存在: {scope_id}")
    if grain == "region" and not stores_of_region(scope_id):
        raise ValueError(f"区域不存在: {scope_id}")

    for row in store.rows("heatmap_grant"):
        if (row["reader_id"] == reader_id and row["grain"] == grain
                and row["scope_id"] == scope_id and row["event_id"] == event_id
                and row["status"] == GRANT_ACTIVE):
            return row

    scope_name = ""
    if grain == "store":
        scope_name = get_store(scope_id)["store_name"]
    else:
        scope_name = stores_of_region(scope_id)[0]["region_name"]

    row = {
        "status": GRANT_ACTIVE,
        "pending": False,
        "abnormal": False,
        "grant_id": f"HG-{store._next_ids.get('heatmap_grant', 1):03d}",
        "reader_id": reader_id,
        "reader_name": reader_name,
        "grain": grain,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "event_id": event_id,
    }
    return store.insert("heatmap_grant", row)


def revoke_grant(grant_id: str) -> dict[str, Any]:
    """撤回一条许可；撤回后所有依赖它的共享链接立即失去对应读取能力。"""
    row = store.find_by_field("heatmap_grant", "grant_id", grant_id)
    if row is None:
        raise ValueError(f"许可不存在: {grant_id}")
    row["status"] = GRANT_REVOKED
    row["pending"] = False
    return row


# ========== 授权热力构建 ==========
def _color_bounds(cells: list[dict[str, Any]]) -> tuple[int, int]:
    """色阶上下界只基于本次实际交付的格值计算，避免泄露无权层级。"""
    if not cells:
        return 0, 0
    intensities = [cell["intensity"] for cell in cells]
    return min(intensities), max(intensities)


def build_authorized_heatmap(reader_id: str, event_id: str, grain: str,
                             scope_id: str, color_scale: str) -> dict[str, Any]:
    """按读者当前许可构建授权热力数据。

    - grain=store：需持有该门店的门店级许可；
    - grain=region：需持有该区域的区域级许可，只返回区域格值。
    无权时抛 ShareAccessError，不返回任何格值。
    """
    grants = effective_grants(reader_id, event_id)
    aggregate = aggregate_spatial_events(event_id)

    if grain == "store":
        allowed = [g for g in grants if g["grain"] == "store" and g["scope_id"] == scope_id]
        if not allowed:
            raise ShareAccessError(403, "STORE_PERMISSION_REQUIRED",
                                   f"读者 {reader_id} 缺少门店 {scope_id} 的有效读取许可")
        cells = aggregate["store_cells"].get(scope_id, [])
        scope_name = get_store(scope_id)["store_name"]
    elif grain == "region":
        allowed = [g for g in grants if g["grain"] == "region" and g["scope_id"] == scope_id]
        if not allowed:
            # 门店级许可不能读区域口径：区域总量会反推出其他门店格值
            raise ShareAccessError(403, "REGION_PERMISSION_REQUIRED",
                                   f"读者 {reader_id} 缺少区域 {scope_id} 的区域级读取许可")
        cells = aggregate["region_cells"].get(scope_id, [])
        scope_name = stores_of_region(scope_id)[0]["region_name"]
    else:
        raise ValueError(f"未知粒度: {grain}")

    min_intensity, max_intensity = _color_bounds(cells)
    return {
        "event_id": event_id,
        "delivered_grain": grain,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "color_scale": color_scale,
        "cells": cells,
        "cell_count": len(cells),
        "total_count": sum(cell["intensity"] for cell in cells),
        "min_intensity": min_intensity,
        "max_intensity": max_intensity,
        "grants_used": [g["grant_id"] for g in allowed],
        "source": "authorized_spatial_event_aggregate",
    }


# ========== 共享看板 ==========
def list_shares() -> list[dict[str, Any]]:
    return store.rows("heatmap_share")


def get_share(share_id: str) -> dict[str, Any] | None:
    return store.find_by_field("heatmap_share", "share_id", share_id)


def _find_share_by_token(token: str) -> dict[str, Any] | None:
    return store.find_by_field("heatmap_share", "token", token)


def _ensure_snapshot(share: dict[str, Any]) -> dict[str, Any]:
    """共享发布时冻结聚合快照；示例数据惰性补齐，保证旧链接读到的是同一次共享。"""
    snapshot = store.get_artifact(ARTIFACT_SNAPSHOT, share["share_id"])
    if snapshot is None:
        snapshot = aggregate_spatial_events(share["event_id"])
        snapshot["frozen_for"] = share["share_id"]
        snapshot["time_range"] = share["time_range"]
        store.set_artifact(ARTIFACT_SNAPSHOT, share["share_id"], snapshot)
    return snapshot


def publish_share(share_name: str, config_id: str, event_id: str, grain: str,
                  scope_id: str, color_scale: str, time_range: str,
                  issued_to: str) -> dict[str, Any]:
    """发布一次共享：先校验发布对象存在，再冻结快照并签发链接。"""
    if grain not in ("store", "region"):
        raise ValueError("grain 只能是 store 或 region")
    if store.find_by_field("behavior_heatmap", "config_id", config_id) is None:
        raise ValueError(f"热力配置不存在: {config_id}")
    if grain == "store":
        store_row = get_store(scope_id)
        if store_row is None:
            raise ValueError(f"门店不存在: {scope_id}")
        scope_name = store_row["store_name"]
    else:
        region_stores = stores_of_region(scope_id)
        if not region_stores:
            raise ValueError(f"区域不存在: {scope_id}")
        scope_name = region_stores[0]["region_name"]

    next_id = store._next_ids.get("heatmap_share", 1)
    share = {
        "status": SHARE_PUBLISHED,
        "pending": False,
        "abnormal": False,
        "share_id": f"HS-{next_id:03d}",
        "token": secrets.token_hex(12),
        "share_name": share_name,
        "config_id": config_id,
        "event_id": event_id,
        "grain": grain,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "color_scale": color_scale,
        "time_range": time_range,
        "issued_to": issued_to,
    }
    inserted = store.insert("heatmap_share", share)
    _ensure_snapshot(inserted)
    return inserted


def revoke_share(share_id: str) -> dict[str, Any]:
    """撤回整个共享，链接随后返回失效错误。"""
    share = get_share(share_id)
    if share is None:
        raise ValueError(f"共享不存在: {share_id}")
    share["status"] = SHARE_WITHDRAWN
    share["pending"] = False
    return share


def _project_snapshot(share: dict[str, Any], grants: list[dict[str, Any]]) -> dict[str, Any]:
    """从冻结快照中按当前许可投影可见格值，并基于可见格值重算色阶与计数。"""
    snapshot = _ensure_snapshot(share)
    grain = share["grain"]
    scope_id = share["scope_id"]

    if grain == "store":
        cells = [dict(cell) for cell in snapshot["store_cells"].get(scope_id, [])]
    else:
        # 区域投影只取区域格值，快照中的门店层级不会进入响应
        cells = [dict(cell) for cell in snapshot["region_cells"].get(scope_id, [])]

    min_intensity, max_intensity = _color_bounds(cells)
    return {
        "share_id": share["share_id"],
        "token": share["token"],
        "share_name": share["share_name"],
        "config_id": share["config_id"],
        "event_id": share["event_id"],
        "delivered_grain": grain,
        "scope_id": scope_id,
        "scope_name": share["scope_name"],
        "color_scale": share["color_scale"],
        "time_range": share["time_range"],
        "cells": cells,
        "cell_count": len(cells),
        "total_count": sum(cell["intensity"] for cell in cells),
        "min_intensity": min_intensity,
        "max_intensity": max_intensity,
        "grants_used": [g["grant_id"] for g in grants],
        "source": "authorized_spatial_event_aggregate",
    }


def access_share(token: str, reader_id: str) -> dict[str, Any]:
    """读者用旧链接打开共享，按当前授权决定可交付的统计粒度。

    判定顺序：
    1. 链接不存在 → 404 失效链接；
    2. 共享已撤回 → 410 失效链接；
    3. 门店级共享：
       - 仍有该门店许可 → 交付门店格值；
       - 门店许可撤回但持有归属区域的区域许可 → 409 授权冲突，
         说明旧链接不能继续读取门店格子，区域复核须用区域级共享；
       - 其他 → 403；
    4. 区域级共享：持有区域许可才交付区域口径，门店许可不足 → 403。
    """
    share = _find_share_by_token(token)
    if share is None:
        raise ShareAccessError(404, "SHARE_LINK_INVALID", "共享链接不存在或已失效")
    if share["status"] == SHARE_WITHDRAWN:
        raise ShareAccessError(410, "SHARE_WITHDRAWN",
                               f"共享 {share['share_id']} 已撤回，链接失效",
                               {"share_id": share["share_id"]})

    grants = effective_grants(reader_id, share["event_id"])
    grain = share["grain"]
    scope_id = share["scope_id"]

    if grain == "store":
        store_grants = [g for g in grants if g["grain"] == "store" and g["scope_id"] == scope_id]
        if store_grants:
            return _project_snapshot(share, store_grants)

        store_row = get_store(scope_id)
        region_id = store_row["region_id"] if store_row else ""
        region_grants = [g for g in grants if g["grain"] == "region" and g["scope_id"] == region_id]
        if region_grants:
            raise ShareAccessError(
                409, "STORE_SCOPE_CONFLICT",
                "门店级访问已撤回：当前只有区域许可，旧链接不能继续读取门店格子；"
                "区域复核请使用区域级共享链接。",
                {
                    "share_id": share["share_id"],
                    "requested_grain": "store",
                    "available_grain": "region",
                    "region_scope_id": region_id,
                    "region_share_required": True,
                },
            )
        raise ShareAccessError(403, "STORE_PERMISSION_REQUIRED",
                               f"读者 {reader_id} 无权读取门店 {scope_id} 的热力格子",
                               {"share_id": share["share_id"]})

    # 区域级共享
    region_grants = [g for g in grants if g["grain"] == "region" and g["scope_id"] == scope_id]
    if not region_grants:
        raise ShareAccessError(403, "REGION_PERMISSION_REQUIRED",
                               f"读者 {reader_id} 缺少区域 {scope_id} 的区域级读取许可；"
                               "门店许可不能交付区域口径。",
                               {"share_id": share["share_id"], "requested_grain": "region"})
    return _project_snapshot(share, region_grants)
