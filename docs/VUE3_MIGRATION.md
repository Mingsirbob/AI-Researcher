# Vue 3 前端状态

> 当前状态：迁移已完成。本文只保留现行前端结构和维护方式，不再维护 P0-P5 阶段验收记录。

Vue 3 是项目唯一生产前端。旧 Vanilla 页面和 `/legacy` 不属于当前运行或回滚方案；生产回滚应恢复一套相互匹配的前端、后端和数据库版本。

## 1. 运行结构

```text
frontend/src/
|- main.ts
|- App.vue
|- router/index.ts
|- api/
|- stores/
|- components/
`- features/
   |- paper/
   |- company/
   |- lab/
   |- strategy/
   `- audit/
```

当前页面：

| 路由 | 页面 |
|---|---|
| `/` | 模拟盘 |
| `/company` | 公司分析 |
| `/theses` | 长期论点 |
| `/quant` | 数据与特征 |
| `/factor-development` | 因子开发 |
| `/factor-evaluation` | 因子评价 |
| `/backtest` | 策略回测与 Shadow |
| `/factor-library` | 因子库 |
| `/strategies` | 策略编辑器与策略仓库 |
| `/decisions` | 决策案例 |
| `/acceptance` | 质量验收 |

## 2. 构建与开发

```powershell
cd frontend
npm ci
npm run typecheck
npm run test
npm run build
```

本地热更新：

```powershell
npm run dev
```

FastAPI 生产入口读取 `frontend/dist/`。构建缺失时应修复构建，不应启用另一套页面掩盖部署问题。

## 3. API 合同

- 请求封装和领域类型位于 `frontend/src/api/`。
- `openapi.generated.ts` 从运行中的后端 OpenAPI 文档生成，不应手工编辑。
- 后端接口变更后执行：

```powershell
npm run types:api
npm run typecheck
```

- 尚未声明完整 `response_model` 的后端接口仍可能依赖手写领域类型；不能把生成类型当作完整运行时校验。
- 页面 mutation 成功后应失效或刷新相关查询，尤其是账户切换、批次、订单审批、撮合和策略发布。

## 4. 当前产品边界

- 模拟盘审批和撮合是显式用户操作；前端不得通过轮询自动批准或自动成交。
- `/strategies` 面向“名称、股票池、需求”的简化创建流程，并展示已发布策略及存储位置。
- `/backtest` 已覆盖因子回测和既有 Shadow 数据；任意大模型代码策略尚未统一接入综合回测。
- LightGBM、线性多因子和 Python 代码策略是不同信号来源，不应在页面文案中硬编码某一种为所有账户的固定策略。
- 页面展示的批次、提案、成交、持仓和 NAV 均来自后端数据库，不应在前端构造替代业务状态。

## 5. 测试要求

```powershell
npm run test
npm run build
```

服务已启动时运行：

```powershell
npm run test:e2e
```

关键改动至少覆盖对应组件单测；模拟盘、路由或跨页工作流改动还应覆盖 Playwright 桌面和移动视口。文档不保存固定测试数量，以当前命令和 CI 为准。
