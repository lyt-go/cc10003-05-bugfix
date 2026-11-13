"""聚合查询下钻：模板准入与版本协同的验收测试。

覆盖需求点名的四类情况：
1. 未启用模板（验证中/草稿/废弃）下路径不能启用；
2. 缺层级（维度无来源、层间层数不一致、度量口径不一致）下路径不能启用；
3. 旧缓存：模板废弃后历史缓存与层级保留，但新聚合请求被阻止；
4. 新模板并发切换：乐观锁只接受一次有效版本替换，路径版本漂移后需重新对齐。

另含状态机、批量整体生效、事务原子性、重复启用幂等与审计还原。
"""
from __future__ import annotations

import threading
import unittest

from app.services.datadrill import DatadrillService
from app.services.drill_domain import split_fields, split_layers, validate_path_admission
from app.services.queryboard import QueryboardService
from app.store import store


class DrillAdmissionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.templates = QueryboardService()
        self.paths = DatadrillService()

    # ------------------------------------------------------------ 场景一：未启用模板
    def test_validating_template_blocks_path_enable(self):
        """异常样例：QUER-0002 还在验证中，DATA-0002 启用必须停在路径评审阶段。"""
        entry, message = self.paths.run_action(2, "启用路径")
        self.assertIsNone(entry)
        self.assertIn("模板验证中", message)
        self.assertIn("维度字段缺少来源", message)
        path = store.find("datadrill", 2)
        self.assertEqual(path["status"], "路径评审中")
        self.assertTrue(path["pending"])

    def test_enabled_template_path_unaffected(self):
        """已启用的 QUER-0003 / DATA-0003 不受影响，可正常聚合。"""
        path = self.paths.get_entry(3)
        self.assertEqual(path["status"], "已启用")
        self.assertTrue(path["模板可用"])
        self.assertTrue(path["版本一致"])
        result, message, cached = self.paths.aggregate(3, level=1)
        self.assertIsNotNone(result)
        self.assertFalse(cached)

    def test_deprecated_and_draft_templates_block_enable(self):
        # QUER-0004 已废弃：关联路径即使处于评审中也无法重新启用。
        draft_path, missing, err = self.paths.create_entry({
            "路径编号": "DATA-T1",
            "起始维度": "活动",
            "下钻层级": "活动>渠道",
            "每层维度名": "活动,渠道",
            "每层度量": "转化金额,转化金额",
            "关联模板ID": 4,
        })
        self.assertIsNotNone(draft_path)
        self.paths.run_action(draft_path["id"], "提交路径评审")
        entry, message = self.paths.run_action(draft_path["id"], "启用路径")
        self.assertIsNone(entry)
        self.assertIn("已废弃", message)
        self.assertEqual(store.find("datadrill", draft_path["id"])["status"], "路径评审中")

        # 草稿模板同样不允许启用路径。
        entry2, message2 = self.paths.run_action(1, "启用路径")
        self.assertIsNone(entry2)
        self.assertIn("草稿", message2)

    def test_template_state_machine_is_strict(self):
        """模板只能逐级推进，跳级与回退都被拒绝。"""
        # QUER-0001 草稿 -> 直接启用模板（跳级）
        entry, message = self.templates.run_action(1, "启用模板")
        self.assertIsNone(entry)
        self.assertIn("逐级推进", message)
        # 正确推进
        self.templates.run_action(1, "提交模板验证")
        self.assertEqual(store.find("queryboard", 1)["status"], "模板验证中")
        self.templates.run_action(1, "启用模板")
        self.assertEqual(store.find("queryboard", 1)["status"], "已启用")
        # 已启用 -> 提交模板验证（回退）
        back, message_back = self.templates.run_action(1, "提交模板验证")
        self.assertIsNone(back)
        self.assertIn("逐级推进", message_back)

    # ------------------------------------------------------------ 场景二：缺层级/口径不一致
    def test_missing_dimension_source_blocks_enable(self):
        # 先把 QUER-0002 推到已启用，暴露纯粹的维度来源问题。
        self.templates.run_action(2, "提交模板验证")
        self.templates.run_action(2, "启用模板")
        entry, message = self.paths.run_action(2, "启用路径")
        self.assertIsNone(entry)
        self.assertIn("维度字段缺少来源", message)
        self.assertIn("机型", message)
        self.assertEqual(store.find("datadrill", 2)["status"], "路径评审中")

    def test_measure_caliber_mismatch_blocks_enable(self):
        path = {
            "id": 99,
            "路径编号": "DATA-X",
            "起始维度": "品类",
            "每层维度名": "品类,子品类,商品",
            "每层度量": "销售额|销售额,销售件数|销售额,销售件数",
        }
        template = store.find("queryboard", 3)
        errors = validate_path_admission(path, template)
        self.assertTrue(any("口径不一致" in item for item in errors))

    def test_layer_count_mismatch_blocks_enable(self):
        path = {
            "id": 98,
            "路径编号": "DATA-Y",
            "起始维度": "品类",
            "每层维度名": "品类,子品类",
            "每层度量": "销售额,销售件数|销售额,销售件数|销售额,销售件数",
        }
        template = store.find("queryboard", 3)
        errors = validate_path_admission(path, template)
        self.assertTrue(any("中间层级" in item for item in errors))

    def test_valid_admission_passes_and_snapshot_pinned(self):
        path, missing, err = self.paths.create_entry({
            "路径编号": "DATA-OK",
            "起始维度": "品类",
            "下钻层级": "品类>子品类>商品",
            "每层维度名": "品类,子品类,商品",
            "每层度量": "销售额,销售件数|销售额,销售件数|销售额,销售件数",
            "关联模板ID": 3,
        })
        self.paths.run_action(path["id"], "提交路径评审")
        entry, message = self.paths.run_action(path["id"], "启用路径")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已启用")
        self.assertEqual(entry["模板版本快照"], 2)
        self.assertIsNotNone(entry["启用时间"])

    # ------------------------------------------------------------ 场景三：旧缓存与废弃联动
    def test_deprecation_keeps_history_but_blocks_new_queries(self):
        # 先产生两层缓存。
        self.paths.aggregate(3, level=1)
        self.paths.aggregate(3, level=2)
        self.assertEqual(len(self.paths.cache_entries(3)), 2)

        entry, message = self.templates.run_action(3, "废弃模板")
        self.assertEqual(entry["status"], "已废弃")

        # 新聚合请求被阻止。
        result, blocked_message, cached = self.paths.aggregate(3, level=1)
        self.assertIsNone(result)
        self.assertFalse(cached)
        self.assertIn("废弃", blocked_message)

        # 历史层级与缓存保留。
        path = self.paths.get_entry(3)
        self.assertEqual(path["status"], "已启用")
        self.assertEqual(len(self.paths.cache_entries(3)), 2)
        self.assertIsNotNone(path["失效来源"])
        self.assertIn("QUER-0003", path["失效来源"])

    def test_deprecation_cascade_is_atomic(self):
        """模板废弃联动路径写入失败时，模板侧也不能进入已废弃。"""
        original_append = store.append_audit

        def flaky_append(record):
            if record.get("action") == "模板废弃联动":
                raise RuntimeError("模拟联动路径审计写入失败")
            return original_append(record)

        store.append_audit = flaky_append  # type: ignore[method-assign]
        try:
            entry, message = self.templates.run_action(3, "废弃模板")
            self.assertIsNone(entry)
            self.assertIn("整体回滚", message)
        finally:
            store.append_audit = original_append  # type: ignore[method-assign]

        # 事务回滚：模板仍是已启用，路径没有失效来源，没有半成品。
        self.assertEqual(store.find("queryboard", 3)["status"], "已启用")
        self.assertIsNone(store.find("datadrill", 3)["失效来源"])

    # ------------------------------------------------------------ 场景四：并发版本切换
    def test_concurrent_version_publish_accepts_only_one(self):
        outcomes: list[tuple[str, str]] = []

        def publish(barrier: threading.Barrier) -> None:
            barrier.wait()
            entry, message = self.templates.publish_version(3, {
                "expected_version": 2,
                "维度字段": "品类,子品类,商品,区域",
            })
            outcomes.append(("ok" if entry else "rejected", message))

        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=publish, args=(barrier,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(status for status, _ in outcomes), ["ok", "rejected"])
        template = store.find("queryboard", 3)
        self.assertEqual(template["模板版本"], 3)
        self.assertEqual(template["status"], "模板验证中")

    def test_stale_expected_version_is_rejected(self):
        entry, message = self.templates.publish_version(3, {"expected_version": 99})
        self.assertIsNone(entry)
        self.assertIn("版本已被其他请求替换", message)

    def test_version_drift_blocks_query_until_rebind(self):
        # 换版前先产生一条旧版本缓存。
        old_result, _, old_cached = self.paths.aggregate(3, level=1)
        self.assertIsNotNone(old_result)
        self.assertFalse(old_cached)

        # 模板换版：路径保留旧版本快照。
        new_template, _ = self.templates.publish_version(3, {"expected_version": 2})
        self.assertEqual(new_template["模板版本"], 3)
        path = self.paths.get_entry(3)
        self.assertEqual(path["模板版本快照"], 2)
        self.assertFalse(path["版本一致"])

        # 版本漂移期间新聚合请求被阻止，旧版本缓存仍保留可读。
        result, message, _ = self.paths.aggregate(3, level=1)
        self.assertIsNone(result)
        self.assertIn("对齐模板版本", message)
        self.assertGreaterEqual(len(self.paths.cache_entries(3)), 1)

        self.templates.run_action(3, "启用模板")
        # 对齐后恢复查询。
        rebound, rebind_message = self.paths.rebind_template_version(3)
        self.assertIsNotNone(rebound, rebind_message)
        self.assertEqual(rebound["模板版本快照"], 3)
        result2, _, _ = self.paths.aggregate(3, level=1)
        self.assertIsNotNone(result2)

    # ------------------------------------------------------------ 幂等与批量
    def test_repeat_enable_is_idempotent(self):
        first, _ = self.paths.run_action(3, "启用路径")
        self.assertIsNotNone(first)
        audit_before = len(self.paths.list_audit(3))
        second, message = self.paths.run_action(3, "启用路径")
        self.assertIsNotNone(second)
        self.assertIn("重复启用不产生新结果", message)
        self.assertEqual(len(self.paths.list_audit(3)), audit_before)

    def test_batch_enable_all_or_nothing(self):
        # 准备一条可启用的新路径（绑定已启用 QUER-0003，口径匹配）。
        ok_path, _, _ = self.paths.create_entry({
            "路径编号": "DATA-B1",
            "起始维度": "品类",
            "下钻层级": "品类>子品类",
            "每层维度名": "品类,子品类",
            "每层度量": "销售额,销售件数|销售额,销售件数",
            "关联模板ID": 3,
        })
        self.paths.run_action(ok_path["id"], "提交路径评审")
        # DATA-0002 绑定验证中模板，必失败。
        outcome = self.paths.batch_enable([ok_path["id"], 2])
        self.assertFalse(outcome["ok"])
        self.assertEqual(len(outcome["results"]), 2)
        # 整组不生效：可启用的那条也必须停在评审中，无半成品。
        self.assertEqual(store.find("datadrill", ok_path["id"])["status"], "路径评审中")
        self.assertEqual(store.find("datadrill", 2)["status"], "路径评审中")

        # 把 DATA-0002 换成合法配置后整组通过。
        bad = store.find("datadrill", 2)
        bad["关联模板ID"] = 3
        bad["起始维度"] = "品类"
        bad["每层维度名"] = "品类,子品类,商品"
        bad["每层度量"] = "销售额,销售件数|销售额,销售件数|销售额,销售件数"
        outcome2 = self.paths.batch_enable([ok_path["id"], 2])
        self.assertTrue(outcome2["ok"], outcome2["message"])
        self.assertTrue(all(item["ok"] for item in outcome2["results"]))
        self.assertEqual(store.find("datadrill", ok_path["id"])["status"], "已启用")

    def test_batch_enable_skips_already_enabled(self):
        outcome = self.paths.batch_enable([3])
        self.assertTrue(outcome["ok"])
        self.assertIn("幂等跳过", outcome["results"][0]["message"])

    def test_batch_enable_dedupes_duplicate_ids(self):
        outcome = self.paths.batch_enable([3, 3])
        self.assertTrue(outcome["ok"])
        self.assertEqual(len(outcome["results"]), 1)

    def test_enable_write_failure_rolls_back_both_sides(self):
        """启用写入（审计）失败时，路径与模板都不能进入可用状态。"""
        path, _, _ = self.paths.create_entry({
            "路径编号": "DATA-RB",
            "起始维度": "品类",
            "下钻层级": "品类>子品类",
            "每层维度名": "品类,子品类",
            "每层度量": "销售额,销售件数|销售额,销售件数",
            "关联模板ID": 3,
        })
        self.paths.run_action(path["id"], "提交路径评审")
        original_append = store.append_audit

        def flaky_append(record):
            if record.get("action") == "启用路径":
                raise RuntimeError("模拟启用审计写入失败")
            return original_append(record)

        store.append_audit = flaky_append  # type: ignore[method-assign]
        try:
            entry, message = self.paths.run_action(path["id"], "启用路径")
            self.assertIsNone(entry)
            self.assertIn("整体回滚", message)
        finally:
            store.append_audit = original_append  # type: ignore[method-assign]

        self.assertEqual(store.find("datadrill", path["id"])["status"], "路径评审中")
        # 模板侧不受影响，仍是已启用。
        self.assertEqual(store.find("queryboard", 3)["status"], "已启用")

    # ------------------------------------------------------------ 审计还原
    def test_audit_reconstructs_template_version(self):
        self.paths.aggregate(3, level=1)
        audit = self.paths.list_audit(3)
        query_records = [row for row in audit if row["action"] == "聚合查询"]
        self.assertTrue(query_records)
        snapshot = query_records[-1]["template_snapshot"]
        self.assertEqual(snapshot["模板编号"], "QUER-0003")
        self.assertEqual(snapshot["模板版本"], 2)
        self.assertEqual(snapshot["维度字段"], ["品类", "子品类", "商品"])
        self.assertEqual(snapshot["度量字段"], ["销售额", "销售件数"])

        # 模板侧审计保留每次状态/版本变化的快照：先在 v2 上产生一条审计，再换版到 v3。
        self.templates.run_action(3, "废弃模板")
        audit_v2 = [row for row in self.templates.list_audit(3) if row.get("template_snapshot", {}).get("模板版本") == 2]
        self.assertTrue(audit_v2)
        # 废弃是终态，另取一个已启用模板验证换版审计链路。
        entry, _ = self.templates.create_entry({
            "模板编号": "QUER-NEW",
            "指标名称": "并发换版验证",
            "维度字段": "a,b",
            "度量字段": "m",
        })
        new_id = entry["id"]
        self.templates.run_action(new_id, "提交模板验证")
        self.templates.run_action(new_id, "启用模板")
        self.templates.publish_version(new_id, {"expected_version": 1, "维度字段": "a,b,c"})
        tpl_audit = self.templates.list_audit(new_id)
        versions = {row["template_snapshot"]["模板版本"] for row in tpl_audit if row.get("template_snapshot")}
        self.assertIn(1, versions)
        self.assertIn(2, versions)

    def test_field_parsing_helpers(self):
        self.assertEqual(split_fields("品类, 子品类、商品；区域"), ["品类", "子品类", "商品", "区域"])
        self.assertEqual(split_layers("品类,子品类,商品"), [["品类"], ["子品类"], ["商品"]])
        self.assertEqual(
            split_layers("销售额,件数|销售额,件数"),
            [["销售额", "件数"], ["销售额", "件数"]],
        )


if __name__ == "__main__":
    unittest.main()
