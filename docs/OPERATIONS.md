# 运行与维护

> 更新日期：2026-08-07
> 适用范围：本地单用户 `quant` Conda 环境

## 1. 环境与配置

```powershell
conda activate quant
python scripts/check_environment.py
```

Python 依赖以 `requirements.lock` 和 `scripts/check_environment.py` 为准。前端使用 `frontend/package-lock.json`。

从 `.env.example` 配置本地 `.env`。不要把 `.env`、账号或 Token 写入源码、日志和文档。

常用配置：

```dotenv
IFIND_USER=...
IFIND_PASSWORD=...

TRADINGAGENTS_LLM_BACKEND_URL=...
OPENAI_COMPATIBLE_API_KEY=...
TRADINGAGENTS_QUICK_THINK_LLM=...
TRADINGAGENTS_DEEP_THINK_LLM=...

STRATEGY_EXECUTION_TIMEOUT_SECONDS=3
```

当前策略生成直接复用上述 OpenAI-compatible 配置，包括项目 `.env` 中已有的小米模型配置。应用只在 URL、Key 和 quick model 都存在时启用“大模型生成策略”。

## 2. 构建与启动

首次安装或依赖变化后构建前端：

```powershell
cd frontend
npm ci
npm run build
cd ..
```

启动服务：

```powershell
python run.py
```

访问 `http://127.0.0.1:8000/`。如果 `frontend/dist/` 缺失，生产入口会返回错误，应重新构建前端，不应恢复旧前端作为回退。

开发前端可单独运行：

```powershell
cd frontend
npm run dev
```

## 3. 数据库与更新责任

| 数据 | 数据库 | 更新方式 |
|---|---|---|
| 不复权日线 | `data/stock_data.db` | `scripts/update_stock_data.py` |
| 前/后复权日线 | `data/stock_data_qfq.db`、`stock_data_hfq.db` | 构建/同步脚本 |
| 股票池 | `data/stock_pool.db` | `scripts/sync_stock_pools.py` |
| 研究状态 | `data/research_state.db` | 应用迁移和研究服务 |
| 因子、策略、回测 | `data/quant_research.db` | 精简量化目录与策略服务 |
| LightGBM 模型 | `data/model_artifacts/*/manifest.json` 与模型文件 | 文件清单和模型产物 |
| 每日模型评分 | `data/runtime_tmp/` 下的批次临时 CSV | 批次结束自动删除，不留历史评分 |
| 模拟盘 | `data/paper_trading.db` | 应用迁移和模拟盘服务 |

状态库 Schema 在应用启动和 Store 初始化时执行幂等迁移。不要手工修改表结构，也不要把运行数据库用于测试 fixture。

### 3.1 不复权日线

先查看计划，再执行写入：

```powershell
python scripts/update_stock_data.py --dry-run
python scripts/update_stock_data.py
```

指定截止日：

```powershell
python scripts/update_stock_data.py --end-date 2026-08-07
```

`stock_data.db` 固定使用 iFinD `THS_HD` 空参数的不复权口径。更新器按交易日补缺，校验响应结构、OHLC、VWAP、成交量和异常跳变；禁止把 `CPS:1` 或 `CPS:2` 写入该库。

### 3.2 股票池

```powershell
python scripts/sync_stock_pools.py --as-of 2026-08-07
python scripts/sync_stock_pools.py --pool-id csi300 --as-of 2026-08-07
```

成分写入日期化快照。若 iFinD 只能返回当前成分，回填历史研究会有幸存者偏差，不能把当前股池当成历史真实成分。

### 3.3 复权研究库

检查和构建指定股池：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment forward --universe csi300 --dry-run
python scripts/build_adjusted_stock_data.py --adjustment forward --universe csi300
python scripts/audit_adjusted_stock_data.py
```

构建当前非 ST A 股前复权库：

```powershell
python scripts/build_all_a_qfq.py --end-date 2026-08-07 --dry-run
python scripts/build_all_a_qfq.py --end-date 2026-08-07
```

构建过程使用临时库并在完整性检查通过后替换目标库。前复权历史会随公司行为变化，不应像不复权原始库一样简单追加并永久视为不变。

### 3.4 公告与研究数据

```powershell
python scripts/sync_research_data.py --bootstrap
python scripts/sync_research_data.py --code 601888.SH --end-date 2026-08-07 --download-limit 5
```

公告 PDF、hash、页码文本和财务事实写入研究库及 `data/documents/`。下载成功不等于文本层或财务抽取通过，必须查看质量状态。

## 4. 每日研究批次

网页模拟盘顶部的“运行完整批次”是标准入口。批次按以下顺序执行：

```text
行情 -> 因子快照 -> Current Shadow/策略信号
     -> 候选研究 -> 持仓复核 -> 交易提案
