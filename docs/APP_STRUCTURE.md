# 项目结构

本文描述当前代码布局和所有权边界。运行时依赖统一在 `app/container.py` 组装，领域模块不应自行创建另一套全局服务。

## 根目录

```text
AI Researcher/
|- app/                  # FastAPI 后端
|- frontend/             # Vue 3 前端
|- scripts/              # 数据更新、审计和离线研究脚本
|- tests/                # Python 测试
|- docs/                 # 当前文档、专项参考和历史记录
|- data/                 # 运行数据库、文档、模型和策略产物（Git 忽略）
|- example/              # 外部架构参考代码，不属于正式运行链路
|- run.py                # 本地服务入口，默认 127.0.0.1:8000
|- environment.yml       # Conda 环境定义
|- requirements.lock     # Python 锁定依赖
`- .env.example          # 配置示例，不包含真实凭据
```

`example/` 中的 Vibe-Trading 模块仅作迁移和设计参考。当前应用不会从该目录导入 Agent、Swarm、Skills 或回测实现。

## 后端

```text
app/
|- main.py               # FastAPI 应用工厂和静态前端挂载
|- container.py          # Store、Service、外部适配器统一装配
|- api/
|  |- routers/           # securities/research/quant/decisions/paper/strategy 等路由
|  |- dependencies.py    # 从 AppContainer 获取依赖
|  `- handlers.py        # 共享处理器和每日批次步骤实现
|- core/                 # 配置、SQLite、迁移、运行事件、韧性和可观测性
|- data/                 # iFinD 统一数据层
|- market/               # 本地行情读取、增量更新、复权同步和股票池
|- research/             # 公告、PDF、财务事实、Evidence、公司研究和验收
|- quant/                # 因子、模型、评价、因子回测、发布和 QuantStore
|- backtest/             # 可复用的日频回测、A 股规则、约束和信号合同
|- strategy/             # 策略注册、编译、版本、代码校验和受限运行
|- llm/                  # 代码策略生成所需的模型适配和 Prompt
|- decision/             # 决策案例、组合规划和结果评价
|- paper/                # 模拟账户、订单、持仓、净值、审批、撮合和调度
|- thesis/               # 长期论点与监控状态
`- workflows/            # 跨领域每日研究批次状态机
```

### 关键边界

- `app/data/ifind.py` 是当前 iFinD 入口；旧 `app/integrations/ifind.py` 不再存在。
- `app/integrations/llm.py` 服务于研究报告等既有模型调用；`app/llm/` 服务于策略代码生成。两者尚未合并为通用 Agent 平台。
- `app/backtest/` 提供共享撮合内核；因子回测的业务持久化仍由 `app/quant/factor_backtest.py` 负责。
- `app/strategy/` 负责策略版本，`app/paper/` 只保存账户部署关系和执行状态。
- `app/workflows/daily_batch.py` 只编排步骤，不拥有行情、研究、量化或模拟盘领域数据。

## 前端

```text
frontend/src/
|- main.ts               # Vue 启动入口
|- App.vue               # 应用框架和主导航
|- router/index.ts       # 根级路由
|- api/                   # HTTP 客户端、领域类型和生成的 OpenAPI 类型
|- stores/               # Pinia 客户端状态
|- components/           # 可复用展示组件
|- features/
|  |- paper/             # 模拟盘
|  |- company/           # 公司研究和 Thesis
|  |- lab/               # 数据、因子、回测和 Shadow
|  |- strategy/          # 策略编辑器与策略仓库
|  `- audit/             # 决策案例与证据验收
`- styles/               # 全局样式
```

Vue 3 是唯一生产前端。`frontend/dist/` 由构建生成，不应手工修改或提交。

## 数据与产物

```text
data/
|- stock_data.db         # 不复权原始日线，应用只读
|- stock_data_qfq.db     # 前复权研究行情
|- stock_data_hfq.db     # 后复权研究行情（按需构建）
|- stock_pool.db         # 股票池定义和日期化成分快照
|- research_state.db     # 研究证据、文档、Thesis、决策和运行事件
|- quant_research.db     # 因子、模型、回测和策略版本
|- paper_trading.db      # 模拟账户、批次、订单、持仓、净值和行情快照
|- documents/            # 下载文档及解析产物
|- model_artifacts/      # 模型产物
|- current_shadow/       # Current Shadow 文件产物
|- strategy_runs/        # 已发布策略的可审计文件副本
`- skills/               # 面向模型的本地数据库使用说明
```

数据库与运行产物由 Git 忽略。测试必须使用临时数据库和 fixture，不应在 `data/` 下创建 pytest 运行目录。
