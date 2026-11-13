"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

聚合查询下钻的模板/路径协同需要以下能力，统一在这一层提供：
- ``lock``：进程内互斥锁，保证模板与路径的跨表写入串行化，并发请求只接受一次有效结果；
- ``transaction``：跨表事务，任一写入失败整体回滚，两侧都不会进入半成品状态；
- ``audit``：审计流水，记录模板与路径共同变化时的模板版本快照；
- ``query_cache``：下钻聚合查询缓存，模板废弃后保留历史缓存但拒绝新请求。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 审计与缓存不属于业务表，独立存放，避免被列表/导出接口扫到。
        self._tables.setdefault("audit", [])
        self._lock = threading.RLock()
        # key: (path_id, template_id, template_version, level) -> 缓存条目
        self.query_cache: dict[tuple[Any, ...], dict[str, Any]] = {}

    @property
    def lock(self) -> threading.RLock:
        """跨表写入统一走这把锁，保证模板版本替换与路径启用互斥。"""
        return self._lock

    def reset(self) -> None:
        """恢复到种子数据并清空审计/缓存，主要供测试隔离使用。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
            self._tables.setdefault("audit", [])
            self.query_cache = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """跨表事务：先快照、再写入，异常时整体回滚。

        模板与路径任一侧写入失败，都通过抛异常触发回滚，保证不会残留
        「模板已改、路径半成品」之类的中间状态。锁是可重入的，服务层
        已经持锁时也可以安全进入。
        """
        with self._lock:
            snapshot = {name: [dict(row) for row in rows] for name, rows in self._tables.items()}
            cache_snapshot = dict(self.query_cache)
            try:
                yield
            except Exception:
                self._tables = snapshot
                self.query_cache = cache_snapshot
                raise

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name != "audit")

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def append_audit(self, record: dict[str, Any]) -> dict[str, Any]:
        """追加一条审计流水；审计只追加不修改，失败不应回滚业务时由调用方决定。"""
        rows = self.rows("audit")
        record = dict(record)
        record["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append(record)
        return record

    def audit_rows(
        self,
        *,
        module: str | None = None,
        entry_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.rows("audit")
        if module:
            rows = [row for row in rows if row.get("module") == module]
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("entry_id", 0)) == entry_id]
        return rows

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
