# A 股量化研究与模拟交易系统

这是一个面向 Windows 单用户本地环境的 A 股策略研究与模拟交易工作台。后端使用 FastAPI，前端使用 Vue 3，运行状态保存在本地 SQLite 数据库中。

当前前端主要开放模拟盘和策略编辑器。系统只执行模拟交易，不连接真实券商，也不构成投资建议。

## 本地部署

### 1. 环境要求

- Windows 10/11；
- Git；
- Conda（Miniconda 或 Anaconda）；
- Node.js 22 和 npm；
- 有效的 iFinD 账号及可用的 iFinD Python SDK；
- OpenAI-compatible 模型接口（生成代码策略时需要）。

项目锁定 Python 3.11，并约定使用名为 `quant` 的 Conda 环境。

### 2. 获取代码

```powershell
git clone https://github.com/Mingsirbob/AI-Researcher.git
cd AI-Researcher
```

### 3. 安装后端环境

首次部署：

```powershell
conda env create -f environment.yml
conda activate quant
python scripts/check_environment.py
```

如果本机已经存在 `quant` 环境：

```powershell
conda activate quant
python -m pip install -r requirements.lock
python scripts/check_environment.py
```

环境检查必须显示 `quant 环境检查通过`。项目依赖的准确版本以 `requirements.lock` 和 `scripts/check_environment.py` 为准。

### 4. 配置环境变量

从配置样例创建本地 `.env`：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少配置实际使用的数据和模型服务：

```dotenv
# iFinD QuantAPI
IFIND_USER=你的账号
IFIND_PASSWORD=你的密码

# OpenAI-compatible 模型网关
TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_LLM_BACKEND_URL=https://你的接口地址/v1
OPENAI_COMPATIBLE_API_KEY=你的APIKey
TRADINGAGENTS_QUICK_THINK_LLM=你的模型名称
TRADINGAGENTS_DEEP_THINK_LLM=你的模型名称
```

`.env` 包含凭据，已被 Git 忽略。不要将它提交到仓库或输出到日志。

缺少模型配置时，依赖大模型的策略生成和公司研究能力不可用。缺少 iFinD 配置时，实时行情、模拟撮合和数据同步不可用。

### 5. 准备本地数据

运行数据位于 `data/`，不会随 Git 仓库分发。要运行现有模拟研究链路，至少应从可信的原部署复制以下数据：

```text
data/stock_data.db
data/stock_data_qfq.db
data/stock_pool.db
data/model_artifacts/
```

各项用途如下：

| 文件或目录 | 用途 | 空环境行为 |
|---|---|---|
| `stock_data.db` | 个股不复权日线 OHLCV | 不会自动补齐历史行情 |
| `stock_data_qfq.db` | Alpha158、LightGBM 和研究使用的前复权行情 | 默认模型链路需要 |
| `stock_pool.db` | 股票池、日期化成分和指数日线 | 可创建空库，但无法正常研究 |
| `model_artifacts/` | LightGBM 模型及 manifest | 默认模型策略需要 |
| `research_state.db` | 公告、证据和研究状态 | 缺失时自动创建 |
| `quant_research.db` | 策略等量化研究记录 | 缺失时自动创建 |
| `paper_trading.db` | 模拟账户、提案、成交、持仓和净值 | 缺失时创建新的 V2 数据库 |
| `paper/` | 每次模拟盘策略运行的永久过程文件 | 运行批次时创建 |
| `strategy_runs/` | 代码策略文件 | 生成代码策略时创建 |

要迁移已有账户和历史状态，还应在服务停止后复制：

```text
data/research_state.db
data/quant_research.db
data/paper_trading.db
data/paper/
data/strategy_runs/
```

不要只复制一个有关联的状态数据库。迁移前建议对原主机的 `data/` 做完整备份。

### 6. 构建前端

FastAPI 直接提供 `frontend/dist/` 中的生产页面，因此首次部署和前端代码更新后必须重新构建：

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

`npm ci` 使用已锁定的 `package-lock.json`。构建命令同时执行 Vue/TypeScript 类型检查。

### 7. 启动服务

```powershell
conda activate quant
python run.py
```

浏览器访问：

```text
http://127.0.0.1:8000/
```

服务默认只监听本机 `127.0.0.1:8000`。保持运行该 PowerShell 窗口；按 `Ctrl+C` 停止服务。

## 验证部署

启动后可检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

完整验证命令：

```powershell
conda activate quant
python scripts/check_environment.py
pytest -q

Set-Location frontend
npm run test
npm run build
```

Playwright 端到端测试要求后端已经在 `127.0.0.1:8000` 运行：

```powershell
Set-Location frontend
npm run test:e2e
```

## 更新现有部署

先停止服务并备份 `data/`，然后执行：

```powershell
git pull --ff-only
conda activate quant
python -m pip install -r requirements.lock
python scripts/check_environment.py

Set-Location frontend
npm ci
npm run build
Set-Location ..

python run.py
```

涉及显式数据库迁移的版本，应先阅读对应迁移文档和脚本的 `--dry-run` 输出，不要手工修改 SQLite 表结构。

## 常见问题

### 页面提示前端尚未构建

说明 `frontend/dist/index.html` 不存在。重新执行：

```powershell
Set-Location frontend
npm ci
npm run build
```

### 一直显示“正在读取数据”

先查看启动终端中的后端错误，再请求健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

常见原因是运行数据库结构不匹配、行情数据库缺失、数据库被其他进程锁定，或前端和后端代码版本不一致。`paper_trading.db` V1 不能由当前服务直接使用，应按照 [模拟盘 V2 迁移说明](docs/PAPER_TRADING_V2_MIGRATION.md) 处理。

### iFinD 功能不可用

确认已经安装并可导入项目锁定的 `iFinDAPI`，`.env` 中账号密码正确，账号具有相应接口权限，并检查：

```text
http://127.0.0.1:8000/api/ifind/status
```

### 端口 8000 被占用

先停止旧的项目进程。`run.py` 固定使用 `127.0.0.1:8000`，如需更换端口，可直接运行：

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## 项目目录

```text
app/             FastAPI 后端
frontend/        Vue 3 前端
scripts/         环境检查、数据更新和迁移脚本
tests/           Python 测试
docs/            架构、接口和运维文档
data/            本地数据库、模型和运行产物（不提交 Git）
```

更多信息：

- [运行与维护](docs/OPERATIONS.md)
- [系统架构](docs/ARCHITECTURE.md)
- [项目结构](docs/APP_STRUCTURE.md)
- [iFinD 接口参考](docs/IFIND_API_REFERENCE.md)
- [模拟盘 V2 迁移说明](docs/PAPER_TRADING_V2_MIGRATION.md)

## 使用边界

- 项目面向本地单用户，没有登录、租户隔离或正式任务队列；
- SQLite 状态库不能由多台主机同时写入；
- 代码策略仅在受限子进程中执行，不是容器级安全沙箱；
- 模拟成交不等同于真实市场成交，历史结果不代表未来收益。
