---
name: stock-pool-db
description: 使用本项目 data/stock_pool.db 查询日期化股票池成员、排除规则和六大指数基准行情。用户提到沪深300、中证500、中证A500、全部A股非ST、创业板、科创50、深证成指、指数成分、策略股票池、历史 as_of 成员或 stock_pool.db 时应使用此 Skill。
compatibility: Python 3 标准库 sqlite3；在 AI Researcher 项目根目录运行
---

# stock_pool.db 使用规则

## 数据定位

- 数据库路径由 `app.core.config.settings.stock_pool_db` 提供，默认是 `data/stock_pool.db`。
- 股票池成员是日期化快照。研究日为 `T` 时，使用 `as_of <= T` 的最新快照，不得使用未来快照。
- 股票池只决定策略可选范围，不提供个股价格。个股行情分别使用 `stock-data-db` 或 `stock-data-qfq-db`。

## 已定义股票池

- `all_a_non_st`：全部 A 股（非 ST）
- `csi300`：沪深300
- `csi500`：中证500
- `csi_a500`：中证A500
- `sse_composite`：上证指数成分
- `szse_component`：深证成指
- `chinext`：创业板指
- `star50`：科创50

先调用 `StockPoolStore.list_pools()` 获取实际可用项，不要假定所有池都已有目标日期快照。

## 优先访问方式

业务代码优先使用 `StockPoolStore.resolve()`，它按研究日选择不晚于该日的最新快照：

```python
from datetime import date
from app.core.config import settings
from app.market.stock_pool import StockPoolStore

store = StockPoolStore(settings.stock_pool_db)
pool = store.resolve("csi300", date.fromisoformat("2026-07-31"))
```

只做审计查询时使用只读 URI：

```python
import sqlite3
from app.core.config import settings

uri = settings.stock_pool_db.resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True, timeout=10)
conn.row_factory = sqlite3.Row
```

## 核心表

- `stock_pool`：股票池定义、来源、最新快照日期、成员数和最近错误。
- `stock_pool_member`：主键为 `(pool_id, as_of, security_code)`，包含名称、可选权重和来源。
- `stock_pool_exclusion`：明确排除的证券和原因。
- `index_<code>_<exchange>`：指数日线，字段为 `time, open, high, low, close, vwap, volume, source, updated_at`。

支持的指数行情表映射：

| 指数代码 | 表名 |
|---|---|
| `000001.SH` | `index_000001_SH` |
| `000300.SH` | `index_000300_SH` |
| `000510.CSI` | `index_000510_CSI` |
| `000688.SH` | `index_000688_SH` |
| `399001.SZ` | `index_399001_SZ` |
| `399006.SZ` | `index_399006_SZ` |

## 查询工作流

1. 查询 `stock_pool`，确认池已启用、`last_error` 为空，并记录最新快照日期。
2. 对目标研究日选择 `MAX(as_of) WHERE pool_id=? AND as_of<=?`。
3. 使用选中的同一个 `as_of` 读取全部成员，不能逐证券混用不同日期。
4. 报告请求日期、实际快照日期、成员数、来源和是否发生日期回退。
5. 获取个股数据时，将成员代码传给对应行情服务；不要在股票池库中寻找个股价格。

只读 SQL 示例：

```sql
SELECT MAX(as_of) AS snapshot_date
FROM stock_pool_member
WHERE pool_id = ? AND as_of <= ?;
```

```sql
SELECT security_code, security_name, weight
FROM stock_pool_member
WHERE pool_id = ? AND as_of = ?
ORDER BY security_code;
```

指数表名不能接受任意输入。优先调用 `app.market.stock_pool.index_price_table()` 校验和转换指数代码。

## 历史与回测约束

- 如果目标日期之前没有快照，应明确返回缺失，不能退回当前成分股。
- 当前数据库可能只有有限快照，不能据此声称拥有完整历史成分变更。
- 回测必须把实际使用的 `pool_id`、快照日期、成员数和来源写入结果。
- 指数日线是基准行情，不等于指数成分权重历史。

## 更新规则

- 股票池定义和成员更新应通过 `python scripts/sync_stock_pools.py`，不要直接改表。
- 可使用 `--as-of YYYY-MM-DD` 或 `--pool-id csi300` 限定更新。
- 同步过程会校验主要指数的预期成员数量；失败时查看 `stock_pool.last_error`。
- 指数行情通过 `python scripts/backfill_index_prices.py` 或应用中的指数刷新流程更新。

## 禁止事项

- 不使用未来日期快照。
- 不把当前成分股冒充历史成分股。
- 不把 `security_name` 当作稳定主键，始终使用 `security_code`。
- 不直接写入数据库，也不让生成的策略代码自行打开 SQLite。
