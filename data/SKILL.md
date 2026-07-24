---
name: local-stock-db-usage
description: >-
  Provides documentation, schema details, table naming conventions, query examples, and controlled update rules for the local SQLite stock database (data/stock_data.db). Use this skill when you need to query or maintain historical A-share data in the local database.
---

# Local Stock Database Usage Guide (本地个股数据库使用规范)

## Overview

本技能记录了本地 A 股历史行情 SQLite 数据库的存储规范与使用方法。该数据库位于项目根目录的 `data/stock_data.db`。它存储了自 **2020 年 1 月**以来的 A 股历史日线数据，涉及 4897 张个股表。

> [!IMPORTANT]
> **价格口径**：该数据库保存 iFinD `THS_HD` 空参数（`""`）不复权日线。实测 `CPS:1` 返回调整后价格，禁止写入现有个股表。

> [!NOTE]
> **未上市数据过滤**：对于在 2020 年 1 月之后上市或恢复交易的个股，数据库已彻底**过滤并丢弃了其上市交易前的所有无效占位行**。因此，每张个股表的最早记录即为该股在 2020 年以来的**正式上市日/上市首日**。这能有效帮助回测模型精准定位首次交易日期，并节省查询开销。

## Database Schema (数据库结构)

数据库中每只个股均拥有一张独立的表。

### 1. 表命名规则
股票代码（如 `000001.SZ`、`600000.SH`、`920000.BJ`）在数据库中对应的表名格式为 `stock_<code>_<market>`：
- `000001.SZ` -> `"stock_000001_SZ"`
- `600000.SH` -> `"stock_600000_SH"`
- `920000.BJ` -> `"stock_920000_BJ"`

> [!IMPORTANT]
> 在 SQL 语句中，如果使用表名，**建议用双引号将表名括起来**（例如 `SELECT * FROM "stock_000001_SZ"`），以防由于下划线或市场代码产生解析歧义。

### 2. 字段定义
每个个股表包含以下列：

| 字段名 | SQLite 类型 | 主键/索引 | 说明 |
| :--- | :--- | :--- | :--- |
| **time** | TEXT | PRIMARY KEY | 交易日期，格式为 `YYYY-MM-DD`（例如 `2026-07-17`） |
| **open** | REAL | - | 开盘价，停牌时为 `NULL` |
| **high** | REAL | - | 最高价，停牌时为 `NULL` |
| **low** | REAL | - | 最低价，停牌时为 `NULL` |
| **close** | REAL | - | 收盘价，停牌时为 `NULL` |
| **vwap** | REAL | - | 成交均价 (Volume Weighted Average Price)，停牌时为 `NULL` |
| **volume** | REAL | - | 成交量（单位：股） |

### 3. 数据过滤与停牌处理规范
- **首次上市截断**：数据按时间升序排列，定位至首个开盘价 `open` 不为空的交易日，该日期之前的所有未上市无交易占位行均已滤除，因此个股表的最早日期即为该股在 2020 年以来的正式交易起点。
- **上市中途停牌**：在上市首日**之后**的交易日里，如果因临时停牌导致没有成交，则该日行的 `open`、`high`、`low`、`vwap`、`volume` 字段会被存储为 `NULL`，而收盘价 `close` 仍会保留停牌前一交易日的收盘价格，以便资产估值或市值计算。

## Quick Start (快速开始)

下面是使用 Python 和 Pandas 快速读取个股时序行情数据的最佳实践代码：

```python
import sqlite3
import pandas as pd

def get_stock_data(stock_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """
    获取单只股票在指定日期范围内的日线数据
    :param stock_code: 股票代码，如 '000001.SZ'
    :param start_date: 开始日期，格式 'YYYY-MM-DD'
    :param end_date: 结束日期，格式 'YYYY-MM-DD'
    """
    # 1. 转换代码为表名
    table_name = f"stock_{stock_code.replace('.', '_')}"
    
    # 2. 拼接 SQL 查询
    query = f'SELECT * FROM "{table_name}"'
    conditions = []
    params = []
    
    if start_date:
        conditions.append("time >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("time <= ?")
        params.append(end_date)
        
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
        
    query += " ORDER BY time ASC"
    
    # 3. 连接数据库并读取
    conn = sqlite3.connect("data/stock_data.db")
    try:
        df = pd.read_sql_query(query, conn, params=params, parse_dates=['time'])
        df.set_index('time', inplace=True)
        return df
    finally:
        conn.close()

# 示例：获取宁德时代 2026 年以来的数据
df_catl = get_stock_data("300750.SZ", start_date="2026-01-01")
print(df_catl.head())
```

## Common Mistakes (常见错误)

1. **直接把点号代码当作表名**：
   - 错误：`SELECT * FROM 000001.SZ`
   - 正确：`SELECT * FROM "stock_000001_SZ"`（点号转换为下划线，添加前缀且带双引号）。
2. **在多进程/多线程下并发写数据库**：
   - 日常研究代码必须只读。唯一允许的写入入口是 `scripts/update_stock_data.py`，它使用进程锁、批次事务和 append-only 规则。
3. **未处理 `NULL` 值**：
   - 停牌期间的开盘价、最高价、最低价、成交量、均价等字段在数据库中为 `NULL`（Python 读取后为 `None` 或 `NaN`），但收盘价 `close` 仍会保留停牌前的价格。在计算相关技术指标（如依赖开/高/低价格的 KDJ、ATR，或依赖成交量的指标）之前，需要使用 `.ffill()` 或其他方法妥善填充或处理缺失值。

## Incremental Update（增量更新）

```powershell
conda activate quant
python scripts/update_stock_data.py --dry-run
python scripts/update_stock_data.py
```

更新规则：

- 数据源固定为 iFinD `THS_HD`；
- 指标固定为 `open;high;low;close;vwap;volume`；
- 参数固定为空字符串 `""`；禁止使用 `CPS:1`；
- 起始日期为数据库中最早的个股最新日期加一天；
- 默认按上海时间识别当前日期，18:00 前只允许补到前一日，18:00 后允许补到当日；
- 正式执行先用 iFinD `THS_Date_Query("SSE", "dateType:0")` 核验最后一个真实交易日，周末和节假日不会形成伪缺口；
- 输出待更新证券数和缺失交易日；没有缺口时不调用 `THS_HD`；
- 仅写入各表当前最大日期之后的记录；
- 使用 `INSERT OR IGNORE`，不覆盖历史主键；
- 失败批次自动二分到单只股票；
- 每次运行记录在 `data_update_runs`；
- `data/.stock_update.lock` 防止两个更新进程同时写库。
