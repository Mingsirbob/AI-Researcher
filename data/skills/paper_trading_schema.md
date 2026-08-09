# paper_trading.db V2

结构版本：V2。数据库只允许包含 `paper_account`、`paper_proposal`、`paper_trade`、
`paper_position`、`paper_nav_snapshot` 五张业务表以及 SQLite 内部表。

策略运行文件位于 `data/paper/{account_name}/{YYYY-MM-DD}/run-{NNN}/`。
数据库不使用 `schema_migration`，结构变更必须通过显式迁移脚本执行。
