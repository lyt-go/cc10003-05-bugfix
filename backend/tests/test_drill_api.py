"""沿真实 HTTP 调用链（路由 → 服务 → 存储）验证模板准入与批量入口。"""
from __future__ import annotations

import asyncio
import unittest

import httpx

from app.main import app
from app.store import store


class DrillApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        store.reset()
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_anomaly_sample_blocked_over_http(self):
        """QUER-0002 验证中时，DATA-0002 经真实接口启用失败且停在路径评审中。"""
        response = await self.client.post(
            "/api/datadrill/2/actions",
            json={"values": {"action": "启用路径"}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("模板验证中", body["message"])
        self.assertIn("维度字段缺少来源", body["message"])

        detail = (await self.client.get("/api/datadrill/2")).json()
        self.assertEqual(detail["status"], "路径评审中")

        # 已启用的 DATA-0003 不受影响。
        ok_detail = (await self.client.get("/api/datadrill/3")).json()
        self.assertEqual(ok_detail["status"], "已启用")
        self.assertTrue(ok_detail["模板可用"])

    async def test_batch_endpoint_all_or_nothing(self):
        response = await self.client.post(
            "/api/datadrill/batch-actions",
            json={"action": "启用路径", "entry_ids": [2, 3]},
        )
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual({item["entry_id"] for item in body["results"]}, {2, 3})
        failed = next(item for item in body["results"] if item["entry_id"] == 2)
        self.assertFalse(failed["ok"])
        # 失败条目没有把任何路径推到已启用之外的半成品状态。
        detail = (await self.client.get("/api/datadrill/2")).json()
        self.assertEqual(detail["status"], "路径评审中")

    async def test_batch_rejects_unsupported_action(self):
        response = await self.client.post(
            "/api/datadrill/batch-actions",
            json={"action": "关闭路径", "entry_ids": [3]},
        )
        self.assertEqual(response.status_code, 400)

    async def test_aggregate_cache_and_deprecation_over_http(self):
        # 首次查询写缓存。
        first = (await self.client.post(
            "/api/datadrill/3/aggregate", json={"values": {"level": 1}}
        )).json()
        self.assertTrue(first["ok"])
        self.assertFalse(first["entry"]["cached"])
        second = (await self.client.post(
            "/api/datadrill/3/aggregate", json={"values": {"level": 1}}
        )).json()
        self.assertTrue(second["entry"]["cached"])

        # 废弃模板。
        dep = (await self.client.post(
            "/api/queryboard/3/actions",
            json={"values": {"action": "废弃模板"}},
        )).json()
        self.assertTrue(dep["ok"])

        # 新请求被阻止。
        blocked = (await self.client.post(
            "/api/datadrill/3/aggregate", json={"values": {"level": 1}}
        )).json()
        self.assertFalse(blocked["ok"])
        self.assertIn("废弃", blocked["message"])

        # 缓存与审计仍可读取，审计里能还原模板版本。
        caches = (await self.client.get("/api/datadrill/3/caches")).json()["items"]
        self.assertTrue(caches)
        audit = (await self.client.get("/api/datadrill/3/audit")).json()["items"]
        snapshots = [row["template_snapshot"] for row in audit if row.get("template_snapshot")]
        self.assertTrue(
            any(snap["模板版本"] == 2 and snap["模板编号"] == "QUER-0003" for snap in snapshots)
        )

    async def test_concurrent_version_publish_over_http(self):
        async def publish() -> bool:
            resp = await self.client.post(
                "/api/queryboard/3/versions",
                json={"expected_version": 2, "values": {"维度字段": "品类,子品类,商品,区域"}},
            )
            return bool(resp.json()["ok"])

        results = await asyncio.gather(publish(), publish())
        self.assertEqual(sorted(results), [False, True])
        template = (await self.client.get("/api/queryboard/3")).json()
        self.assertEqual(template["模板版本"], 3)
        self.assertEqual(template["status"], "模板验证中")


if __name__ == "__main__":
    unittest.main()
