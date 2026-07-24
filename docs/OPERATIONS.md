# 迹研运行与数据更新手册

> 更新日期：2026-07-22  
> 适用环境：Windows + conda `quant`

## 1. 环境基线

项目运行环境固定为 `quant`：

```powershell
conda activate quant
python scripts/check_environment.py
```

环境定义：

- `environment.yml`：conda 环境名和 Python 版本；
- `requirements.txt`：项目直接依赖；
- `requirements.lock`：已验证环境的精确 Python 依赖版本。

在新机器重建：

```powershell
conda env create -f environment.yml
conda activate quant
python scripts/check_environment.py
```

也可以对已有 `quant` 环境使用 uv 安装锁定依赖：

```powershell
uv pip install --python C:\path\to\envs\quant\python.exe -r requirements.lock
```

## 2. 启动应用

```powershell
conda activate quant
python run.py
```

浏览器访问 `http://127.0.0.1:8000`。iFinD SDK 对运行环境敏感，应从普通本机终端启动正式服务。

## 3. 日线数据口径

| 项目 | 约定 |
|---|---|
| 数据源 | iFinD QuantAPI SDK |
| 函数 | `THS_HD` |
| 指标 | `open;high;low;close;vwap;volume` |
| 复权 | 空参数 `""`，不复权；复权序列禁止写入本库 |
| 时间 | 交易日，`YYYY-MM-DD` |
| 成交量 | 股 |
| 写入策略 | append-only，历史主键不覆盖 |

首次增量运行已在 2026-07-21 完成：4,897 张个股表从 2026-07-17 更新到 2026-07-20。初次运行误用 `CPS:1`，经单股双参数契约对比发现后已回滚；随后使用空参数不复权口径重新拉取并校验。

### 3.1 独立复权研究库

| 数据库 | adjustment | iFinD 参数 | 用途 |
|---|---|---|---|
| `data/stock_data_qfq.db` | `forward` | `CPS:2` | 前复权特征、收益标签和 Qlib |
| `data/stock_data_hfq.db` | `backward` | `CPS:1` | 后复权交叉核验与扩展研究 |

两个库沿用原库的 `stock_<code>` 分表和 OHLC/VWAP/volume 字段，但具有独立 `price_database_metadata`、更新运行和质量问题表。构建过程写入临时数据库；只有全部证券通过契约和质量检查后才原子替换目标库，失败不会破坏上一版。

前复权历史会在新的公司行为发生后变化，因此复权库第一版统一使用全量重建，不使用不复权库的 append-only 更新逻辑。

检查计划：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment forward --dry-run
python scripts/build_adjusted_stock_data.py --adjustment backward --dry-run
```

构建用于 Qlib 的前复权库：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment forward
```

如需后复权库：

```powershell
python scripts/build_adjusted_stock_data.py --adjustment backward
```

## 4. 手工增量更新

先查看计划，不登录 iFinD、不写数据库：

```powershell
python scripts/update_stock_data.py --dry-run
```

执行更新：

```powershell
python scripts/update_stock_data.py
```

临时覆盖日线尝试次数与退避基数：

```powershell
python scripts/update_stock_data.py --max-retries 3 --retry-delay 0.5
```

指定截止日期：

```powershell
python scripts/update_stock_data.py --end-date 2026-07-20
```

默认截止日期规则：上海时间 18:00 以前取前一日，18:00 以后取当日。正式执行会先调用 iFinD `THS_Date_Query("SSE", "dateType:0")`，把这个日历截止日修正为最后一个真实交易日，再计算全库缺口；周末和节假日不会被误判为缺失交易日，也不会反复空拉。

命令输出中的关键字段：

- `today`：上海时区当前日期；
- `requested_end_date`：按 18:00 数据窗口得到的日历截止日；
- `latest_completed_trading_date`：iFinD 官方交易日历确认的目标交易日；
- `pending_security_count`：最新日期早于目标交易日的股票表数量；
- `missing_trading_dates`：市场整体尚未补齐的交易日；
- `status=up_to_date`：无需调用日线接口或写库。

