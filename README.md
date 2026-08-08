# A 股量化研究与模拟交易系统

这是一个面向单用户本地环境的 A 股研究与模拟交易工作台。后端使用 FastAPI，前端使用 Vue 3，数据主要保存在本地 SQLite 数据库中。

当前项目聚焦四条可运行链路：

- 本地行情、股票池、公告和财务证据管理；
- 因子维护与历史回测结果查看；
- 使用 OpenAI-compatible 大模型生成 Python 策略；
- LightGBM 或代码策略驱动的模拟组合研究、人工审批与模拟成交。

项目只执行模拟交易，不连接真实券商，也不构成投资建议。

## 当前功能

### 模拟盘

- 创建模拟账户并选择内置策略或已生成的代码策略；
- 运行每日完整研究批次；
- 查看当前提案、历史提案、历史成交、持仓和组合净值；
- 每条交易提案必须由用户单独批准或拒绝；
- 仅在交易时段使用 iFinD 实时行情执行模拟撮合；
- 支持 T+1、整手、可用现金、可卖数量、涨跌停和费用约束。

### 策略编辑器

用户只需要填写策略名称、股票池和策略需求。系统调用已配置的大模型生成 `generate_signals(context)`，完成基础代码检查后保存到策略仓库。

策略元数据保存在 `quant_research.db`，代码文件保存在：

```text
data/strategy_runs/{strategy_id}/v1/strategy.py
```

代码策略在独立 Python 子进程中运行，包含 AST 限制、超时和输出校验，但这不是容器或虚拟机级安全沙箱，只适合本地受信任用户。

### 量化研究

量化研究数据库只保留当前因子、策略、回测和迁移记录，不再维护旧因子版本、发布审批和多层审查历史。

前端当前支持：

- `/factor-development`：创建和查看当前因子；
- `/strategies`：生成策略并查看策略仓库；
- `/backtest`：查看最近一次因子或策略回测结果。

当前 `/backtest` 是结果查看界面，不是所有代码策略都已接入的一键回测入口。

### 公司研究

项目仍保留公告、PDF 文档、财务事实、公司研究、长期论点、决策案例和证据验收功能。研究文本可以使用大模型，但行情计算、证据日期和交易状态由后端确定性代码控制。

## 系统结构

```text
app/
|- api/          FastAPI 路由
|- core/         配置、SQLite、迁移、运行事件和韧性机制
|- data/         iFinD 数据接口
|- market/       行情、复权数据和股票池
|- research/     公告、财务事实、证据和公司研究
|- quant/        因子、LightGBM 推理和临时运行数据
|- backtest/     共享日频回测内核
|- llm/          OpenAI-compatible 模型适配和策略生成
|- strategy/     策略存储、代码检查和运行适配
|- paper/        模拟账户、提案、审批、成交、持仓和净值
|- decision/     决策案例和结果评价
|- thesis/       长期论点
`- workflows/    每日完整批次编排

frontend/src/
|- api/          前端 API 客户端和类型
|- components/   公共组件
|- features/     模拟盘、研究、量化、策略和审计页面
|- router/       页面路由
`- stores/       Pinia 状态
```

`app/container.py` 是后端依赖的统一装配入口，`app/main.py` 创建 FastAPI 应用。`example/` 仅保存外部架构参考，不参与正式运行。

更详细的模块边界见 [docs/APP_STRUCTURE.md](docs/APP_STRUCTURE.md) 和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 数据与数据库

运行数据库和模型文件位于 `data/`，默认不提交到 Git。

| 文件或目录 | 用途 | 是否自动创建 |
|---|---|---|
| `stock_data.db` | 不复权日线和行情更新状态 | 否，需准备或更新 |
| `stock_data_qfq.db` | 前复权研究行情，供 Alpha158、LightGBM 和回测使用 | 否，需准备或构建 |
| `stock_data_hfq.db` | 后复权研究行情 | 可选 |
| `stock_pool.db` | 股票池和日期化成分 | 空库可创建，但正式运行应同步数据 |
| `research_state.db` | 公告、证据、研究、论点、决策和运行事件 | 是 |
| `quant_research.db` | 当前因子、策略和回测 | 是 |
| `paper_trading.db` | 模拟账户、批次、提案、成交、持仓和净值 | 是 |
| `model_artifacts/` | LightGBM 模型、manifest 和相关产物 | 否，默认链路必须提供 |
| `strategy_runs/` | 已启用策略的代码副本 | 可由策略记录重新生成 |
| `runtime_tmp/` | 当前进程和批次的临时数据库、评分 CSV | 是，自动清理 |

`quant_research.db` 当前固定为四张表：

```text
quant_factor
quant_strategy
quant_backtest
schema_migration
```

`quant_runtime.db` 已退役。应用启动后会在 `data/runtime_tmp/` 创建进程级临时运行目录；LightGBM 对股池证券的评分先写入批次内的 `lightgbm_scores.csv`，排序和研究完成后删除，不长期保存每日全量评分。

数据库 Schema 在 Store 初始化时通过幂等迁移创建或更新。不要手工修改表结构，测试也不应直接使用 `data/*.db`。

## 每日模拟组合链路

网页首页的“运行完整批次”依次执行：

```text
行情检查与更新
  -> 因子快照
  -> LightGBM 或代码策略评分
  -> 候选研究
  -> 持仓复核
  -> 交易提案
```

对应的六个批次步骤为：

1. `market_data`
2. `factor_snapshot`
3. `current_shadow`
4. `candidate_research`
5. `holdings_review`
6. `order_proposals`

