"""行为热力服务：消费事件聚合数据与授权热力数据，生成热力图分析结果。"""
from __future__ import annotations

from typing import Any
from collections import defaultdict

from app.services import heatmap_share
from app.services.heatmap_share import ShareAccessError
from app.store import store


def generate_heatmap(config_id: str) -> dict[str, Any]:
    """生成行为热力图，消费事件聚合结果。"""
    config = store.find_by_field("behavior_heatmap", "config_id", config_id)
    if config is None:
        raise ValueError(f"Heatmap config not found: {config_id}")

    event_id = config["event_id"]
    spatial_dim = config["spatial_dim"]
    agg_grain = config["agg_grain"]
    time_range = config["time_range"]

    # 获取事件聚合产物作为输入
    agg_artifacts = store.list_artifacts("event_aggregate")
    related_agg = None
    for agg in agg_artifacts:
        if agg.get("event_id") == event_id:
            related_agg = agg
            break

    # 生成热力数据
    heat_data: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # 模拟热力数据点
    if spatial_dim == "page":
        pages = ["/home", "/product", "/cart", "/checkout", "/profile"]
        for page in pages:
            for hour in range(24):
                heat_data[page].append({
                    "dimension": page,
                    "time_slot": f"{hour:02d}:00",
                    "intensity": 100 + (hour * 10) + hash(page) % 50,
                })
    elif spatial_dim == "element":
        elements = ["button_buy", "button_cart", "link_detail", "banner_top", "search_box"]
        for elem in elements:
            for day in range(7):
                heat_data[elem].append({
                    "dimension": elem,
                    "time_slot": f"day_{day}",
                    "intensity": 50 + (day * 20) + hash(elem) % 30,
                })
    else:  # region
        regions = ["header", "sidebar", "main", "footer", "modal"]
        for region in regions:
            heat_data[region].append({
                "dimension": region,
                "time_slot": "all",
                "intensity": 80 + hash(region) % 100,
            })

    result = {
        "config_id": config_id,
        "event_id": event_id,
        "spatial_dim": spatial_dim,
        "agg_grain": agg_grain,
        "time_range": time_range,
        "heatmap_data": dict(heat_data),
        "max_intensity": max(
            (d["intensity"] for vals in heat_data.values() for d in vals),
            default=0,
        ),
        "min_intensity": min(
            (d["intensity"] for vals in heat_data.values() for d in vals),
            default=0,
        ),
        "source_aggregate": related_agg.get("config_id") if related_agg else None,
        "output_image": config.get("output_image", ""),
    }

    # 存储产物
    store.set_artifact("behavior_heatmap", config_id, result)

    # 更新状态
    store.update("behavior_heatmap", config["id"], {
        "status": "已完成",
        "pending": False,
    })

    return result


def get_heatmap(config_id: str) -> dict[str, Any] | None:
    """获取热力图结果。"""
    artifact = store.get_artifact("behavior_heatmap", config_id)
    if artifact:
        return artifact
    try:
        return generate_heatmap(config_id)
    except ValueError:
        return None


def generate_authorized_heatmap(config_id: str, reader_id: str, grain: str,
                                scope_id: str) -> dict[str, Any]:
    """基于授权热力数据生成行为热力：格值与色阶只取读者获准的层级。

    与 generate_heatmap 的区别：
    - 输入是 heatmap_share 从获准空间事件明细聚合出的授权热力数据；
    - 色阶上下界、总量计数随授权投影变化，不夹带无权层级；
    - 产物按读者隔离存储，读者之间互不可见。
    """
    config = store.find_by_field("behavior_heatmap", "config_id", config_id)
    if config is None:
        raise ValueError(f"Heatmap config not found: {config_id}")

    event_id = config["event_id"]
    color_scale = config.get("color_scale", "viridis")

    # 授权校验在服务层完成，无权时直接抛出，不产出任何格值
    authorized = heatmap_share.build_authorized_heatmap(
        reader_id, event_id, grain, scope_id, color_scale,
    )

    # 授权格值按网格单元重新组织为行为热力的数据结构
    heat_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in authorized["cells"]:
        heat_data[cell["cell_id"]].append({
            "dimension": cell["cell_id"],
            "time_slot": cell["time_slot"],
            "intensity": cell["intensity"],
        })

    result = {
        "config_id": config_id,
        "event_id": event_id,
        "reader_id": reader_id,
        "delivered_grain": authorized["delivered_grain"],
        "scope_id": authorized["scope_id"],
        "scope_name": authorized["scope_name"],
        "spatial_dim": config["spatial_dim"],
        "agg_grain": config["agg_grain"],
        "time_range": config["time_range"],
        "heatmap_data": dict(heat_data),
        # 色阶来源：授权热力数据（只基于可见格值）
        "color_scale": color_scale,
        "max_intensity": authorized["max_intensity"],
        "min_intensity": authorized["min_intensity"],
        "total_count": authorized["total_count"],
        "cell_count": authorized["cell_count"],
        "grants_used": authorized["grants_used"],
        "source_authorized_heatmap": True,
        "source_aggregate": None,
        "output_image": config.get("output_image", ""),
    }

    store.set_artifact(
        "behavior_heatmap_authorized",
        f"{config_id}:{reader_id}:{grain}:{scope_id}",
        result,
    )
    return result


def get_authorized_heatmap(config_id: str, reader_id: str, grain: str,
                           scope_id: str) -> dict[str, Any]:
    """读取授权热力产物；每次都按当前授权重新校验，撤回后旧缓存不再可读。"""
    # 不直接返回缓存：缓存只留作审计，许可校验必须以当前授权为准
    return generate_authorized_heatmap(config_id, reader_id, grain, scope_id)