`--dry-run` 保持完全离线，不登录 iFinD，因此其 `calendar_verified=false`，只用于预览。正式执行才会完成交易日历核验并按核验结果补齐。

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功或无需更新 |
| 2 | 部分股票失败 |
| 其他 | 登录、数据库或程序错误 |

## 5. 更新器安全机制

- `data/.stock_update.lock` 防止两个更新器同时运行；
- SDK 字段、日期、代码或数据类型契约变化会终止整次更新并回滚本次写入；
- `INSERT OR IGNORE` 保证已有日期不被覆盖；
- 每只股票根据自身最大日期过滤；
- 批次失败后自动二分，直到定位到单只失败股票；
- 单股质量异常会被隔离，其他合格股票仍可写入；
- `data_update_runs` 保存运行时间、来源、复权口径、行数、错误分类和失败原因；
- `data_quality_issues` 保存被隔离股票的规则、日期、观测值和期望条件；
- 研究服务以只读 URI 打开行情库。

写库前质量门禁：

| 规则 | 行为 |
|---|---|
| 字段集合变化、重复字段、非法日期/数值、重复证券日期 | 视为 SDK 契约变化，整次失败并回滚 |
| 返回请求范围外的证券代码 | 视为 SDK 契约变化，整次失败并回滚 |
| 收盘价非正或非有限值 | 隔离该股票 |
| OHLC 上下界错误、成交量为负、VWAP 超出日内高低价 | 隔离该股票 |
| 相邻收盘绝对变化超过 60% | 隔离该股票，等待公司行为或源数据人工核验 |

60% 是异常检测阈值，不是涨跌幅规则。新股、复牌、拆并股或除权事件可能产生真实大幅变化，因此系统选择隔离并保留证据，不自动修正或强行写入。

### 恢复策略

| 服务 | 超时 | 最多尝试 | 指数退避 | 熔断 |
|---|---:|---:|---|---|
| iFinD | 30 秒 | 3 次 | 0.5、1、2 秒 | 连续 3 次可恢复故障后打开 60 秒 |
| LLM | 连接 10 秒、读取 90 秒 | 3 次 | 0.75、1.5、3 秒 | 连续 3 次传输或服务故障后打开 60 秒 |

仅网络、限频、超时和上游服务错误进入恢复策略。认证、权限、请求参数和行情契约错误不会重试。LLM 的 JSON/Schema 输出失败允许有限重试，但不计入服务熔断。

iFinDAPI 0.0.8 没有暴露原生请求 timeout。应用等待超过 30 秒后会立即返回失败并禁止新的重叠 SDK 调用；底层 DLL 线程可能继续执行到自行返回，系统不会尝试不安全的强制终止，也不会在此情况下立即重试。

`GET /api/health` 同时返回 iFinD 和 LLM 的熔断状态、连续失败次数、配置阈值和 LLM 进程内重试指标。可在 `.env` 使用 `.env.example` 中的 `IFIND_*` 和 `LLM_*` 参数调整策略。

如果进程异常退出并遗留 `.stock_update.lock`，必须先确认没有更新进程，再手工删除锁文件。

## 6. 执行方式

当前采用人工日更，不配置 Windows 计划任务。建议交易日 18:30 后在普通本机终端手工执行，以便数据源完成盘后落库。

## 7. 更新后检查

更新后再次执行 dry-run，正常情况下应显示 `needed: false`：

```powershell
python scripts/update_stock_data.py --dry-run
```

查看最近运行日志：

```sql
SELECT *
FROM data_update_runs
ORDER BY started_at DESC
LIMIT 10;
```

查看最近质量隔离：

```sql
SELECT run_id, security_code, trading_date, rule, observed, expected
FROM data_quality_issues
ORDER BY created_at DESC
LIMIT 50;
```

iFinD 的登录、日线、证券资料和公告查询统一记录在 `data/research_state.db` 的 `ifind_call_events`。日志只保存函数名、耗时、请求对象数量、返回行数、错误码和错误分类，不保存证券列表、SDK 参数或凭据。

