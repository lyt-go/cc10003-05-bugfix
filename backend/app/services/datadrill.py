"""多维下钻业务规则：模板准入、状态流转、版本对齐与聚合查询缓存。

启用路径前必须同时满足：
1. 关联查询模板存在且处于「已启用」；
2. 路径每一层维度都能在模板维度字段中找到来源；
3. 路径各层度量口径一致，且与模板度量字段完全相同。

模板与路径围绕同一模板版本共同变化：路径保存启用时的模板版本快照，模板后续
换版/废弃不会改写历史层级；模板废弃后历史缓存保留但拒绝新的聚合请求，并在
路径详情给出失效来源。所有跨表写入都在 store 事务内完成，任一失败两侧都不会
进入可用状态。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.drill_domain import (
    PATH_CLOSED,
    PATH_ENABLED,
    PATH_REVIEWING,
    PATH_STATUS_ORDER,
    TEMPLATE_DEPRECATED,
    TEMPLATE_ENABLED,
    deprecation_reason,
    split_layers,
    template_snapshot,
    validate_path_admission,
)
from app.services.queryboard import MODULE as TEMPLATE_MODULE
from app.store import store

MODULE = "datadrill"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级"]
STATUS_ORDER = PATH_STATUS_ORDER
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "关闭路径": "已关闭"}

# 创建路径时允许一并登记的路径专属字段。
_OPTIONAL_FIELDS = [
    "每层维度名",
    "每层度量",
    "路径深度",
    "默认展开层",
    "关联看板",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DatadrillService:
    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._enrich(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._enrich(entry) if entry else None

    def _enrich(self, path: dict[str, Any]) -> dict[str, Any]:
        """补充模板实时状态与可用性结论，列表与详情走同一口径，避免显示成不同版本。"""
        template = store.find(TEMPLATE_MODULE, int(path.get("关联模板ID", 0))) if path.get("关联模板ID") else None
        enriched = dict(path)
        if template is None:
            enriched["模板当前版本"] = None
            enriched["模板当前状态"] = None
            enriched["模板可用"] = False
            enriched["版本一致"] = False
            if not enriched.get("失效来源"):
                enriched["失效来源"] = "关联查询模板不存在或已归档"
            return enriched
        enriched["模板当前版本"] = int(template.get("模板版本", 1))
        enriched["模板当前状态"] = template.get("status")
        enriched["模板可用"] = template.get("status") == TEMPLATE_ENABLED
        enriched["版本一致"] = int(template.get("模板版本", 1)) == int(path.get("模板版本快照", 0))
        if template.get("status") == TEMPLATE_DEPRECATED and not enriched.get("失效来源"):
            enriched["失效来源"] = deprecation_reason(template)
        return enriched

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing, ""
        with store.lock:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in _OPTIONAL_FIELDS:
                if values.get(field) is not None:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["启用时间"] = None
            entry["失效来源"] = None

            # 绑定模板：模板必须存在；草稿阶段先固化版本快照，准入留到启用前统一判断。
            template_id = values.get("关联模板ID")
            if template_id is not None:
                template = store.find(TEMPLATE_MODULE, int(template_id))
                if template is None:
                    return None, [], f"关联查询模板（ID={template_id}）不存在，路径不能绑定不存在的模板"
                entry["关联模板ID"] = int(template_id)
                entry["关联模板编号"] = template.get("模板编号")
                entry["模板版本快照"] = int(template.get("模板版本", 1))
            else:
                entry["关联模板ID"] = None
                entry["关联模板编号"] = None
                entry["模板版本快照"] = None

            rows.append(entry)
            store.append_audit({
                "module": MODULE,
                "entry_id": entry["id"],
                "action": "登记路径",
                "at": _now(),
                "template_snapshot": (
                    template_snapshot(store.find(TEMPLATE_MODULE, int(template_id)))
                    if template_id is not None else None
                ),
            })
            return entry, [], ""

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"下钻路径 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于多维下钻可执行范围"
            target = ACTION_RULES[action]
            current_index = STATUS_ORDER.index(entry["status"])
            target_index = STATUS_ORDER.index(target)

            # 重复启用同一路径按幂等处理：只接受第一次有效结果，重复请求直接返回当前状态。
            if action == "启用路径" and entry["status"] == PATH_ENABLED:
                return entry, f"路径「{entry.get('路径编号')}」已是启用状态，重复启用不产生新结果"

            if target_index != current_index + 1:
                return None, (
                    f"路径「{entry.get('路径编号')}」当前为「{entry['status']}」，"
                    f"不能直接执行「{action}」；路径状态只能按"
                    "草稿→路径评审中→已启用→已关闭逐级推进"
                )

            if action == "提交路径评审":
                entry["status"] = PATH_REVIEWING
                entry["pending"] = True
                store.append_audit({
                    "module": MODULE,
                    "entry_id": entry_id,
                    "action": action,
                    "at": _now(),
                    "template_snapshot": self._bound_snapshot(entry),
                })
                return entry, "下钻路径已提交路径评审"

            if action == "启用路径":
                return self._enable_one(entry, audit=True)

            # 关闭路径：只影响状态，历史层级与缓存继续保留。
            entry["status"] = PATH_CLOSED
            entry["pending"] = False
            store.append_audit({
                "module": MODULE,
                "entry_id": entry_id,
                "action": action,
                "at": _now(),
                "template_snapshot": self._bound_snapshot(entry),
            })
            return entry, "下钻路径已关闭，历史层级与查询缓存保留"

    def _bound_template(self, path: dict[str, Any]) -> dict[str, Any] | None:
        template_id = path.get("关联模板ID")
        return store.find(TEMPLATE_MODULE, int(template_id)) if template_id is not None else None

    def _bound_snapshot(self, path: dict[str, Any]) -> dict[str, Any] | None:
        template = self._bound_template(path)
        return template_snapshot(template) if template else None

    def _enable_one(
        self,
        entry: dict[str, Any],
        *,
        audit: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        """启用单条路径；已启用路径重复启用按幂等处理，不产生第二条有效结果。"""
        if entry["status"] == PATH_ENABLED:
            return entry, f"路径「{entry.get('路径编号')}」已是启用状态，无需重复启用"

        template = self._bound_template(entry)
        errors = validate_path_admission(entry, template)
        if errors:
            # 关键修复：启用失败时停在路径评审阶段，不留半成品，原因逐条返回。
            entry["status"] = PATH_REVIEWING
            entry["pending"] = True
            return None, "；".join(errors)

        assert template is not None
        try:
            with store.transaction():
                entry["status"] = PATH_ENABLED
                entry["pending"] = False
                entry["abnormal"] = False
                entry["启用时间"] = _now()
                entry["失效来源"] = None
                entry["关联模板编号"] = template.get("模板编号")
                entry["模板版本快照"] = int(template.get("模板版本", 1))
                if audit:
                    store.append_audit({
                        "module": MODULE,
                        "entry_id": int(entry["id"]),
                        "action": "启用路径",
                        "at": entry["启用时间"],
                        "template_snapshot": template_snapshot(template),
                    })
        except Exception as exc:  # noqa: BLE001 - 事务已回滚，转成可读结论
            entry["status"] = PATH_REVIEWING
            entry["pending"] = True
            return None, f"启用写入失败，模板与路径变更已整体回滚：{exc}"
        return entry, (
            f"下钻路径已启用，绑定模板 {template.get('模板编号')}"
            f"(v{int(template.get('模板版本', 1))})"
        )

    def batch_enable(self, entry_ids: list[int]) -> dict[str, Any]:
        """批量启用：先整组引用校验，全部通过才在一个事务里整体生效。

        任一条不通过则整组拒绝、没有任何路径改变状态，因此不会残留半成品路径；
        已启用的路径按幂等跳过。逐条返回结论，整体结论以 ok 表示。
        """
        if not entry_ids:
            return {"ok": False, "message": "未选择任何下钻路径", "results": []}
        # 去重并保持提交顺序，避免同一路径在一批里被处理两次。
        seen: set[int] = set()
        entry_ids = [int(x) for x in entry_ids if not (int(x) in seen or seen.add(int(x)))]
        results: list[dict[str, Any]] = []
        with store.lock:
            targets: list[dict[str, Any]] = []
            rejected = False
            # 先按提交顺序逐条做引用校验，每条都给出独立结论。
            for raw_id in entry_ids:
                entry = store.find(MODULE, int(raw_id))
                if entry is None:
                    rejected = True
                    results.append({
                        "entry_id": raw_id,
                        "路径编号": None,
                        "ok": False,
                        "message": f"下钻路径 {raw_id} 不存在或已归档",
                    })
                    continue
                if entry["status"] == PATH_ENABLED:
                    results.append({
                        "entry_id": int(entry["id"]),
                        "路径编号": entry.get("路径编号"),
                        "ok": True,
                        "message": "已是启用状态，幂等跳过",
                    })
                    continue
                errors = validate_path_admission(entry, self._bound_template(entry))
                if errors:
                    rejected = True
                    results.append({
                        "entry_id": int(entry["id"]),
                        "路径编号": entry.get("路径编号"),
                        "ok": False,
                        "message": "；".join(errors),
                    })
                    # 启用被拒后停在路径评审阶段，绝不残留半成品状态。
                    entry["status"] = PATH_REVIEWING
                    entry["pending"] = True
                else:
                    targets.append(entry)
                    # 先登记逐条结论，整体生效阶段再补充最终消息。
                    results.append({
                        "entry_id": int(entry["id"]),
                        "路径编号": entry.get("路径编号"),
                        "ok": False,
                        "message": "等待整组引用校验结果",
                    })

            if rejected:
                # 整组引用校验未通过：不进入生效阶段，所有待启用路径都停在评审中。
                for entry in targets:
                    entry["status"] = PATH_REVIEWING
                    entry["pending"] = True
                return {
                    "ok": False,
                    "message": "整组引用校验未通过，本次批量启用整体不生效",
                    "results": results,
                }

            enabled_at = _now()
            try:
                with store.transaction():
                    for entry in targets:
                        template = self._bound_template(entry)
                        assert template is not None
                        entry["status"] = PATH_ENABLED
                        entry["pending"] = False
                        entry["abnormal"] = False
                        entry["启用时间"] = enabled_at
                        entry["失效来源"] = None
                        entry["关联模板编号"] = template.get("模板编号")
                        entry["模板版本快照"] = int(template.get("模板版本", 1))
                        store.append_audit({
                            "module": MODULE,
                            "entry_id": int(entry["id"]),
                            "action": "批量启用路径",
                            "at": enabled_at,
                            "template_snapshot": template_snapshot(template),
                        })
                        # 回填该条目的最终结论，保持返回顺序与提交顺序一致。
                        for item in results:
                            if item["entry_id"] == int(entry["id"]):
                                item["ok"] = True
                                item["message"] = (
                                    f"已启用，绑定模板 {template.get('模板编号')}"
                                    f"(v{int(template.get('模板版本', 1))})"
                                )
                                break
            except Exception as exc:  # noqa: BLE001 - 整组回滚，逐条标记失败
                for entry in targets:
                    entry["status"] = PATH_REVIEWING
                    entry["pending"] = True
                for item in results:
                    if item["entry_id"] in {int(row["id"]) for row in targets}:
                        item["ok"] = False
                        item["message"] = f"整组生效写入失败已回滚：{exc}"
                return {"ok": False, "message": "批量启用写入失败，整组变更已回滚", "results": results}
            return {"ok": True, "message": f"整组 {len(targets)} 条路径已整体生效", "results": results}

    def rebind_template_version(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """路径重新对齐模板最新版本：仍需通过同一套准入校验，失败则保留旧版本快照。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"下钻路径 {entry_id} 不存在或已归档"
            template = self._bound_template(entry)
            errors = validate_path_admission(entry, template)
            if errors:
                return None, "版本对齐失败，保留原模板版本快照：" + "；".join(errors)
            assert template is not None
            try:
                with store.transaction():
                    old_version = entry.get("模板版本快照")
                    entry["模板版本快照"] = int(template.get("模板版本", 1))
                    entry["关联模板编号"] = template.get("模板编号")
                    entry["失效来源"] = None
                    store.append_audit({
                        "module": MODULE,
                        "entry_id": entry_id,
                        "action": "对齐模板版本",
                        "at": _now(),
                        "remark": f"v{old_version} → v{entry['模板版本快照']}",
                        "template_snapshot": template_snapshot(template),
                    })
            except Exception as exc:  # noqa: BLE001 - 回滚后保留旧版本快照
                return None, f"版本对齐写入失败已回滚，保留原版本快照：{exc}"
            return entry, (
                f"路径已对齐模板 {template.get('模板编号')}(v{entry['模板版本快照']})"
            )

    # ------------------------------------------------------------------ 聚合查询
    def aggregate(
        self,
        entry_id: int,
        *,
        level: int,
        filters: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """沿路径某一层发起聚合查询，返回 (结果, 说明, 是否命中缓存)。

        准入：路径已启用、模板已启用且版本未漂移；模板废弃/换版后新请求一律拒绝，
        但历史层级与缓存保留，可通过 cache_entries 回看。
        """
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"下钻路径 {entry_id} 不存在或已归档", False
            if entry["status"] != PATH_ENABLED:
                return None, (
                    f"路径「{entry.get('路径编号')}」当前为「{entry['status']}」，"
                    "只有已启用路径才能发起聚合查询"
                ), False
            template = self._bound_template(entry)
            if template is None:
                return None, "关联查询模板不存在或已归档，聚合请求已阻止", False
            if template["status"] == TEMPLATE_DEPRECATED:
                return None, deprecation_reason(template), False
            # 版本漂移优先于模板状态提示：路径绑定的是旧版本，必须先对齐才能继续。
            if int(template.get("模板版本", 1)) != int(entry.get("模板版本快照", 0)):
                return None, (
                    f"路径绑定 v{entry.get('模板版本快照')}，模板已切换到 "
                    f"v{template.get('模板版本')}，请先对齐模板版本再查询；历史缓存保留"
                ), False
            if template["status"] != TEMPLATE_ENABLED:
                return None, (
                    f"模板 {template.get('模板编号')}(v{template.get('模板版本')}) "
                    f"当前为「{template['status']}」，新的聚合请求已阻止"
                ), False

            layer_dims = split_layers(entry.get("每层维度名"))
            layer_measures = split_layers(entry.get("每层度量"))
            if level < 1 or level > len(layer_dims):
                return None, f"层级 level={level} 超出路径深度 {len(layer_dims)}", False

            cache_key = (
                int(entry["id"]),
                int(template["id"]),
                int(template["模板版本"]),
                level,
                repr(sorted((filters or {}).items())),
            )
            cached = store.query_cache.get(cache_key)
            if cached is not None:
                return cached, f"命中模板 v{template['模板版本']} 历史查询缓存", True

            # 内存演示环境：聚合结果按模板维度/度量生成确定性样例行，真实项目替换为数据源执行。
            dims = layer_dims[level - 1]
            measures = layer_measures[level - 1]
            rows_out = [
                {
                    "层级": level,
                    **{dim: f"{dim}-样例-{index + 1}" for dim in dims},
                    **{measure: (index + 1) * 100 + level * 7 for measure in measures},
                }
                for index in range(3)
            ]
            result = {
                "路径ID": int(entry["id"]),
                "路径编号": entry.get("路径编号"),
                "模板编号": template.get("模板编号"),
                "模板版本": int(template["模板版本"]),
                "level": level,
                "维度": dims,
                "度量": measures,
                "filters": filters or {},
                "rows": rows_out,
                "at": _now(),
            }
            store.query_cache[cache_key] = result
            store.append_audit({
                "module": MODULE,
                "entry_id": entry_id,
                "action": "聚合查询",
                "at": result["at"],
                "remark": f"level={level} 写入查询缓存",
                "template_snapshot": template_snapshot(template),
            })
            return result, f"聚合查询完成，结果已按模板 v{template['模板版本']} 缓存", False

    def cache_entries(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        """回看历史查询缓存；模板废弃后这里的数据仍然保留。"""
        items = list(store.query_cache.values())
        if entry_id is not None:
            items = [item for item in items if int(item.get("路径ID", 0)) == entry_id]
        return items

    def list_audit(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return store.audit_rows(module=MODULE, entry_id=entry_id)
