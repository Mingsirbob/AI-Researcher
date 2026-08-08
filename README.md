# 迹研 · Evidence AI Research

一个面向 A 股的本地证据型研究工作台。当前版本已覆盖本地行情、公告与财报证据、结构化研究、Thesis 手工监控、全市场候选池、当前 Shadow Signal、不可变 DecisionCase，以及完整周期的 DecisionOutcome 与跨案例归因。

## 当前边界

- 支持 4,897 只股票的本地日线查询与历史截止日 `as_of`。
- 计算收益、均线、RSI、波动率、最大回撤、量比和 52 周位置。
- 每条结论绑定 Evidence ID、数据日期、来源与计算方法。
- AI 仅接收已计算证据，并经过 JSON Schema 与证据引用校验。
- AI 服务不可用时自动降级为确定性报告。
- 公告、财务、研究、决策、Thesis 与运行审计保存在 `data/research_state.db`；当前因子、策略和回测保存在 `data/quant_research.db`；模拟账户、订单、持仓、净值和每日模拟批次保存在 `data/paper_trading.db`。LightGBM 模型使用文件 manifest，单次批次评分使用临时 CSV，不保留历史评分。原行情库始终只读。

首次使用拆分版本启动时，迁移 `0014_split_paper_trading_database` 和
`0015_split_paper_daily_batch_database` 会把旧状态库中的模拟盘数据幂等复制到
`paper_trading.db`。迁移完成后的新增模拟盘写入仅进入新库。
量化拆分迁移 `0017` 至 `0021` 采用相同策略，将旧量化数据复制到
`quant_research.db`，并在启动时从研究库批量同步只读证券主数据投影。
正式模式随后由 `0022_retire_split_domain_tables` 删除研究库中的旧模拟盘和量化表；
回滚依赖迁移前数据库备份，不再保留运行时旧库回退。
- 可选使用本机 iFinD SDK 补充证券简称和近期公告证据。

当前仍不包含完整行业、同行和估值数据。当前 Shadow Signal 可以作为 DecisionCase 吸引力的一个显式分项，但不能覆盖证据、风险或准入门禁。人工批准只允许进入后续 Shadow 评价，系统不会输出买卖建议或目标价。

模拟盘现已提供“运行完整批次”，按同一研究日依次完成：不复权日线补齐、证券主数据日期同步、全市场因子快照、Current Shadow、候选与现有持仓的证据研究、持仓复核和订单提案。六步状态与产物 ID 持久化，失败后可从断点继续；同日同配置重复执行会复用不可变产物。批次不会自动批准或成交订单。

`paper-evidence-risk-v3.2` 将全部持仓与 Shadow 候选统一评价：维持和冻结仓位继续占用组合席位，降权只允许减仓，只有否决或量化硬风险失败才提出退出。订单成交严格先卖后买；若卖单未释放席位，新建仓买单会被阻止，确保“最多持有 N 只”约束落实到实际持仓。

量化实验室已推进到 M11.2。9 个口径有效的价量因子已从不复权 v1 升级为 `CPS:2 / CSI300 current` 的不可变 v2，并建立未来 1/5/20 日 Rank IC、年化 ICIR、五分层收益、Top 层换手和跨因子相关性评价。首个正式基线覆盖 2021-01-04 至 2026-06-18 的 67 个调仓截面，结果按数据、因子与参数指纹冻结。前复权 VWAP 不能与真实成交量直接相乘还原历史成交额，因此平均成交额因子仅保留不复权 v1 历史定义，不进入前复权评价。现有 LightGBM 仍绑定 `alpha158_set@v1`；评价结果不会自动进入模型、候选池或交易。当前股票池是查询时点的沪深300，历史评价存在幸存者偏差。

M11.3 已建立单因子 Top-N 策略回测。回测绑定 M11.2 的评价 ID、因子版本和行情指纹，信号在收盘后形成并于下一交易日开盘成交，逐日记录现金、持仓、净值、回撤、调仓和交易成本。首个 `volume_ratio_20d@v2` 基线采用 Top 30、20日调仓；结果只用于研究，基准是当前沪深300等权代理，仍存在幸存者偏差且尚未完整模拟历史涨跌停、整手和冲击成本。

## 运行

```powershell
conda activate quant
python scripts/check_environment.py
python run.py
```

默认打开 `http://127.0.0.1:8000`；也可通过环境变量或启动参数使用其他端口。

Vue 3 是唯一生产前端。运行 `npm ci && npm run build` 后，访问
`http://127.0.0.1:8000` 会直接打开模拟盘；其他页面使用 `/company`、`/backtest`
等根级路由。若构建产物缺失，根入口返回503；生产回滚通过恢复上一完整 Git/部署版本完成。迁移范围、开发方式和退役记录见
`docs/VUE3_MIGRATION.md`。

