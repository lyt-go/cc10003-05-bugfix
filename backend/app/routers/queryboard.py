"""聚合查询接口：维护查询模板，覆盖提交模板验证、启用模板、废弃模板等动作。

额外提供模板版本替换（乐观并发）与审计查询：版本与下钻路径围绕同一模板版本
共同变化，审计里保存每次变化时的模板版本快照，可还原当时版本。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, VersionPublishPayload
from app.services.queryboard import QueryboardService

router = APIRouter(prefix="/api/queryboard", tags=["聚合查询"])

service = QueryboardService()

LIST_FIELDS = ["模板编号", "指标名称", "维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源"]
STATUSES = ["草稿", "模板验证中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、模板验证中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤聚合查询列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/audit")
def list_audit() -> dict[str, Any]:
    """查询模板侧审计流水，含每次状态/版本变化时的模板版本快照。"""
    return {"module": "queryboard", "items": service.list_audit()}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合查询清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "queryboard", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/audit")
def get_entry_audit(entry_id: int) -> dict[str, Any]:
    """单条模板的审计流水，用于还原该模板每个版本当时的字段口径。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return {"module": "queryboard", "entry_id": entry_id, "items": service.list_audit(entry_id)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条查询模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条查询模板执行提交模板验证、启用模板、废弃模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/versions", response_model=ActionResult)
def publish_version(entry_id: int, payload: VersionPublishPayload) -> ActionResult:
    """替换模板版本：expected_version 与当前版本不一致时拒绝（并发只接受一次有效结果）。"""
    values = dict(payload.values)
    values["expected_version"] = payload.expected_version
    entry, message = service.publish_version(entry_id, values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
