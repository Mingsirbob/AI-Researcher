# Vue 3 渐进迁移

## 当前入口

- `/`：当前以 `307` 重定向到 Vue 3 的 `/next/paper`。
- `/next`：Vue 3 生产构建入口。

FastAPI 继续提供全部 `/api` 接口。Vue 构建产物位于 `frontend/dist`，仅在该目录存在时挂载 `/next/assets`。

## 已迁移范围

| 阶段 | Vue 路由 | 已迁移主流程 |
|---|---|---|
| P0 | 全局 | 后端回归基线、双入口观察与可回滚切换 |
| P1 | 全局 | Vue Router、Pinia UI 状态、Vue Query 服务端状态、统一 API 错误、响应式壳层 |
| P2 | `/acceptance`、`/factor-library` | 验收运行与历史、因子注册表、生命周期、每日快照和排名 |
| P3 | `/quant`、`/factor-development`、`/factor-evaluation`、`/backtest` | 特征快照、候选池、因子草稿、评价和策略回测 |
| P4 | `/company`、`/theses` | 行情分析、证据、Claim、公司研究、文档问答、运行历史和 Thesis 监控 |
| P5 | `/decisions`、`/paper` | 决策案例、规则门禁、人工审批、结果评价、每日批次、订单审批和持仓 |

P0-P4 的功能对等和工程化工作已在 2026-07-27 完成。Vue 现已覆盖财务变化模板、财务事实刷新、助手历史回放、Thesis 状态与 Claim 确认、量化阈值筛选、因子发布审批、回测净值、模型/Shadow 审计、冻结案例合同、成熟进度、跨案例归因、Paper 研究目标、决策依据和指数基准比较。

本轮已关闭剩余差异：Paper 自动恢复完整批次、运行事件时间线和多基准曲线；公司主数据时点查询与 BM25 来源链接；验收金标逐项比对和失败明细；因子发布限制、哈希、门禁版本和独立审批历史；决策成熟扫描、人工勾选和批量评价结果；指数成分历史合同和行业/市值中性化质量隔离。核心页面已移除显式 `any`，表单标签、图标按钮和决策抽屉补齐了可访问性语义。

P5-B 已完成真实 iFinD/LLM 环境验收。2026-07-29 完成观察门禁收尾后，Vue 成为生产入口；Vanilla HTML/CSS/JavaScript 页面及 `/legacy` 已退役。

工程收尾同时完成：生成的 `openapi.generated.ts` 已通过 API 客户端的 `OpenApiSchema` 和 `openApiJsonBody` 进入页面请求链路，模拟盘、因子发布、Thesis 监控和决策评价的关键写请求由生成 schema 约束。响应类型暂不伪装为完整 OpenAPI 合同；尚未声明 FastAPI `response_model` 的接口继续使用页面领域类型。`PaperView` 的收益对比、订单和持仓，`CompanyView` 的证据检索，以及 `FactorLibraryView` 的发布审查已拆为独立组件，查询和 mutation 仍由父页面持有。

## P5 上线与回滚

Vue 是唯一生产前端。构建存在时，`/` 以 `307` 重定向到 `/next/paper`；构建缺失时根入口返回 503，不使用另一套前端掩盖部署错误。当前状态可通过 `/api/health` 的 `frontend` 字段检查，正常值为 `configured_default=vue`、`active_default=vue`、`vue_built=true`。

生产部署必须先执行 `npm ci && npm run build`，再启动或重启 FastAPI。回滚使用上一完整 Git 提交或部署制品，同时恢复与该版本匹配的前后端，不再通过运行时开关切换前端实现。

P5-A 和 P5-B 已在 `codex/vue-p5` 分支完成：入口观察、健康状态、真实数据合同、完整研究批次、安全写操作、最终切换和 Vanilla 退役均已验收。当前标准服务端口为 8000，使用 quant 环境运行。

## P5-B 最终验收记录