```

运行规则：

- 研究日解析为请求日期之前最近的有效交易日。
- 同账户、同研究日、同批次版本、同配置重复运行会复用原批次。
- 已完成步骤会被保留；失败后再次运行只重试失败及后续步骤。
- 服务重启会把运行中的批次标记为失败，用户可重新运行恢复。
- 研究日早于账户最近成交日时拒绝创建订单批次，避免用当前持仓回溯历史。
- 完整批次只生成提案，不批准、不成交。

### 自动批次

账户策略若配置 `auto_run=true`、`schedule=daily` 和 `run_time`，后台调度器会在交易日指定时间后尝试运行一次完整批次。自动批次仍需要人工逐单审批，也不会自动撮合。

同一账户同一研究日不要同时点击手工批次并等待自动批次。当前不同默认参数可能形成不同 `config_hash`，两个批次并发生成提案时会竞争模拟盘唯一约束。

## 5. 提案、审批与模拟成交

标准流程：

1. 完整批次完成后，在“当前提案”查看该研究日订单。
2. 对每条 `proposed` 订单单独批准或拒绝。
3. 下一个交易日开市后点击“执行模拟撮合”。
4. 在“历史成交”和持仓区核对成交、现金、数量和 NAV。

实时撮合规则：

- 仅允许上海时区 `09:30-11:30`、`13:00-15:00`；午间休市和收市后不能撮合。
- 只读取该账户最新有效批次中的 `approved` 订单，旧批次不会一起执行。
- 提案研究日必须早于实时行情交易日。
- 人工批准时间必须早于所用报价时间。
- 使用 iFinD `THS_RQ` 的 `latest` 价格，并保存报价快照 ID。
- 零成交量、停牌、一字涨跌停、现金不足、不可卖数量、T+1 或整手不满足时跳过并记录原因。
- 卖单先执行，随后才执行买单。

例如，2026-08-06 收盘研究并批准的提案，最早在 2026-08-07 的允许交易时段由用户点击撮合。若 8 月 7 日未启动程序，后续新研究批次会替代旧批次未成交提案；系统不会自动累计执行多天计划。

`POST /api/paper/settle` 是基于本地历史开盘价的维护接口；网页标准流程使用 `POST /api/paper/settle/realtime`，两者不要混用解释同一笔成交。

## 6. 策略编辑器

最快可用流程：

1. 在 `/strategies` 输入策略名称、股票池和策略需求。
2. 大模型生成 `generate_signals(context)` 源码。
3. 基础编译检查通过后直接启用策略。
4. 策略写入 `quant_strategy`，代码写入 `data/strategy_runs/{strategy_id}/v1/strategy.py`。
5. 创建模拟账户或更新部署时选择该 `strategy_version_id`。
6. 每日批次加载该版本，对绑定股池生成评分并形成提案。

`quant_strategy` 是策略元数据的权威记录，`data/strategy_runs/` 是实际代码位置。修改策略时创建一条新策略，模拟账户继续引用原策略 ID，避免运行中代码被静默替换。

代码运行采用 AST 限制、Python 隔离模式子进程、默认 3 秒超时和输出校验。这不是强安全沙箱，只适合本地受信任用户生成的代码。

## 7. 因子与回测记录

因子只保留 `quant_factor` 中的当前定义，保存同一 `factor_id` 会直接更新，不再创建版本或发布审查。历史回测统一保存在 `quant_backtest`，前端 `/backtest` 当前提供结果查看；旧因子评价、Top-N 回测和发布脚本已停用。

## 8. 备份与恢复

写库、迁移或批量构建前先停止服务，再备份整个 `data/` 中的状态数据库和必要产物：

```powershell
$backupDir = "data/backups/$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Path $backupDir
Copy-Item data/research_state.db,data/quant_research.db,data/paper_trading.db,data/stock_pool.db -Destination $backupDir
```

`quant_runtime.db` 已退役。模型元数据随模型文件保存在 manifest；每日因子和评分只服务于当前研究批次，提案写入 `paper_trading.db` 后即清理。

行情库较大，可按变更范围单独备份。恢复时停止服务，用同一时间点的数据库文件整体替换，再启动应用让幂等迁移运行。不要只恢复 `paper_trading.db` 而保留不匹配的策略或研究数据库。

完整性检查：

```powershell
python -c "import sqlite3; print(sqlite3.connect(r'data/research_state.db').execute('PRAGMA integrity_check').fetchone()[0])"
python -c "import sqlite3; print(sqlite3.connect(r'data/quant_research.db').execute('PRAGMA integrity_check').fetchone()[0])"
python -c "import sqlite3; print(sqlite3.connect(r'data/paper_trading.db').execute('PRAGMA integrity_check').fetchone()[0])"
```

## 9. 常见故障

### `UNIQUE constraint failed: paper_daily_run`

同一账户、研究日和策略版本被两个批次同时推进到 `order_proposals`。常见原因是自动调度和手工操作并发，或同日使用了不同配置哈希。

处理：

1. 不要再次连续点击运行。
2. 等待另一个批次结束并刷新页面。
3. 重新运行失败批次；已完成步骤会复用。
4. 若持续出现，检查 `/api/paper/scheduler/status` 和同日批次列表，避免自动与手工重复启动。

这属于当前已知并发限制。不要通过删除数据库记录绕过唯一约束。

### 批次停在 `current_shadow`

检查前复权行情、模型文件、模型 hash、股票池覆盖和 `trust_pickle` 前提。拒绝快照应保留，不要手工改为通过。

### 实时撮合没有成交

先查看返回的 `skipped.reason`：

- `no_later_trading_day`：报价日期没有晚于研究日；
- `quote_before_approval`：报价早于人工批准；
- `missing_realtime_quote`：iFinD 未返回该证券；
- `suspended_or_preopen_quote`：停牌、盘前或成交量无效；
- `one_price_limit_up/down`：一字涨跌停；
- 其他原因通常来自现金、持仓、整手或 T+1 约束。

### iFinD 或 LLM 失败

检查 `.env` 配置和 `/api/ifind/status`。认证、权限和合同错误会快速失败；网络和上游错误才会有限重试。不要在日志中输出请求头、密码或完整 API Key。

## 10. 验证

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

端到端测试需要先启动服务：

```powershell
cd frontend
npm run test:e2e
```

文档中不固定记录测试数量；以当前命令退出码和 CI 结果为准。