### iFinD 配置

项目通过 `quant` 环境中安装的官方 `iFinDAPI` 包直接导入 `iFinDPy`。`.env` 至少需要：

```dotenv
IFIND_USER=你的数据接口账号ID
IFIND_PASSWORD=你的数据接口密码
```

账号和密码只用于进程内调用 `THS_iFinDLogin`，不会写入日志、数据库或 API 响应。`iFinD_Refresh_Token` 属于 HTTP API 的 access-token 流程，不能直接作为 `THS_iFinDLogin` 的账号参数；当前应用实现的是与 `example/crawl_ashares.py` 一致的 Python SDK 登录流程。

### 日线增量更新

原始证据库 `data/stock_data.db` 固定使用 iFinD `THS_HD` 空参数（`""`）的不复权口径，任何复权序列都禁止写入该库。先查看增量计划：

```powershell
python scripts/update_stock_data.py --dry-run
```

执行增量更新：

```powershell
python scripts/update_stock_data.py
```

命令会按 `Asia/Shanghai` 识别当前日期：18:00 前只检查前一自然日，18:00 后检查当天；登录后再通过 iFinD 官方 `THS_Date_Query` 取得该范围内最后一个真实交易日。系统会输出本地最早/最晚日期、待更新证券数和缺失交易日列表，仅在存在缺口时调用 `THS_HD`。因此周末和节假日不会被误判为缺失交易日，重复执行也不会重复写入。当前仍为人工触发，不配置后台定时任务。

写库前会校验 iFinD 响应结构、日期、证券代码、OHLC/VWAP/成交量和跨日异常跳变。结构契约变化会回滚整次更新；单股质量异常写入 `data_quality_issues` 并隔离，不会静默进入行情表。iFinD 调用指标可通过 `/api/ifind/status` 查看。

iFinD 和 LLM 均配置应用超时、有限重试、指数退避和熔断。默认连续 3 次可恢复故障后熔断 60 秒；认证、权限和数据契约错误快速失败，不进行无意义重试。

### 复权研究数据库

复权研究库构建工具已保留，但不是当前 M6 历史实验纳管的前提，也不会修改原始证据库：

| 数据库 | iFinD 参数 | 口径 |
|---|---|---|
| `data/stock_data_qfq.db` | `CPS:2` | 前复权（forward） |
| `data/stock_data_hfq.db` | `CPS:1` | 后复权（backward） |

先检查全量构建计划：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment forward --dry-run
python scripts/build_adjusted_stock_data.py --adjustment backward --dry-run
```

执行前复权库构建：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment forward
```

### 导入既有 Qlib/MLflow 实验

当前 M6 复用用户已经训练的 LightGBM 运行，不会重新训练。仅对来源可信、由自己生成的 pickle 产物执行导入：

```powershell
python scripts/import_qlib_run.py `
  --experiment-dir "F:\项目\qlib-main\mlruns\937502219828981168" `
  --run-id a11a5663c34a477aafe9fc0c466193a4 `
  --trust-pickle
```

导入会保存模型配置、指标、限制、产物 SHA-256、预测快照和逐证券历史信号。重复导入相同运行是幂等的；同一运行 ID 的产物指纹发生变化时会拒绝覆盖。历史预测截止 `2020-07-31`，用于滚动 OOS 复核和历史回放，不代表当前推荐。

### 构建当前 Shadow Signal

当前推理复用已经注册的冻结 LightGBM 模型，不重新训练。命令会先完成 2017-2020 OOS 预测的 63 交易日滚动窗口复核，再获取当前沪深300成分股，以 `CPS:2` 前复权行情构建 Alpha158 输入。只有模型哈希、证券覆盖、日期范围、字段、有限值和价格关系等门禁全部通过，才会发布信号：

```powershell
python scripts/build_current_shadow.py `
  --run-id a11a5663c34a477aafe9fc0c466193a4 `
  --as-of 2026-07-20 `
  --lookback-days 240 `
  --trust-pickle
