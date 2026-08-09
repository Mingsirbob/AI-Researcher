# Paper Trading V2 Migration

V2 将模拟盘数据库收敛为五表交易账本，策略运行详情永久保存到 `data/paper/`。

## 操作

停机并备份后依次运行：

```powershell
python scripts/migrate_paper_trading_v2.py --dry-run
python scripts/migrate_paper_trading_v2.py --apply
```

`--dry-run` 不写数据库或运行目录。`--apply` 先创建并校验临时 V2 数据库，再将原库
保存为带 UTC 时间戳的 `paper_trading.v1-*.db`，最后原子替换运行库。

## 回滚

停机，将当前 `paper_trading.db` 移走，再把对应的 `paper_trading.v1-*.db` 重命名为
`paper_trading.db`，并回滚应用代码。迁移生成的 `data/paper/` 目录不影响 V1，可保留。
