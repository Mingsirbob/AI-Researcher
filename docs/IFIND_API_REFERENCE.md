# iFinD QuantAPI 接口分类与项目调用说明

最后核对日期：2026-07-31
适用项目：AI Researcher
SDK 基线：`iFinDAPI==0.0.8`，Python 模块 `iFinDPy`

## 1. 文档目的

本文档记录本项目实际调用的 iFinD QuantAPI 接口，说明接口所属配额分类、请求参数、标准化输出、调用入口、落库位置和使用边界。

本文档只描述已经在代码中出现的调用。账号配额页面展示的是账号累计用量，可能还包含其他程序或人工查询产生的流量，不能直接归因到本项目。

官方参考：

- [iFinD QuantAPI 产品手册](https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/help-center/manual.html)
- 项目行情口径与运维命令：[OPERATIONS.md](./OPERATIONS.md)
- 项目整体架构：[ARCHITECTURE.md](./ARCHITECTURE.md)

## 2. 分类总览

### 2.1 本项目实际使用状态

| 配额大类 | 配额子类 | iFinD 接口 | 项目状态 | 当前用途 |
|---|---|---|---|---|
| 会话管理 | 不计入业务数据分类 | `THS_iFinDLogin`、`THS_iFinDLogout` | 已使用 | 建立和释放 SDK 会话 |
| 行情数据 | 实时行情 | `THS_RQ` | 已使用 | 模拟盘持仓盯市、实时模拟撮合、指数盘中估值 |
| 行情数据 | 高频数据 | `THS_HF` | 未使用 | 项目明确不拉取高频序列 |
| 行情数据 | 历史数据 | `THS_HD` | 已使用 | 日线更新、复权库构建、Current Shadow、结果评价、指数基准 |
| 行情数据 | 日内快照 | `THS_SS` | 未使用 | 当前不需要独立日内快照接口 |
| 基本面数据 | 基础数据 | `THS_BD` | 有限使用 | 只查询证券简称，尚未查询结构化财务或估值指标 |
| 基本面数据 | 日期序列 | `THS_Date_Query` | 已使用 | 查询上交所官方交易日历 |
| 基本面数据 | 数据报表 | `THS_DR` | 已使用 | 按指数代码和日期查询成分股快照 |
| 特色数据 | 智能选股 | `THS_WCQuery` | 已使用 | `THS_DR` 不可用期间获取指数、板块和 ETF 当前成分股快照 |
| 特色数据 | 期股联动 | `THS_Special_StockLink` | 未使用 | 当前策略不使用期股联动数据 |
| 特色数据 | 公告查询 | `THS_ReportQuery` | 已使用 | 获取公告元数据和 PDF 地址 |
| 特色数据 | 公告下载 | 未调用专用 SDK 接口 | 间接使用 | 项目使用公告查询返回的 URL 通过 HTTP 下载 PDF |
| 宏观经济数据 | EDB | `THS_EDB` | 未使用 | 当前没有宏观经济数据模块 |

### 2.2 账号配额截图说明

以下数字来自用户提供的 2026-07-31 账号配额截图，只能作为当时的账号级观察值：

| 分类 | 已用/总量或余量 | 与本项目的关系 |
|---|---:|---|
| 行情数据总量 | `14,526,360 / 150,000,000` | 本项目会消耗实时行情和历史数据配额 |
| 实时行情 | `6,405,995` | 本项目使用 `THS_RQ`，但不能仅凭总量确定项目占比 |
| 高频数据 | `4,704,912` | 本项目未调用 `THS_HF`，该用量来自账号内其他活动 |
| 历史数据 | `3,415,453` | 本项目使用 `THS_HD` |
| 日内快照 | `0` | 与项目未使用 `THS_SS` 一致 |
| 基本面数据总量 | `5,002,247 / 5,000,000` | 截图已显示红色，需要在账号侧确认超额后的计费或限流规则 |
| 基础数据 | `2,223` | 本项目的 `THS_BD` 简称查询会产生部分用量 |
| 日期序列 | `5,000,024` | 本项目会调用交易日历，但该规模明显不应只由当前项目的少量日历请求解释 |
| 智能选股 | `10 / 15,000` | 股池同步通过 `THS_WCQuery` 查询，结果按日期保存到 `stock_pool.db` |
| 公告查询 | `48,727 / 1,000,000` | 本项目调用 `THS_ReportQuery` |
| 公告下载 | `0 / 100` | 项目未调用专用公告下载接口，而是下载查询结果中的 PDF URL |
| EDB | `0 / 5,000,000` | 本项目未调用 `THS_EDB` |