- 切换前 SQLite 在线备份：`data/backups/p5b-before-live-validation/research_state.db`
- 备份 SHA256：`7DCDB3547773661C98F20034079AA3D3BA7729233C11A5FCEEBFAD1DD6393AFF`
- 2026-07-24 因子快照：`dd9240a5-b3ea-4cc7-a2e4-a6b27d63a1aa`，通过 4127，只排除 770。
- 2026-07-24 Current Shadow：`e731b80a29374ed2cadc97efcb7a63ca76ed5c9229f082770074ac3baf4dec1e`。
- 完整批次：`4ce853dd-c899-451f-85e6-c6648c16a419`，六个步骤全部完成；候选研究完成 10、失败 0，持仓复核 5，生成订单提案 4，状态保持 `awaiting_review`。
- 审计事件：共 136 条，序号 1-136 连续，六个步骤均有 `step_started` 和 `step_completed`，`step_failed=0`，最终事件为 `task_completed`。
- Evidence Acceptance：`f162d3ee-1e34-418a-bb7a-16dbda76ff40`，状态 `passed`，失败指标 0。
- 决策成熟扫描：`ready=0`、`pending=5`，因此没有执行批量结果评价。
- 全程没有自动批准或拒绝因子、Thesis Claim 或 Paper 订单，也没有执行模拟成交。
- 默认入口验收：`/` 返回 `307 -> /next/paper`，`/next/paper` 返回 200；浏览器控制台无错误，页面无横向溢出。
- 2026-07-29 最终观察期复验：服务由 `C:\Users\13056\anaconda3\envs\quant\python.exe` 在 8000 端口启动，健康状态为 `configured_default=vue`、`active_default=vue`、`vue_built=true`。
- Vanilla 退役验收：`/legacy` 返回 404，Vue 构建缺失时根入口返回 503，回滚责任转移到完整 Git/部署版本。

## 多账户多策略

每日决策工作区现已支持创建和切换多个隔离模拟账户。账户在创建时冻结策略、初始资金和基准，持仓、订单、成交、净值、批次及基准历史继续按 `account_id` 隔离。

内置策略：

- `lightgbm_shadow_v1`：使用通过数据合同的 Current Shadow LightGBM 截面排名，默认目标总仓位 50%。
- `multifactor_linear_v1`：使用同日因子快照的 20/60 日动量、低波动、最大回撤和价格位置做截面线性合成，默认目标总仓位 80%。

两类策略仅替换信号生成合同，共享量化门禁、研究评估、组合约束、人工审批、T+1、费用、撮合与净值引擎。旧账户通过 `0013_paper_strategies` 自动绑定 LightGBM 策略，不修改历史订单或持仓。

## P0-P4 完成记录

| 工作包 | 完成内容 |
|---|---|
| P0 功能对等 | 旧版关键审计和人工门禁均已接入 Vue；因子评价保留明确的因子选择 |
| P1 研究闭环 | 增加案例成熟扫描、批量评价和证券主数据时点查询 |
| P2 量化真实性 | 增加成分快照查询、行业/市值 OLS 中性化；交易费用与 T+1 约束沿用既有模拟盘合同 |
| P3 证据能力 | 增加 FTS5/BM25 搜索及 LIKE 回退，财务事实继续绑定报告期、比较期、文档哈希和页码 |
| P4 工程化 | 增加 OpenAPI TypeScript 契约、Vitest、Playwright 双视口回归和 GitHub Actions 质量门禁 |

最终验证基线（2026-07-29）：quant 环境 Python `154 passed`、Vue 单元测试 `7 passed`、Playwright `42 passed`，`npm run typecheck` 和 `npm run build` 通过。Playwright 包含桌面和移动路由回归，以及回测指标、Paper 研究候选、多账户创建、订单批准/拒绝与实时撮合、因子发布批准/拒绝、决策批量评价、Thesis Claim 确认与删除确认、公司 BM25 搜索的路由模拟测试；模拟测试不会写入真实数据库。

## 本地开发

后端：

```powershell
conda activate quant
python run.py
```

前端开发服务器：

```powershell
cd frontend
npm install
npm run dev
```

Vite 使用 `http://127.0.0.1:5173/next/`，并将 `/api` 代理到 `http://127.0.0.1:8000`。

生产构建：

```powershell
cd frontend
npm ci
npm run build
```

构建完成后，通过 `http://127.0.0.1:8000/next` 访问 Vue 版本。

针对已由 quant 环境启动在其他端口的服务，可覆盖 Playwright 基址：

```powershell
$env:PLAYWRIGHT_BASE_URL='http://127.0.0.1:8001'
npm run test:e2e
```

## 验收门禁

- `npm run build` 必须同时通过 `vue-tsc` 和 Vite 构建。
- `npm run test` 必须通过 Vue 单元测试。
- `npm run test:e2e` 必须通过桌面和移动两个项目。
- Python 回归测试必须全部通过。
- 根入口必须重定向到 Vue，10 条 Vue 路由必须返回 200，`/legacy` 必须返回 404。
- 浏览器不得出现 page error、console error 或静态资源加载失败。
- 1440px 桌面和 390px 移动视口不得发生页面级横向溢出。
- 离开页面后，批次轮询和 ResizeObserver 必须被清理。
- 所有审批、快照、研究和模拟成交仍由明确的用户命令触发。