```

可用 `--validate-only` 只执行历史 OOS 复核，或用 `--diagnose-data` 输出当前输入诊断。滚动 OOS 是对冻结模型已有样本外预测的连续窗口评价，不是滚动重训。被门禁拒绝的快照会保留用于审计，但不会发布信号。

### M7 受控决策案例

在网页“决策案例”中选择证券、截止日、20/60/120/250 交易日周期和沪深300/中证500/中证1000基准。系统自动绑定同日最新 ResearchRun、Evidence/Company/Quant Artifact 哈希、Thesis 和当前 Shadow，并通过 `decision-policy-v1.1` 生成：

- `attractiveness`、`evidence_confidence`、`risk_severity` 三个独立评分；
- `excluded`、`insufficient_evidence`、`research_required`、`watch` 或 `eligible_for_review` 规则状态；
- 流动性、波动率、回撤、证据时点、Thesis 基线和模型验证等可审计门禁；
- `approve_for_tracking`、`return_for_research` 或 `reject` 追加式人工审批。

只有 `eligible_for_review` 能批准进入 M8 Shadow 跟踪。审批不会产生交易授权，同一案例审批后不能覆盖；研究或策略变化必须创建新案例。

### M8 结果评价

在“决策案例”详情中手工点击“评价结果”。系统使用 iFinD `THS_HD` 的 `CPS:2` 前复权证券与基准收盘价。未满完整交易日周期时只保存和显示 `pending` 进度；成熟后计算证券收益、基准收益、超额收益与最大不利波动。页面下方的跨案例归因只纳入每个案例最新的完整结果，低于 5 个样本的分组明确标记为低样本。

```text
POST /api/decision-cases/{case_id}/outcome
GET  /api/decision-cases/{case_id}/outcome
GET  /api/decision-outcomes/attribution
```

结果评价仍是 Shadow 研究审计，不构成交易建议或交易授权。

#### 如何积累真实成熟案例

真实成熟案例必须在结果发生前冻结 `DecisionCase`，等待完整决策周期结束，通过 `CPS:2` 前复权价格、基准日期和价格对齐合同，并最终得到 `DecisionOutcome.status=completed`；不能使用已经知道结果的历史区间倒填案例。当前建议每周固定一个截止日，先更新行情并重建同日 Current Shadow，再按预先规定的 Shadow 分位选择 6–10 只证券，完成同日 ResearchRun、Thesis 基线、DecisionCase 和人工审批。前段、中间段、低分、规则未通过或人工拒绝样本都应保留，不能只记录最终表现好的证券。

第一批以 `20d` 为主获得约四周反馈，同时为少量证券建立 `60d` 案例。每周手工运行结果评价；系统会让未到期案例保持 `pending`，完整周期后才转为 `completed`。同一证券的重叠案例并非独立样本，归因时还需按建案日期批次观察。

样本数解释边界：5 个仅验证工程链路；30 个完整案例才开始观察分布。进入 M9 准入评审的最低样本条件是至少 100 个完整案例、关键分组各至少 30 个，并覆盖至少 6 个不同建案批次；它不是自动放行条件。还必须确认跨时期表现稳定、数据拒绝率可控、研究过程可复现，并通过批次级汇总、相关性、Bootstrap 置信区间和集中度检查。上述门槛是项目治理要求，不是统计显著性的保证。

复权库采用与原库相同的 `stock_<code>` 分表结构，但只通过全量临时库构建并原子替换。前复权历史会随公司行为改变，禁止按日期简单 append。

### 阶段 A 公告证据

初始化证券主数据并同步公告 PDF：

```powershell
python scripts/sync_research_data.py --bootstrap
python scripts/sync_research_data.py --code 300750.SZ --end-date 2026-07-20 --download-limit 5
```

公告原件按 SHA-256 保存，文本按页提取和切块；只有通过文本层质量检测的块才会进入 AI 证据，引用绑定本地 PDF、页码和 hash。详细数据模型和限制见 `docs/STAGE_A.md`。

更新器只追加每张股票表最大日期之后的数据，不覆盖历史主键，并将运行结果写入 `data_update_runs`。当前采用人工日更，完整操作、质量隔离和故障处理见 `docs/OPERATIONS.md`。

## 测试

```powershell
conda activate quant
pytest -q
```

## API

- `GET /api/stocks/{code}/analysis?as_of=YYYY-MM-DD`
- `GET /api/stocks/{code}/context?as_of=YYYY-MM-DD`
- `GET /api/ifind/status`
- `POST /api/research`
- `POST /api/research-runs`
- `GET /api/stocks/{code}/research-runs`
- `GET /api/research-runs/{id}`
- `POST /api/document-assistant`
- `POST /api/financial-change-template`
- `GET /api/securities?q=300750`
- `GET|POST /api/theses`
- `PATCH|DELETE /api/theses/{id}`
- `GET /api/model-runs/latest`
- `GET /api/model-runs/{model_run_id}`
- `GET /api/model-runs/{model_run_id}/signals`
- `GET /api/model-runs/{model_run_id}/validation`
- `GET /api/current-shadow/latest`
- `GET /api/current-shadow/{snapshot_id}/signals`
- `GET|POST /api/quant-research/factors`
- `GET /api/quant-research/factor-templates`
- `GET /api/quant-research/backtests/latest`
- `POST /api/paper/daily-batches`
- `GET /api/paper/daily-batches/latest`
- `GET /api/paper/daily-batches/{batch_id}`
- `POST|GET /api/decision-cases`
- `GET /api/decision-cases/{case_id}`
- `POST /api/decision-cases/{case_id}/reviews`