应用运行时可通过 `GET /api/ifind/status` 查看最近 24 小时聚合指标。错误分类包括 `authentication`、`permission`、`timeout`、`rate_limit`、`network`、`environment_or_network`、`contract` 和 `upstream`。

## 8. 故障处理

### iFinD 登录失败

1. 确认使用普通本机终端和 `quant` 环境；
2. 运行 `python example/testdemo.py`；
3. 确认 `.env` 中存在 `IFIND_USER` 和 `IFIND_PASSWORD`；
4. 检查账号是否在其他设备登录以及 SDK 权限是否有效；
5. 不要把凭据打印到终端或日志。

### SQLite locked

1. 确认没有另一个更新器；
2. 检查 `data/.stock_update.lock` 中记录的 PID；
3. 确认进程不存在后再删除遗留锁；
4. 不要直接删除 `stock_data.db-wal` 或 `stock_data.db-shm`。

### 部分股票失败

先查询 `data_update_runs.failed_codes`、`failed_reasons` 和 `quality_failed_codes`。质量异常继续查询 `data_quality_issues`，核对公司行为或 iFinD 原始值后再决定处理方式。再次运行更新器会从各表最新日期继续，成功股票不会重复写入。

## 9. 凭据规则

- 真实凭据只能保存在 `.env` 或受控密钥系统；
- `.env.example` 只能包含占位符；
- 示例、测试、日志和文档禁止出现真实账号、密码或 token；
- 旧凭据如果曾写入源码或版本历史，应立即轮换；
- `.gitignore` 只能防止新文件误提交，不能清除历史记录。

## 10. M6 模型导入与当前 Shadow

M6 当前只纳管已经训练完成的 Qlib/LightGBM 实验，不在应用请求或导入流程中重新训练。确认 `quant` 环境可用后执行：

```powershell
conda activate quant
python scripts/check_environment.py
python scripts/import_qlib_run.py `
  --experiment-dir "F:\项目\qlib-main\mlruns\937502219828981168" `
  --run-id a11a5663c34a477aafe9fc0c466193a4 `
  --trust-pickle
```

`--trust-pickle` 是显式安全确认。Python pickle 可以在反序列化时执行任意代码，只能用于用户自己生成或已经独立验证来源的本地产物；不得导入下载自不可信来源的 `params.pkl`、`pred.pkl` 或 `label.pkl`。

导入器会：

- 计算并保存各产物 SHA-256、大小和实验源指纹；
- 保存模型配置、时间切分、历史指标、用途和限制；
- 将预测与标签转换为逐日截面排名和分位；
- 对相同运行和指纹幂等复用，对同一运行 ID 的冲突指纹拒绝覆盖。

导入后检查：

```text
GET /api/model-runs/latest
GET /api/model-runs/a11a5663c34a477aafe9fc0c466193a4
GET /api/model-runs/a11a5663c34a477aafe9fc0c466193a4/signals?date=2020-07-31&limit=20
```

历史边界：现有预测截止 `2020-07-31`，使用下载的 Qlib CSI300 数据而不是项目 `stock_data.db`。历史信号用于复核和回放，不得解释成当前推荐。

### 10.1 手工构建当前 Shadow

当前阶段不配置自动任务。每次行情更新完成后，由操作者在 `quant` 环境手工执行：

```powershell
python scripts/build_current_shadow.py `
  --run-id a11a5663c34a477aafe9fc0c466193a4 `
  --as-of 2026-07-20 `
  --lookback-days 240 `
  --trust-pickle
