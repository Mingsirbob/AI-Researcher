---
name: stock-data-qfq-db
description: 使用本项目 data/stock_data_qfq.db 查询 A 股前复权日线、构建元数据和构建时证券范围。用户提到前复权、收益率、技术指标、因子计算、策略回测、历史价格可比性、source fingerprint 或 stock_data_qfq.db 时应使用此 Skill；实时成交参考价应改用 stock-data-db。
compatibility: Python 3 标准库 sqlite3；在 AI Researcher 项目根目录运行
---

# stock_data_qfq.db 使用规则

## 数据定位

- 数据库路径由 `app.core.config.settings.stock_qfq_db` 提供，默认是 `data/stock_data_qfq.db`。
- 这是前复权日线库，适合收益率、技术指标、因子、模型特征和策略回测。
- 最新成交参考、订单价格和账户盯市优先使用不复权 `stock_data.db`。
- 个股表命名与原始行情库一致：`000001.SZ` 对应 `stock_000001_SZ`。

## 访问方式

优先复用只读仓库：

```python
from app.core.config import settings
from app.market.repository import StockRepository

repo = StockRepository(settings.stock_qfq_db)
rows = repo.get_history("000001.SZ", start="2020-01-01", end="2026-07-31")
```

查询构建元数据时使用只读 SQLite URI：

```python
import sqlite3
from app.core.config import settings

uri = settings.stock_qfq_db.resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True, timeout=10)
conn.row_factory = sqlite3.Row
metadata = conn.execute("SELECT * FROM price_database_metadata").fetchone()
```

## 核心表

### `stock_<code>_<exchange>`

字段为 `time, open, high, low, close, vwap, volume`。价格是前复权口径，`time` 是主键。

### `price_database_metadata`

重要字段：

- `builder_version`、`adjustment`、`ifind_params`：构建合同。
- `source_db`、`source_db_size`、`source_db_mtime_ns`：来源指纹。
- `start_date`、`end_date`、`security_count`、`row_count`：覆盖范围。
- `built_at`、`blank_rows_dropped`、`accepted_quality_exception_count`：构建和质量状态。
- `universe_name`、`universe_as_of`、`universe_source`：构建时证券范围。

### `price_universe_membership`

记录构建时纳入的证券及 `universe_as_of`。它不是完整的历史成分变更表，不能单独用于历史时点选股；需要历史股票池时使用 `stock-pool-db` 并按 `as_of` 解析。

## 查询工作流

1. 先读取唯一一行 `price_database_metadata`，确认 `adjustment='forward'`、`end_date` 覆盖目标日期。
2. 检查目标证券是否存在于 `price_universe_membership` 和对应个股表中。
3. 严格标准化证券代码后再生成动态表名，查询必须带目标日期上限，防止未来数据泄漏。
4. 计算指标前明确最小回看窗口；历史不足时返回缺失，不用未来数据或其他证券填补。
5. 输出数据库、`adjustment=forward`、构建日期、覆盖日期、来源指纹和实际样本范围。

示例：

```python
from app.core.primitives import code_to_table

table = code_to_table("600000.SH")
rows = conn.execute(
    f'SELECT time, open, high, low, close, vwap, volume FROM "{table}" '
    "WHERE time<=? ORDER BY time DESC LIMIT ?",
    ("2026-07-31", 260),
).fetchall()
rows = list(reversed(rows))
```

## 回测约束

- 股票池必须按回测日期解析，不能把 `price_universe_membership` 当作历史成分表，否则会产生幸存者偏差。
- 信号日只能使用该日及以前的数据；成交价应由回测引擎按下一可交易时点决定。
- 保存结果时记录 `source_db`、来源指纹、元数据覆盖日期和策略参数，保证可复现。

## 更新规则

- 前复权历史会因公司行为变化，不要直接增量修改个股表。
- 先运行 `python scripts/build_adjusted_stock_data.py --adjustment forward --dry-run`。
- 正式构建使用 `python scripts/build_adjusted_stock_data.py --adjustment forward`，由构建器在独立文件完成校验后发布。
- 发布后重新检查 `price_database_metadata`、证券数量、行数、结束日期和质量例外数量。

## 禁止事项

- 不使用前复权价格作为模拟成交或真实报价。
- 不混用前复权和不复权序列计算收益。
- 不使用当前构建股池冒充历史股池。
- 不直接写入或修改该数据库，不让生成的策略代码自行打开 SQLite。