配额数字会变化，不应写入程序配置或测试断言。生产限流应依据接口类别、单次请求规模和应用自己的调用事件统计，而不是定期抓取此截图。

## 3. 统一数据层

项目只有一个 iFinD 入口：`app/data/ifind.py::IFindDataLayer`。

同一应用进程共享一个实例和一个登录会话。数据层提供日线、指数成分、公司数据和实时行情四组能力；业务代码和脚本不得直接导入 `iFinDPy`。

## 4. 会话管理接口

### 4.1 `THS_iFinDLogin`

**分类**：会话管理。
**调用位置**：`app/data/ifind.py`。

```python
THS_iFinDLogin(username, password)
```

项目约定：

- `0`：本进程登录成功，本进程拥有该会话；
- `-201`：当前环境已经登录，视为可用，但本进程不拥有该会话；
- 其他返回码：登录失败；
- 用户名和密码只从环境变量读取，不写入日志、数据库或接口响应。

### 4.2 `THS_iFinDLogout`

**分类**：会话管理。
**调用位置**：`app/data/ifind.py`。

```python
THS_iFinDLogout()
```

只有本进程通过返回码 `0` 创建的登录会话才会主动注销。对于登录返回 `-201` 的既有会话，项目不会擅自注销。

## 5. 行情数据接口

### 5.1 `THS_HD`：历史日线

**配额分类**：行情数据 / 历史数据。
**核心代码**：`app/data/ifind.py::IFindDataLayer.get_daily_prices`。

```python
THS_HD(
    ",".join(codes),
    "open;high;low;close;vwap;volume",
    history_params,
    start_date,
    end_date,
    "format:dataframe",
)
```

复权参数合同：

| 业务名称 | iFinD 参数 | 用途 |
|---|---|---|
| `unadjusted` | `""` | 真实历史价格、成交与模拟执行 |
| `backward` | `CPS:1` | 后复权扩展研究和交叉核验 |
| `forward` | `CPS:2` | Alpha158、模型信号、因子、收益标签和决策结果评价 |

标准化字段：

| 字段 | 含义 |
|---|---|
| `thscode` / `security_code` | 同花顺证券代码，如 `600000.SH` |
| `time` / `date` | 交易日 |
| `open`、`high`、`low`、`close` | 开高低收 |
| `vwap` | 成交量加权平均价 |
| `volume` | 成交量 |

主要消费者：

- `scripts/update_stock_data.py`：增量更新 `data/stock_data.db`；
- `scripts/build_adjusted_stock_data.py`：全量构建前复权或后复权数据库；
- `app/quant/current_shadow_service.py`：构建当前沪深300模型信号；
- `app/api/routers/decisions.py`：评价证券收益、基准收益、超额收益和 MAE；
- `app/api/handlers.py::refresh_paper_benchmarks`：固化模拟盘指数日线基准。

兼容性注意：当前官网手册展示的历史行情函数名为 `THS_HQ`，本项目固定 SDK `iFinDAPI==0.0.8` 的已验证代码使用 `THS_HD`。升级 SDK 前必须使用真实账号做函数存在性、参数语义、字段和配额类别契约测试，禁止直接把 `THS_HD` 全局替换为 `THS_HQ`。

### 5.2 `THS_RQ`：实时行情

**配额分类**：行情数据 / 实时行情。
**核心代码**：`app/data/ifind.py::IFindDataLayer.get_realtime_quotes`。

```python
THS_RQ(
    ",".join(codes),
    "open;latest;high;low;volume;amount;preClose",
    "",
    "format:dataframe",
)
```

项目按每批最多 50 只证券查询，并标准化为：

- `security_code`、`quote_time`；
- `open`、`latest`、`high`、`low`、`previous_close`；
- `volume`、`amount`；
- `source = "iFinD THS_RQ"`。

调用范围只允许包含当前持仓、已批准待成交证券和账户基准。主要触发入口：

- `POST /api/paper/realtime-quotes/refresh`：刷新实时盯市；
- `POST /api/paper/settle/realtime`：刷新行情并执行实时模拟撮合；
- 模拟盘页面轮询：页面打开期间定时刷新，不是服务端常驻行情订阅。

项目明确不使用 `THS_HF` 高频序列或行情推送替代 `THS_RQ`。

## 6. 基本面数据接口

### 6.1 `THS_BD`：基础数据

