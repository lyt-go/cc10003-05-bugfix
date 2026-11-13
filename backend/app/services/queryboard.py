"""聚合查询业务规则：模板状态机、版本发布与废弃联动。

模板只能沿「草稿 → 模板验证中 → 已启用 → 已废弃」单向推进，不允许跳级或回退；
模板版本通过乐观锁（expected_version）替换，并发替换只接受一次有效结果。
废弃模板时在同一事务内把失效来源写到所有关联下钻路径，历史层级与查询缓存保留，
但路径上的新聚合请求会在 datadrill 服务里被拦住。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.drill_domain import (
    TEMPLATE_DEPRECATED,
    TEMPLATE_STATUS_ORDER,
    TEMPLATE_VALIDATING,
    deprecation_reason,
    template_snapshot,
)
from app.store import store

MODULE = "queryboard"
DATADRILL_MODULE = "datadrill"
REQUIRED_FIELDS = ["模板编号", "指标名称", "维度字段"]
STATUS_ORDER = TEMPLATE_STATUS_ORDER
ACTION_RULES = {"提交模板验证": "模板验证中", "启用模板": "已启用", "废弃模板": "已废弃"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class QueryboardService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 度量等选填字段原样保留，方便路径侧解析口径。
            for field in ("度量字段", "排序规则", "分页上限", "缓存秒数", "数据源"):
                if values.get(field) is not None:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["模板版本"] = 1
            entry["前序版本"] = None
            entry["版本说明"] = values.get("版本说明") or "初版"
            entry["废弃时间"] = None
            rows.append(entry)
            store.append_audit({
                "module": MODULE,
                "entry_id": entry["id"],
                "action": "登记模板",
                "at": _now(),
                "remark": entry["版本说明"],
                "template_snapshot": template_snapshot(entry),
            })
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"查询模板 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于聚合查询可执行范围"
            target = ACTION_RULES[action]
            current_index = STATUS_ORDER.index(entry["status"])
            target_index = STATUS_ORDER.index(target)
            # 状态机：只能按草稿→验证中→已启用→已废弃逐级推进，禁止跳级/回退。
            if target_index != current_index + 1:
                return None, (
                    f"模板「{entry.get('模板编号')}」当前为「{entry['status']}」，"
                    f"不能直接执行「{action}」；模板状态只能按"
                    "草稿→模板验证中→已启用→已废弃逐级推进"
                )
            if action == "废弃模板":
                try:
                    # 事务内联动写入；任一异常都会整体回滚，模板与路径都不会进入半成品状态。
                    self._deprecate(entry)
                except Exception as exc:  # noqa: BLE001 - 回滚后把原因转成可读结论
                    return None, f"模板废弃联动写入失败，两侧变更已整体回滚：{exc}"
            else:
                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = False
                store.append_audit({
                    "module": MODULE,
                    "entry_id": entry_id,
                    "action": action,
                    "at": _now(),
                    "template_snapshot": template_snapshot(entry),
                })
            return entry, f"查询模板已{action}"

    def publish_version(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """替换模板版本（并发安全）。

        调用方必须带 expected_version：与当前版本不一致说明已有别的请求先换过版，
        本次直接拒绝，保证并发替换只接受一次有效结果。换版在事务内完成，
        模板写入失败不会留下半成品，关联路径保留旧版本快照、不被静默改写。
        """
        expected = values.get("expected_version")
        try:
            expected_version = int(expected)
        except (TypeError, ValueError):
            return None, "缺少 expected_version（期望替换的模板版本号），无法做并发版本校验"
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"查询模板 {entry_id} 不存在或已归档"
            current_version = int(entry.get("模板版本", 1))
            if expected_version != current_version:
                return None, (
                    f"模板「{entry.get('模板编号')}」版本已被其他请求替换："
                    f"当前 v{current_version}，提交基于 v{expected_version}，请刷新后重试"
                )
            if entry["status"] == TEMPLATE_DEPRECATED:
                return None, "已废弃模板不能替换版本，请登记新模板"
            # 事务内换版；写入异常会整体回滚，不会留下「新版模板 + 旧版路径」的半成品。
            with store.transaction():
                previous = dict(entry)
                new_version = current_version + 1
                for field in ("维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源", "指标名称"):
                    if values.get(field) is not None:
                        entry[field] = values.get(field)
                entry["模板版本"] = new_version
                entry["前序版本"] = current_version
                entry["版本说明"] = values.get("版本说明") or f"v{new_version} 版本替换"
                entry["废弃时间"] = None
                # 换版后的模板仍需走完验证/启用流程；已启用模板换版后回到验证中，
                # 关联路径继续保留在旧版本上，不会被静默带新版。
                entry["status"] = TEMPLATE_VALIDATING
                entry["pending"] = True
                entry["abnormal"] = False
                store.append_audit({
                    "module": MODULE,
                    "entry_id": entry_id,
                    "action": "替换模板版本",
                    "at": _now(),
                    "remark": f"v{current_version} → v{new_version}：{entry['版本说明']}",
                    "template_snapshot": template_snapshot(entry),
                    "previous_snapshot": template_snapshot(previous),
                })
            return entry, (
                f"模板新版本 v{new_version} 已进入模板验证中；"
                "关联下钻路径保留旧版本，需重新对齐版本后才能继续聚合查询"
            )

    def _deprecate(self, template: dict[str, Any]) -> None:
        """废弃模板并联动路径：同事务写入，任一失败整体回滚。"""
        deprecated_at = _now()
        with store.transaction():
            template["status"] = TEMPLATE_DEPRECATED
            template["pending"] = False
            template["abnormal"] = True
            template["废弃时间"] = deprecated_at
            snapshot = template_snapshot(template)
            bound_paths = [
                row for row in store.rows(DATADRILL_MODULE)
                if int(row.get("关联模板ID", 0)) == int(template["id"])
            ]
            reason = deprecation_reason(template)
            for path in bound_paths:
                # 只在路径详情记录失效来源，不改路径状态，历史层级原样保留。
                path["失效来源"] = reason
                path["abnormal"] = True
                store.append_audit({
                    "module": DATADRILL_MODULE,
                    "entry_id": int(path["id"]),
                    "action": "模板废弃联动",
                    "at": deprecated_at,
                    "remark": reason,
                    "template_snapshot": snapshot,
                })
            store.append_audit({
                "module": MODULE,
                "entry_id": int(template["id"]),
                "action": "废弃模板",
                "at": deprecated_at,
                "remark": f"联动 {len(bound_paths)} 条下钻路径，历史层级与缓存保留",
                "template_snapshot": snapshot,
            })

    def list_audit(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return store.audit_rows(module=MODULE, entry_id=entry_id)