```

运行顺序固定为：校验已注册模型及产物哈希、滚动复核冻结模型的历史 OOS 预测、查询当前沪深300成分股、准备前复权 Alpha158 provider、执行数据合同、推理并原子发布当前信号。该滚动复核不是重新训练模型。

诊断命令：

```powershell
python scripts/build_current_shadow.py --run-id a11a5663c34a477aafe9fc0c466193a4 --validate-only
python scripts/build_current_shadow.py --run-id a11a5663c34a477aafe9fc0c466193a4 --as-of 2026-07-20 --lookback-days 240 --diagnose-data --trust-pickle
```

发布后检查：

```text
GET /api/model-runs/a11a5663c34a477aafe9fc0c466193a4/validation
GET /api/current-shadow/latest
GET /api/current-shadow/3699241113268372272dadab2b78d1eeff02a038b2f8db069604198a44152abc/signals?limit=20
```

只有 `status=current_shadow_ready` 且所有 gate 为通过时，前端才展示当前信号。`rejected` 快照必须保留以便追查字段漂移、覆盖不足、复权错配或 SDK 异常，不得人工改状态或绕过门禁。当前信号统一标记“待评价”；M7 仅允许把它作为 DecisionCase 吸引力分项，不得进入候选池或交易建议，也不能覆盖证据与风险门禁。

## 11. M7 DecisionCase 手工运行

确认目标证券已经有同一截止日的成功 ResearchRun、Thesis 基线和 `current_shadow_ready` 快照后，在网页“决策案例”建立案例；也可调用：

```text
POST /api/decision-cases
{
  "code": "300750.SZ",
  "as_of": "2026-07-20",
  "decision_horizon": "60d",
  "benchmark_code": "000300.SH"
}
```

运行后必须检查 `rule_status`、三个独立 `scores`、所有 `gates`、`snapshot_hash` 和来源 Artifact 哈希。`research_required` 常见原因是 Thesis 基线没有更新到最新 ResearchRun；此时应先完成 Claim 重评并重新设置基线，再创建新案例，不得直接批准旧案例。

人工审批接口：

```text
POST /api/decision-cases/{case_id}/reviews
{
  "decision": "approve_for_tracking",
  "reviewer": "human",
  "note": "证据、风险和时点已复核，仅进入 Shadow 跟踪。"
}
```

只有 `eligible_for_review` 能使用 `approve_for_tracking`。`return_for_research` 和 `reject` 可用于其他状态；审批事件只追加一次，研究变化后必须新建案例。旧策略版本自动标记 `policy_superseded` 并禁止审批。人工批准不授权建仓、调仓或下单。

## 12. M8 DecisionOutcome 手工评价

M8 暂不配置自动任务。进入网页“决策案例”，选择案例并点击“评价结果”；或调用：

```text
POST /api/decision-cases/{case_id}/outcome
{}
```

调用会在 `quant` 环境使用 iFinD `THS_HD` 拉取从案例截止日至当前已完成数据日的证券和基准行情，固定参数为 `CPS:2`。处理规则：

- 周期未满保存 `pending` 快照，只显示已观察/所需交易日，不展示部分收益；
- 周期完整后保存 `completed`，计算证券总收益、基准总收益、超额收益和 MAE；
- 非前复权口径、日期重复、非法价格或证券/基准日期错位保存为 `data_rejected`；
- 相同案例、评价版本和行情指纹重复运行时幂等复用；新增行情会追加新快照，不覆盖旧结果。

查询接口：

```text
GET /api/decision-cases/{case_id}/outcome
GET /api/decision-outcomes?status=completed
GET /api/decision-outcomes/{outcome_id}
GET /api/decision-outcomes/attribution
```

跨案例归因只纳入每个案例最新的 `completed` 结果。任何少于 5 个样本的分组都标记为低样本，不得据此宣称 Shadow 信号有效。评价失败时先检查 `/api/ifind/status` 的认证、熔断和最近调用分类；禁止改用不复权本地库填补结果。

### 12.1 真实成熟案例定义

一个案例只有同时满足以下条件，才属于真实成熟案例：

1. `DecisionCase` 在结果发生前建立，案例截止日、周期、基准、规则、评分、证据和来源哈希已经冻结；
2. 证券和基准使用声明为 `CPS:2` 的前复权日线，并通过日期、价格和对齐合同；
3. 已经获得入场观察后的完整 N 个交易观察，而不是用自然日估算到期；
4. `DecisionOutcome.status=completed`，没有将部分周期收益包装为完成结果；
5. 原始规则状态和人工审批均保留，不因事后表现删除、改名或覆盖案例。

使用已知未来结果倒填的历史案例只能用于功能回归，不能进入真实 Shadow 评价样本。

### 12.2 周度积累流程

当前不做每日自动执行，建议每周固定一个交易日收盘后执行一个建案批次：

1. 手工增量更新 `stock_data.db` 并检查质量隔离和 iFinD 状态；
2. 使用相同 `as_of` 构建 Current Shadow，确认 `current_shadow_ready`；
3. 在看结果前按固定抽样规则选择 6–10 只证券，覆盖 Shadow 高分、中间和低分/规则失败区间；
4. 为样本完成同日 ResearchRun、Thesis/Claim 复核和基线更新；
5. 建立不可变 DecisionCase，并按真实研究结论审批或退回；
6. 给该批次记录统一截止日。即使当前版本没有独立 batch 表，也不得在结果出现后改变成员；
7. 每周对所有已有案例手工运行“评价结果”，直到变成 `completed` 或 `data_rejected`；
8. 保留所有规则状态和审批状态。`research_required`、`watch`、`reject` 是检验门禁区分度所需的对照样本。

初期以 `20d` 为主，约四周后形成首批反馈；同步建立少量 `60d` 案例观察约三个月结果。`120d` 和 `250d` 只做小规模长期队列，现阶段不依赖其反馈推进工程。

### 12.3 样本解释门槛

| 完整案例规模 | 允许用途 | 不允许的解释 |
|---|---|---|
| 1–4 | 单案例回放与故障检查 | 任何跨案例有效性判断 |
| 5–29 | 验证归因界面、分桶和数据合同 | 宣称信号、规则或审批有效 |
| 30–99 | 探索收益、超额收益、MAE 和拒绝率分布 | 直接开放 M9 或交易决策 |
| ≥100 | 在关键分组各 ≥30、至少 6 个建案批次且结果跨时期稳定时，进入 M9 准入评审 | 自动交易或忽略独立性检验 |

同一证券、相邻日期和重叠持有周期的案例高度相关，名义样本数会高估有效样本量。评审时必须同时按建案批次汇总，检查结果是否由单一市场阶段、行业或少数证券主导。以上数量是治理门槛，不替代置信区间、Bootstrap 或其他统计检验。

### 12.4 当前案例状态

2026-07-22 已使用真实 iFinD 行情评价宁德时代 `2026-07-20 / 60d / 沪深300` 案例。结果为 `pending`，已观察 `2 / 60` 个交易日，剩余 58 个交易观察；系统没有展示部分收益。预计需到 2026 年 10 月附近才能成熟，实际日期以交易日观察数为准。

下一工程项定义为 **M8.1 案例批次与到期扫描**：增加预设分位抽样清单、批次标识、到期队列、批量手工评价和批次级归因。M8.1 尚未实现，不能把当前逐案例手工操作描述为已经具备批量能力。

## 13. M9-lite 风险预算模拟盘

每日收盘后在网页“模拟盘”选择研究日并点击“运行完整批次”。系统按顺序执行：补齐日线并通过质量门禁、同步 `security_master.last_price_date`、生成同日因子快照、生成 `current_shadow_ready`、研究当日候选与当前持仓、持仓复核、生成订单提案。

当前批次版本为 `daily-research-batch-v1.2`，策略版本为 `paper-evidence-risk-v3.2`。六个步骤分别持久化 `pending/running/completed/failed`、开始结束时间、结果摘要和错误；同日同配置重复执行复用已完成批次，失败批次从首个失败步骤继续。服务重启时未完成批次会标为可恢复失败，不会假装成功。

订单引擎会把 Current Shadow 观察区与全部现有持仓合并评价。`maintain` 持仓保留，`reduce` 持仓只允许减仓，`defer` 或不在 Shadow 覆盖内的持仓冻结，只有 `veto` 或量化硬门禁失败才提出退出。冻结与保留持仓均占用组合席位；当席位已满时不生成新建仓提案。成交时卖单先于买单执行，并再次检查实际持仓数量，卖出未成功释放席位时新建仓买单不得成交。

证券级硬门禁包括：因子质量 `passed`、至少 251 个观察、20 日平均成交额至少 1 亿元、60 日波动率不超过 80%、250 日最大回撤不低于 -50%，以及截止日有正数 OHLC 和成交量。候选从 Shadow 前 30 名按排名检查，最多选择 5 只。

完整批次会合并量化硬门禁通过的前 5 个候选和所有当前持仓，去重后分别同步最多 2 份财报和 3 份其他公告，生成 Evidence Pack、Company Snapshot 和不可变 `ResearchAssessment`。模型只能提取带 Evidence ID 的基本面事实、重大负面、催化剂、证据置信度和失效条件；最终信号由 `evidence-admission-v1` 程序规则生成：`admit=1.0`、`reduce=0.5`、`defer=0`、`veto=0`。单步按钮仍保留用于排障，但不再是正常日更顺序。

`defer` 对新仓表示禁止准入，对已有持仓表示冻结而不是自动清仓；量化硬风险或跌出候选范围仍可触发退出。`veto` 对新仓禁止准入，对已有持仓触发退出提案。缺失、过期、策略版本不符或哈希异常的评估一律按 `defer` 处理。降权释放的仓位保持现金，不重新分配给其他股票。

组合目标总仓位为 50%，按 60 日波动率倒数分配，单票目标限制在 5%–12%。正式一级行业存在时执行单行业 20% 和最多 2 只；行业缺失时页面必须显示“相关性代理”，并使用 60 日收益相关性 0.85 上限。相关性代理不能被描述为正式行业约束。

所有提案保持 `proposed`，由用户逐笔批准或拒绝。批准不代表当日成交；系统只在研究日后的首个可交易开盘价撮合，停牌、无成交量或一字涨跌停继续等待。成交计入 5bp 滑点、万三佣金、最低 5 元佣金和卖出印花税，并遵守买入后 T+1 可卖数量。

盘中路径只允许使用 `THS_RQ` 实时快照，禁止调用 `THS_HF`、高频序列或行情推送。查询范围固定为当前持仓、已批准待成交证券和账户基准。快照通过时间戳、正数价格、成交量/成交额、最高最低价关系等合同后写入 `paper_realtime_quote`；异常或缺失快照不写入，也不能触发成交。订单必须在成交日 09:30 前完成审批，否则不得回填当日开盘价，只能等待下一交易日。实际成交引用保存在 `paper_order.execution_quote_id`。

网页“模拟盘”打开时每 60 秒调用一次 `THS_RQ` 并尝试撮合；关闭页面后不会继续轮询，因此这仍是页面驱动的 MVP，不是服务端常驻调度器。可使用“刷新实时行情”只更新盯市快照，或使用“实时撮合”显式执行同一流程。

修改策略必须提升 `STRATEGY_VERSION`。同日旧版本未审批提案标记 `cancelled` 并保留记录，不得删除或覆盖。正式行业数据、指数基准收益、ST 状态和精确涨跌停价格仍是已知缺口。

禁止为早于账户最近成交日的日期创建新研究批次，避免用当前持仓回溯生成历史决策。2026-07-23 首次真实评估使用 2026-07-22 时点证据完成 5/5：江西铜业、圣邦股份、豪威集团为 `reduce`，藏格矿业、药明康德为 `admit`。该结果仅用于验证新规则；应在 2026-07-23 收盘数据、因子快照和 Current Shadow 完成后再创建首个 v3 交易批次。

2026-07-23 已完成首个真实 `daily-research-batch-v1.1`：日线 4,897/4,897 已齐，因子通过 4,125/4,897，Current Shadow 发布 300/300 信号，候选与持仓合并后完成 9 家同日研究，5 只持仓复核结果为维持 3、降权复核 1、冻结 1、退出 0。旧 `paper-evidence-risk-v3.1` 的 3 笔未成交提案已由 `v3.2` 替代；新版将 5 个现有持仓席位全部纳入约束，没有生成新增买单。
