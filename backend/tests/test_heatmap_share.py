"""授权热力共享测试：空间事件聚合、许可撤回、旧链接失效与区域口径防泄露。

覆盖任务场景：
- 门店许可撤回后，旧链接不能继续读取门店格子；
- 区域读者只获得区域口径，色阶与计数不泄露门店格值；
- 门店级共享与当前授权冲突时，区域运营用原链接做区域比较会被拒绝，
  区域复核须由区域级共享交付；
- 行为热力用授权热力数据计算格值与色阶。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import heatmap_share
from app.services.behavior_heatmap import generate_authorized_heatmap
from app.services.heatmap_share import ShareAccessError
from app.store import store


STORE_READER = "reader-store-001"
REGION_EAST_READER = "reader-region-east"
REGION_SOUTH_READER = "reader-region-south"
STORE_1 = "STORE-001"
STORE_2 = "STORE-002"
REGION_EAST = "REGION-EAST"
REGION_SOUTH = "REGION-SOUTH"
EVENT = "EV-002"
STORE_TOKEN = "tok-store-001-ev002"
REGION_TOKEN = "tok-region-east-ev002"


class AuthorizedHeatmapTest(unittest.TestCase):
    def setUp(self) -> None:
        # 回到种子初始状态：清掉运行期产物，并重载许可/共享两张表
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        from app.seed import SEED_ROWS
        for module in ("heatmap_grant", "heatmap_share"):
            store._tables[module] = [dict(r) for r in SEED_ROWS[module]]
            store._next_ids[module] = max(
                int(r["id"]) for r in store.rows(module)) + 1

    # ========== 1. 空间事件明细与两级聚合 ==========
    def test_spatial_events_carry_store_and_region(self) -> None:
        events = heatmap_share.generate_spatial_events(EVENT)
        self.assertGreater(len(events), 0)
        sample = events[0]
        for key in ("store_id", "region_id", "cell_id", "time_slot", "user_id"):
            self.assertIn(key, sample)
        # 所有门店都有明细
        store_ids = {e["store_id"] for e in events}
        self.assertEqual(store_ids, {STORE_1, STORE_2, "STORE-003", "STORE-004"})

    def test_region_cells_equal_sum_of_member_store_cells(self) -> None:
        """区域口径必须等于其下属门店格值之和（同一次共享交付完整区域汇总）。"""
        agg = heatmap_share.aggregate_spatial_events(EVENT)
        east_stores = [r["store_id"] for r in heatmap_share.stores_of_region(REGION_EAST)]

        store_index = {}
        for store_id in east_stores:
            for cell in agg["store_cells"][store_id]:
                store_index[(cell["cell_id"], cell["time_slot"])] = (
                    store_index.get((cell["cell_id"], cell["time_slot"]), 0)
                    + cell["intensity"]
                )
        for cell in agg["region_cells"][REGION_EAST]:
            key = (cell["cell_id"], cell["time_slot"])
            self.assertEqual(cell["intensity"], store_index[key])

    # ========== 2. 门店账号：许可有效时可读取门店格子 ==========
    def test_store_reader_gets_store_cells(self) -> None:
        data = heatmap_share.access_share(STORE_TOKEN, STORE_READER)
        self.assertEqual(data["delivered_grain"], "store")
        self.assertEqual(data["scope_id"], STORE_1)
        self.assertGreater(data["cell_count"], 0)
        self.assertEqual(
            data["max_intensity"], max(c["intensity"] for c in data["cells"]))
        self.assertEqual(
            data["min_intensity"], min(c["intensity"] for c in data["cells"]))
        # 门店口径不出现区域字段/其他门店
        for cell in data["cells"]:
            self.assertNotIn("store_id", cell)
            self.assertNotIn("region_id", cell)

    # ========== 3. 撤回门店许可：旧链接立即失效，且不静默降级 ==========
    def test_revoked_store_link_blocks_store_cells(self) -> None:
        grant = next(
            g for g in heatmap_share.list_grants(STORE_READER)
            if g["scope_id"] == STORE_1 and g["grain"] == "store"
        )
        heatmap_share.revoke_grant(grant["grant_id"])

        # 撤回后门店读者（无任何其他许可）打开旧链接 → 403
        with self.assertRaises(ShareAccessError) as ctx:
            heatmap_share.access_share(STORE_TOKEN, STORE_READER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "STORE_PERMISSION_REQUIRED")

    def test_conflict_when_only_region_permission_remains(self) -> None:
        """门店许可收回至区域：同一门店链接 + 区域许可 → 409 授权冲突。"""
        grant = next(
            g for g in heatmap_share.list_grants(STORE_READER)
            if g["scope_id"] == STORE_1 and g["grain"] == "store"
        )
        heatmap_share.revoke_grant(grant["grant_id"])
        # 给原门店账号补一张区域许可，模拟“查看权限从门店收回至区域”
        heatmap_share.grant_permission(
            STORE_READER, "原门店账号（现区域口径）", "region", REGION_EAST, EVENT)

        with self.assertRaises(ShareAccessError) as ctx:
            heatmap_share.access_share(STORE_TOKEN, STORE_READER)
        err = ctx.exception
        self.assertEqual(err.status_code, 409)
        self.assertEqual(err.code, "STORE_SCOPE_CONFLICT")
        self.assertEqual(err.extra["available_grain"], "region")
        self.assertTrue(err.extra["region_share_required"])

    def test_region_review_works_only_via_region_share(self) -> None:
        """冲突后，区域复核改走区域级共享即可交付完整区域汇总。"""
        # 先撤回门店许可，保留区域运营账号的区域许可
        grant = next(
            g for g in heatmap_share.list_grants(STORE_READER)
            if g["scope_id"] == STORE_1
        )
        heatmap_share.revoke_grant(grant["grant_id"])

        # 区域运营用区域链接 → 区域口径
        data = heatmap_share.access_share(REGION_TOKEN, REGION_EAST_READER)
        self.assertEqual(data["delivered_grain"], "region")
        self.assertEqual(data["scope_id"], REGION_EAST)
        self.assertGreater(data["total_count"], 0)

    # ========== 4. 区域读者防泄露：色阶与计数只基于区域格值 ==========
    def test_region_reader_cannot_see_store_cells(self) -> None:
        data = heatmap_share.access_share(REGION_TOKEN, REGION_EAST_READER)
        # 响应不含任何门店标识
        serialized = str(data)
        self.assertNotIn(STORE_1, serialized)
        self.assertNotIn(STORE_2, serialized)
        self.assertEqual(data["delivered_grain"], "region")

        agg = heatmap_share.aggregate_spatial_events(EVENT)
        region_cells = agg["region_cells"][REGION_EAST]
        # 色阶与计数严格等于区域格值，无法反推门店
        self.assertEqual(data["cells"], region_cells)
        self.assertEqual(
            data["max_intensity"], max(c["intensity"] for c in region_cells))
        self.assertEqual(
            data["min_intensity"], min(c["intensity"] for c in region_cells))
        self.assertEqual(
            data["total_count"], sum(c["intensity"] for c in region_cells))

    def test_store_grant_cannot_read_region_scope(self) -> None:
        """门店许可不能读区域口径（区域总量会反推出其他门店格值）。"""
        with self.assertRaises(ShareAccessError) as ctx:
            heatmap_share.access_share(REGION_TOKEN, STORE_READER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "REGION_PERMISSION_REQUIRED")

    def test_region_reader_cannot_access_other_region(self) -> None:
        # 华南区域账号访问华东区域链接
        with self.assertRaises(ShareAccessError) as ctx:
            heatmap_share.access_share(REGION_TOKEN, REGION_SOUTH_READER)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_authorized_heatmap_builder_enforces_grain(self) -> None:
        # 区域读者直接申请门店粒度 → 拒绝
        with self.assertRaises(ShareAccessError):
            heatmap_share.build_authorized_heatmap(
                REGION_EAST_READER, EVENT, "store", STORE_1, "viridis")
        # 正常区域构建
        data = heatmap_share.build_authorized_heatmap(
            REGION_EAST_READER, EVENT, "region", REGION_EAST, "viridis")
        self.assertEqual(data["delivered_grain"], "region")

    # ========== 5. 失效链接与撤回共享 ==========
    def test_unknown_and_withdrawn_link(self) -> None:
        with self.assertRaises(ShareAccessError) as ctx:
            heatmap_share.access_share("not-a-real-token", STORE_READER)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "SHARE_LINK_INVALID")

        heatmap_share.revoke_share("HS-001")
        with self.assertRaises(ShareAccessError) as ctx:
            heatmap_share.access_share(STORE_TOKEN, STORE_READER)
        self.assertEqual(ctx.exception.status_code, 410)
        self.assertEqual(ctx.exception.code, "SHARE_WITHDRAWN")

    # ========== 6. 行为热力消费授权热力数据 ==========
    def test_behavior_heatmap_uses_authorized_data(self) -> None:
        result = generate_authorized_heatmap(
            "BH-001", REGION_EAST_READER, "region", REGION_EAST)
        self.assertTrue(result["source_authorized_heatmap"])
        self.assertEqual(result["delivered_grain"], "region")
        # 色阶来自授权热力数据
        agg = heatmap_share.aggregate_spatial_events(EVENT)
        region_cells = agg["region_cells"][REGION_EAST]
        self.assertEqual(result["max_intensity"],
                         max(c["intensity"] for c in region_cells))
        self.assertEqual(result["total_count"],
                         sum(c["intensity"] for c in region_cells))

        # 同一读者申请门店粒度被拒，且不产出格值
        with self.assertRaises(ShareAccessError):
            generate_authorized_heatmap(
                "BH-001", REGION_EAST_READER, "store", STORE_1)

    def test_authorized_heatmap_changes_after_revocation(self) -> None:
        """许可撤回后，已生成的授权热力再次读取会被拒绝（不缓存旧授权）。"""
        generate_authorized_heatmap("BH-001", STORE_READER, "store", STORE_1)
        grant = next(
            g for g in heatmap_share.list_grants(STORE_READER)
            if g["scope_id"] == STORE_1
        )
        heatmap_share.revoke_grant(grant["grant_id"])
        with self.assertRaises(ShareAccessError):
            generate_authorized_heatmap("BH-001", STORE_READER, "store", STORE_1)

    # ========== 7. 发布与许可登记 ==========
    def test_publish_and_grant_lifecycle(self) -> None:
        share = heatmap_share.publish_share(
            "华南区域热力", "BH-001", EVENT, "region", REGION_SOUTH,
            "plasma", "2026-11-20/2026-11-20", REGION_SOUTH_READER)
        self.assertEqual(share["status"], "已发布")
        self.assertTrue(share["token"])

        # 新读者无许可 → 拒绝；授权后可访问
        with self.assertRaises(ShareAccessError):
            heatmap_share.access_share(share["token"], "reader-new")
        heatmap_share.grant_permission(
            "reader-new", "新读者", "region", REGION_SOUTH, EVENT)
        data = heatmap_share.access_share(share["token"], "reader-new")
        self.assertEqual(data["scope_id"], REGION_SOUTH)

    def test_publish_rejects_unknown_scope(self) -> None:
        with self.assertRaises(ValueError):
            heatmap_share.publish_share(
                "bad", "BH-001", EVENT, "store", "STORE-X",
                "viridis", "2026-11-20/2026-11-20", STORE_READER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