**配额分类**：基本面数据 / 基础数据。
**核心代码**：`app/data/ifind.py::IFindDataLayer.get_company_data`。

```python
THS_BD(
    ",".join(codes),
    "ths_stock_short_name_stock",
    "",
    "format:dataframe",
)
```

当前项目每批最多查询 100 只证券，只使用以下指标：

| 指标 | 含义 | 落库用途 |
|---|---|---|
| `ths_stock_short_name_stock` | 证券简称 | 补全证券主数据和页面展示名称 |

虽然官方手册说明 `THS_BD` 可以获取财务报表、盈利预测、并购重组等指标，但本项目尚未调用这些结构化基本面指标。当前财务事实来自公告 PDF 原文解析，不能写成“已接入 iFinD 结构化财务数据库”。

### 6.2 `THS_Date_Query`：交易日历

**配额分类**：基本面数据 / 日期序列。
**核心代码**：`app/data/ifind.py::IFindDataLayer._trading_dates`。

```python
THS_Date_Query(
    "SSE",
    "dateType:0",
    start_date,
    end_date,
)
```

项目用途：

- 判断目标日期是否为交易日；
- 将日线更新截止日修正为最后一个真实交易日；
- 避免周末和节假日被误判为行情缺口。

响应必须满足日期合法、范围内、无重复并按时间排序。官网当前手册使用 `THS_DateQuery` 命名，而项目 SDK 使用 `THS_Date_Query`，升级时同样需要真实契约验证。

## 7. 特色数据接口

### 7.1 `THS_DR`：指数成分数据报表

**配额分类**：账号真实错误码 `-4301` 将其计入基本数据周配额；最终分类以 iFinD 账号规则为准。
**核心代码**：`app/data/ifind.py::IFindDataLayer.get_index_members`。

```python
THS_DR(
    "p03473",
    f"iv_date={as_of:%Y%m%d};iv_zsdm={index_code}",
    "p03473_f001:Y,p03473_f002:Y,p03473_f003:Y",
    "format:dataframe",
)
```

标准化字段：

- `security_code`：证券代码；
- `security_name`：证券简称；
- `weight`：指数权重。

结果按 `universe_code + as_of + security_code` 保存到 `stock_data.db.universe_member`。当前研究使用对应截止日的快照，不再把当前成分股冒充历史股票池。

2026-07-31 最小真实测试确认 SDK 提供 `THS_DR`。`000300.CSI` 当日请求返回 `-4001 no data`，随后 `000510.CSI` 请求因基本数据周配额超过 500 万返回 `-4301`。因此当前字段映射已由模拟合同测试覆盖，但指数代码和真实字段仍需在配额恢复后补做真实验收。

### 7.2 `THS_ReportQuery`：公告查询

**配额分类**：特色数据 / 公告查询。
**核心代码**：`app/data/ifind.py::IFindDataLayer.get_company_data`。

```python
THS_ReportQuery(
    code,
    f"beginrDate:{start_date};endrDate:{end_date}",
    "reportDate:Y,thscode:Y,secName:Y,ctime:Y,"
    "reportTitle:Y,pdfURL:Y,seq:Y",
    "format:dataframe",
)
```

标准化字段：

| 字段 | 来源字段 | 含义 |
|---|---|---|
| `date` | `reportDate` | 公告日期 |
| `published_at` | `ctime` | 发布时间 |
| `title` | `reportTitle` | 公告标题 |
| `url` | `pdfURL` | 公告 PDF 地址 |
| `sequence` | `seq` | iFinD 公告唯一标识 |
| `source` | 固定值 | `iFinD` |

业务链路：

```text
THS_ReportQuery
  -> 公告元数据落库
  -> 按白名单 URL 下载 PDF
  -> 记录 SHA-256、页码和文本切块
  -> 提取程序化财务事实
  -> 构建 Evidence Pack 和 Company Snapshot
  -> 大模型证据型研究
```

`THS_ReportQuery` 返回公告标题并不等于证明公告内容。涉及财务或重大事项的结论必须引用已下载 PDF 的原文页码和文件哈希。

公告 PDF 使用普通 HTTP 下载逻辑，不调用专用“公告下载”SDK 接口。因此账号面板中的“公告下载 0”与项目实现并不矛盾，但实际配额归类仍以 iFinD 账号计费规则为准。

## 8. 当前未调用接口