批次按账户、研究日、批次版本和配置哈希复用。运行失败后可重新执行，已完成步骤会保留，失败步骤及其后续步骤会重试。完整批次只生成 `proposed` 提案，不会自动批准或成交。

### 人工审批与成交

标准流程是：

1. 收盘后运行完整批次并生成当日研究提案；
2. 用户逐条批准或拒绝；
3. 下一交易日开市后点击“执行模拟撮合”；
4. 在历史成交、持仓和净值区域核对结果。

实时模拟撮合只允许上海时区交易日的两个时段：

```text
09:30-11:30
13:00-15:00
```

撮合只读取账户最新有效研究批次中状态为 `approved` 的订单。新研究日批次会替代旧批次中尚未成交的提案，因此不会把多天未执行的批准订单一起累积到后续交易日。

成交还要求行情交易日晚于提案研究日，且报价时间晚于人工批准时间。卖单先于买单执行，成交后现金、订单状态、持仓和净值统一写入 `paper_trading.db`。

## 环境要求

- Windows 或能够运行项目依赖的 Python 主机；
- Conda；
- Python 3.11；
- Node.js 22；
- iFinD Python SDK 和有效账号；
- OpenAI-compatible 模型接口；
- 当前 LightGBM 模型产物。

项目约定使用 `quant` Conda 环境：

```powershell
conda env create -f environment.yml
conda activate quant
python scripts/check_environment.py
```

如果环境已经存在：

```powershell
conda activate quant
pip install -r requirements.lock
python scripts/check_environment.py
```

## 配置

在仓库根目录创建 `.env`，可从 `.env.example` 开始配置：

```dotenv
IFIND_USER=你的账号
IFIND_PASSWORD=你的密码

TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_LLM_BACKEND_URL=https://你的模型接口/v1
OPENAI_COMPATIBLE_API_KEY=你的APIKey
TRADINGAGENTS_QUICK_THINK_LLM=你的模型名称
TRADINGAGENTS_DEEP_THINK_LLM=你的模型名称
```

当前策略生成使用 `TRADINGAGENTS_QUICK_THINK_LLM`。模型供应商只要提供兼容 `/chat/completions` 的 JSON 响应接口即可。

`.env` 包含凭据，不能提交到 GitHub，也不要输出到日志。

## 首次启动

安装并构建前端：

```powershell
cd frontend
npm ci
npm run build
cd ..
```

启动应用：

```powershell
conda activate quant
python run.py
```

访问：

```text
http://127.0.0.1:8000
```

如果 `frontend/dist/` 不存在，后端不会提供完整生产页面，需要先运行前端构建。

## 部署到其他主机

Git 仓库负责传输源代码、迁移、前端源码、测试和配置样例。以下内容不要提交到 GitHub，应通过受控方式手工复制：

- `.env`
- `data/stock_data.db`
- `data/stock_data_qfq.db`
- `data/stock_pool.db`
- `data/model_artifacts/`

需要保留现有业务状态时，再复制：

- `data/research_state.db`
- `data/quant_research.db`
- `data/paper_trading.db`
- `data/documents/`
- `data/strategy_runs/`

`paper_trading.db` 可以由应用自动创建；不复制它会得到一个新的模拟盘，并在首次访问时创建默认的“每日模拟组合”账户。若要保留已有账户、提案、成交、持仓和净值，则必须复制原数据库。

推荐迁移顺序：

1. 在目标主机拉取仓库；
2. 创建 Conda 环境并安装前端依赖；
3. 手工放置 `.env`、行情库、股票池和模型产物；
4. 按需复制三个状态数据库和文档；
5. 执行 `python scripts/check_environment.py`；
6. 执行 `npm run build`；
7. 执行 `python run.py`。

完整运维说明见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 数据更新

不复权日线先检查再更新：

```powershell
python scripts/update_stock_data.py --dry-run
python scripts/update_stock_data.py
```

同步股票池：

```powershell
python scripts/sync_stock_pools.py --as-of YYYY-MM-DD
```

构建前复权研究库：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment forward --universe csi300 --dry-run
python scripts/build_adjusted_stock_data.py --adjustment forward --universe csi300
```

同步公告和研究数据：

```powershell
python scripts/sync_research_data.py --bootstrap
python scripts/sync_research_data.py --code 601888.SH --end-date YYYY-MM-DD --download-limit 5
```

数据更新脚本涉及写库时应先使用 `--dry-run`。前复权历史可能随公司行为变化，不应把它当作永远不变的增量序列。

## 测试

后端：

```powershell
conda activate quant
pytest -q
```

前端：

```powershell
cd frontend
npm run typecheck
npm run test
npm run build
```

端到端测试需要先启动后端服务：

```powershell
cd frontend
npm run test:e2e
```

GitHub Actions 会执行后端测试、前端测试、生产构建和 Playwright 端到端测试。

## 当前限制

- 项目面向单用户本地部署，没有登录、租户隔离或正式任务队列；
- SQLite 不适合多主机同时写入同一状态库；
- iFinD 是主要行情和实时报价来源；
- LightGBM 模型文件是默认每日研究链路的必要外部产物；
- 代码策略执行只提供基础限制，不应运行不可信第三方代码；
- 回测结果不能代表未来收益，模拟成交也不等同于真实市场成交。

## 文档

- [系统架构](docs/ARCHITECTURE.md)
- [项目结构](docs/APP_STRUCTURE.md)
- [运行与维护](docs/OPERATIONS.md)
- [iFinD 接口参考](docs/IFIND_API_REFERENCE.md)
