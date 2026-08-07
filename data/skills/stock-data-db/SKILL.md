---
name: stock-data-db
description: 使用本项目 data/stock_data.db 查询 A 股不复权日线、最新行情覆盖、更新批次和质量问题。用户提到原始行情、最新收盘价、成交参考价、行情是否更新、缺失交易日、iFinD 日线或 stock_data.db 时应使用此 Skill；跨期收益、因子和回测应改用 stock-data-qfq-db。
compatibility: Python 3 标准库 sqlite3；在 AI Researcher 项目根目录运行
---

# stock_data.db 使用规则

## 数据定位

- 数据库路径由 `app.core.config.settings.stock_db` 提供，默认是 `data/stock_data.db`。
- 这是 iFinD 不复权日线库，适合最新行情、成交参考、数据完整性和更新状态检查。
- 跨越分红、拆股或送转的收益计算会受到价格跳变影响。因子、收益率和回测优先使用 `stock_data_qfq.db`。
- 数据库按证券分表。`000001.SZ` 对应 `stock_000001_SZ`，`600000.SH` 对应 `stock_600000_SH`，`920000.BJ` 对应 `stock_920000_BJ`。

## 优先访问方式

业务代码优先复用 `app.market.repository.StockRepository`，它会以只读模式连接、验证证券代码并限制返回行数：

```python
from app.core.config import settings
from app.market.repository import StockRepository

repo = StockRepository(settings.stock_db)
rows = repo.get_history("000001.SZ", start="2026-01-01", end="2026-08-06")
```

只有在检查更新日志或质量表时才直接使用 SQLite，并强制使用只读 URI：

```python
import sqlite3
from app.core.config import settings

uri = settings.stock_db.resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True, timeout=10)
conn.row_factory = sqlite3.Row
```

## 核心表

### `stock_<code>_<exchange>`

| 字段 | 含义 |
|---|---|
| `time` | 交易日，ISO 日期，主键 |
| `open/high/low/close` | 不复权 OHLC |
| `vwap` | 成交量加权均价，可能为空 |
| `volume` | 成交量，可能为空 |

### 状态与审计表

- `market_data_status(security_code, latest_date, updated_at, status)`：逐证券最新日期和状态。
- `data_update_runs`：每次行情更新的范围、写入数量、失败代码和错误分类。
- `data_quality_issues`：按运行、证券、日期和校验规则记录质量异常。

## 查询工作流

1. 先查询 `market_data_status`，确认目标证券状态为 `ready` 且 `latest_date` 覆盖目标日期。
2. 将输入代码标准化为 `NNNNNN.SZ|SH|BJ`。动态表名不能参数化，因此只能在严格校验后生成。
3. 所有业务查询都带 `start`、`end` 或 `as_of`，不得读取研究日之后的数据。
4. 单证券默认不超过 3,000 行；大批量查询使用 `StockRepository.iter_histories()`，不要一次载入整个数据库。
5. 返回结果时说明数据库、`adjustment=unadjusted`、实际日期范围、行数和数据状态。

安全生成表名：

```python
from app.core.primitives import code_to_table

table = code_to_table("000001.SZ")
sql = f'SELECT time, open, high, low, close, vwap, volume FROM "{table}" '
sql += "WHERE time BETWEEN ? AND ? ORDER BY time"
rows = conn.execute(sql, ("2026-01-01", "2026-08-06")).fetchall()
```

## 更新规则

- 不要直接对个股表执行 `INSERT`、`UPDATE`、`DELETE`、`ALTER` 或 `DROP`。
- 先运行 `python scripts/update_stock_data.py --dry-run` 查看计划。
- 正式更新使用 `python scripts/update_stock_data.py`，更新器负责交易日判断、进程锁、重试、质量隔离和运行日志。
- 更新后检查最新 `data_update_runs.status`、失败代码、质量问题数量，以及所有目标证券的 `market_data_status`。

## 禁止事项

- 不根据未经校验的用户输入拼接表名。
- 不把不复权价格直接用于长期收益或回测。
- 不把文件修改时间当作数据日期；以 `market_data_status.latest_date` 为准。
- 不让大模型生成的策略代码直接打开数据库。策略运行所需行情应由应用读取后注入 `context`。
