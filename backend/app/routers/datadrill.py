"""多维下钻接口：维护下钻路径，覆盖提交路径评审、启用路径、关闭路径等动作。

模板准入规则在服务层统一执行：
- 启用前校验模板已启用、每层维度有来源、逐层度量口径一致；
- 批量启用先做整组引用校验，全部通过才整体生效，结论逐条返回；
- 聚合查询在模板废弃/版本漂移后拒绝新请求，历史层级与缓存保留；
- 审计流水保存每次变化时的模板版本快照，可还原当时模板版本。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    BatchActionPayload,
    BatchActionResult,
    EntryPayload,
    PageResult,
)
from app.services.datadrill import DatadrillService

router = APIRouter(prefix="/api/datadrill", tags=["多维下钻"])

service = DatadrillService()

LIST_FIELDS = ["路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量", "路径深度", "默认展开层", "关联看板"]
STATUSES = ["草稿", "路径评审中", "已启用", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="草稿、路径评审中、已启用、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号与状态过滤多维下钻列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/audit")
def list_audit() -> dict[str, Any]:
    """下钻路径侧审计流水，含模板版本快照与聚合查询记录。"""
    return {"module": "datadrill", "items": service.list_audit()}


@router.get("/caches")
def list_caches() -> dict[str, Any]:
    """历史聚合查询缓存；模板废弃后缓存仍保留，可在此回看。"""
    return {"module": "datadrill", "items": service.cache_entries()}


@router.post("/batch-actions", response_model=BatchActionResult)
def batch_action(payload: BatchActionPayload) -> BatchActionResult:
    """批量启用入口：支持多选一次提交，整组引用校验通过后才整体生效。"""
    action = (payload.action or "启用路径").strip()
    if action != "启用路径":
        raise HTTPException(status_code=400, detail="批量入口当前仅支持「启用路径」")
    outcome = service.batch_enable(payload.entry_ids)
    return BatchActionResult(**outcome)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datadrill", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/audit")
def get_entry_audit(entry_id: int) -> dict[str, Any]:
    """单条路径的审计流水，可还原启用/查询时绑定的模板版本。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return {"module": "datadrill", "entry_id": entry_id, "items": service.list_audit(entry_id)}


@router.get("/{entry_id}/caches")
def get_entry_caches(entry_id: int) -> dict[str, Any]:
    """单条路径的历史查询缓存。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return {"module": "datadrill", "entry_id": entry_id, "items": service.cache_entries(entry_id)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条下钻路径，缺字段时说明原因而不是静默丢弃；可同时绑定查询模板版本。"""
    entry, missing, error = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="下钻路径已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条下钻路径执行提交路径评审、启用路径、关闭路径；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/rebind-version", response_model=ActionResult)
def rebind_version(entry_id: int) -> ActionResult:
    """把路径重新对齐到关联模板的最新已启用版本；校验失败时保留旧版本快照。"""
    entry, message = service.rebind_template_version(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/aggregate", response_model=ActionResult)
def aggregate(entry_id: int, payload: EntryPayload) -> ActionResult:
    """沿路径某一层发起聚合查询；模板废弃/版本漂移时拒绝新请求并说明失效来源。"""
    try:
        level = int(payload.values.get("level", 1))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="level 必须是正整数层级号")
    filters = payload.values.get("filters")
    if filters is not None and not isinstance(filters, dict):
        return ActionResult(ok=False, message="filters 必须是字段到取值的映射")
    result, message, cached = service.aggregate(entry_id, level=level, filters=filters)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"cached": cached, "result": result})