| 接口或类别 | 状态 | 引入前必须解决的问题 |
|---|---|---|
| `THS_HF` 高频数据 | 禁止进入当前模拟盘 MVP | 数据频率、存储规模、订单簿模型、交易成本和回放基础设施 |
| `THS_SS` 日内快照 | 未使用 | 与 `THS_RQ` 的职责重叠和增量价值 |
| `THS_DS` / `THS_HQ` 基本面序列 | 未使用 | 指标合同、披露日、可得日、修订版本和未来数据泄漏 |
| `THS_EDB` | 未使用 | 宏观指标目录、发布日期、修订历史和策略消费合同 |
| `THS_Special_StockLink` | 未使用 | 期股映射、适用策略、风险暴露与配额预算 |
| 专题报表 | 未使用 | 报表定义、字段稳定性、授权和数据血缘 |

新增接口时不得只验证“能返回数据”。必须同时验证指标语义、时间口径、单位、空值、复权、修订行为、配额消耗、批量上限和错误码。

## 9. 项目触发入口

### 9.1 命令行入口

| 命令 | 可能调用的 iFinD 接口 |
|---|---|
| `python scripts/update_stock_data.py ...` | 登录、交易日历、`THS_HD`、注销 |
| `python scripts/build_adjusted_stock_data.py ...` | 单次登录、可选 `THS_DR`、复权和不复权 `THS_HD`、注销 |
| `python scripts/build_current_shadow.py ...` | 单次登录、`THS_DR`、前复权 `THS_HD`、必要时不复权回补、注销 |
| `python scripts/sync_research_data.py --code ...` | 登录、`THS_BD`、`THS_ReportQuery`、注销；PDF 下载不经过 SDK |

### 9.2 HTTP 入口

| 项目接口 | 关联 iFinD 调用 |
|---|---|
| `GET /api/ifind/status` | 检查 SDK、共享会话和启动行情同步状态 |
| `GET /api/stocks/{code}/context` | 按需查询简称和公告上下文 |
| `POST /api/stocks/{code}/announcements/sync` | `THS_BD`、`THS_ReportQuery`，随后下载并解析 PDF |
| `POST /api/paper/benchmarks/refresh` | 不复权 `THS_HD` |
| `POST /api/paper/realtime-quotes/refresh` | `THS_RQ` |
| `POST /api/paper/settle/realtime` | `THS_RQ`，随后执行模拟撮合 |
| `POST /api/decision-cases/{case_id}/outcome` | 前复权 `THS_HD` |
| `POST /api/paper/daily-batches` | 间接调用日线、指数基准、Current Shadow 和研究链路所需接口 |

## 10. 轻量数据保护

研究阶段只保留必要保护：

- 一个进程共享一个登录会话；
- 使用进程内锁串行调用 SDK；
- 检查非零错误码、空结果和必要字段；
- 可恢复错误最多重试一次；
- 行情通过 SQLite 事务增量写入；
- 调用日志不记录用户名、密码和完整证券列表。

`iFinDAPI==0.0.8` 不提供可安全取消的原生请求超时。应用超时只能停止等待并阻止新的重叠调用，不能强制终止仍在底层 DLL 中运行的线程。

## 11. 基本面扩展约束

如果后续使用 `THS_BD`、`THS_DS` 或 `THS_HQ` 接入结构化基本面，建议作为独立数据层实施，不直接混入现有 Alpha158 合同：

1. 使用 SuperCommand 生成并人工确认真实指标名和参数；
2. 同时保存报告期、公告日期、数据可得时间和供应商修订时间；
3. 训练和回测只能读取当时已经公开的数据，禁止按报告期直接回填；
4. 将价值、质量、成长、现金流和杠杆因子分别版本化；
5. 保存原始值、单位、币种、来源接口和请求参数摘要；
6. 与公告 PDF 提取事实交叉核验，不一致时进入质量隔离；
7. 为新增接口设置独立日配额预算和批量上限。

## 12. 变更检查清单

新增或修改 iFinD 调用时必须同步完成：

- [ ] 更新本文档的分类总览和接口明细；
- [ ] 增加适配层单元测试；
- [ ] 使用真实账号完成最小规模契约测试；
- [ ] 确认接口所属配额类别和单次数据量算法；
- [ ] 记录参数、字段、单位、时间与复权口径；
- [ ] 验证空结果、重复行、非法代码和非零错误码；
- [ ] 接入共享会话、一次可选重试和基础调用日志；
- [ ] 确认日志不泄露凭据或敏感参数；
- [ ] 明确落库位置、幂等键、版本和数据血缘；
- [ ] 检查回测中的未来数据泄漏和幸存者偏差。
