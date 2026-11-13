# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 测试

```bash
make install-dev   # 安装后端验收测试依赖（httpx）
make test          # 后端 unittest + 前端类型检查
```

后端用例在 `backend/tests/`，基于标准库 `unittest`；`test_drill_api.py` 通过
`httpx.ASGITransport` 走真实路由 → 服务 → 存储调用链，无需起 socket。

## 聚合查询下钻的模板准入与版本协同

聚合查询（queryboard，查询模板）与多维下钻（datadrill，下钻路径）围绕同一
模板版本共同变化，规则集中在 `app/services/drill_domain.py`：

- 模板状态机：只能按「草稿 → 模板验证中 → 已启用 → 已废弃」逐级推进，禁止跳级/回退。
- 路径状态机：只能按「草稿 → 路径评审中 → 已启用 → 已关闭」逐级推进。
- 启用准入：路径启用前必须同时满足
  1. 关联模板存在且处于「已启用」；
  2. 路径每一层维度都能在模板维度字段中找到来源；
  3. 各层度量口径一致，且与模板度量字段完全相同；
  4. 维度层数与度量层数对齐，起始维度与第一层一致。

  任一不满足，路径停在「路径评审中」并逐条返回原因，不产生半成品路径。
- 版本绑定：启用时把模板版本快照固化到路径与审计；模板换版（带
  `expected_version` 乐观锁，并发替换只接受一次有效结果）后路径保留旧版本，
  版本漂移期间新聚合请求被阻止，需调 `rebind-version` 重新对齐。
- 模板废弃：在同一事务内把失效来源写到所有关联路径；历史下钻层级与查询缓存
  保留，但新的聚合请求一律拒绝，路径详情展示失效来源。
- 事务原子性：跨表写入走 `store.transaction()`，模板与路径任一侧写入失败都整体
  回滚，两侧都不会进入可用状态。
- 批量启用：`POST /api/datadrill/batch-actions` 支持多选一次提交，先做整组引用
  校验，全部通过才整体生效；结论逐条返回，任一条失败则整组不生效。
- 审计：模板侧 `/api/queryboard/{id}/audit`、路径侧 `/api/datadrill/{id}/audit`
  保存每次变化的模板版本快照，可还原当时的模板版本、维度与度量口径。

领域：数据工程
子方向：数据分析与数据应用
