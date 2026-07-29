# App 目录结构

P3-P6 完成后，`app/` 按业务边界组织实现代码：

```text
app/
├─ main.py                 # FastAPI 应用工厂与 Router 组合
├─ container.py            # Store / Service 依赖构造
├─ api/                    # HTTP 处理器、依赖和领域 Router
├─ core/                   # 配置、SQLite、迁移、事件、韧性和可观测性
├─ integrations/           # iFinD 与 LLM 外部集成
├─ market/                 # 行情仓储、更新和复权数据
├─ research/               # 证据、公告、文档、财务和公司研究
├─ quant/                  # 因子、模型、Shadow、评价、回测和 QuantStore
├─ decision/               # 决策案例、结果归因和组合规划
├─ paper/                  # 账户、基准、执行、看板和策略 Facade
├─ thesis/                 # Thesis Store 与持续监控
└─ workflows/              # 跨领域的每日批次编排
```

## 数据边界

- `ResearchStore` 只负责研究、证据、决策和研究运行。
- `QuantStore` 直接管理量化数据库，不继承 `ResearchStore`。
- `SQLiteStore` 作为模拟盘数据库连接边界。
- `AppContainer` 是正式运行时唯一的服务装配入口。

## 兼容路径

根目录下原有模块暂时保留为模块别名，例如 `app.research_store` 会直接解析到
`app.research.store`。它们不再包含实现代码，用于保证现有脚本和外部调用平稳迁移。
兼容别名将在 P7 更新全部脚本和文档后统一移除。

## 模拟盘职责

- `paper/store.py`：Schema、账户、策略和基础持久化。
- `paper/benchmarks.py`：基准同步与收益对齐。
- `paper/execution.py`：报价、订单审核、成交、持仓与 NAV。
- `paper/dashboard.py`：账户看板聚合。
- `paper/service.py`：稳定的 `PaperExecutionService` / `PaperTradingService` Facade。
- `workflows/daily_batch.py`：每日跨领域批次，不属于模拟盘领域内部。
