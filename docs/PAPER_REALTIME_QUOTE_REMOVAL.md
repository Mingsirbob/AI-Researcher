# 移除模拟盘实时行情表

## 变更

迁移 `0031_remove_paper_realtime_quote` 从 `paper_trading.db` 删除
`paper_realtime_quote`，并删除 `paper_order.execution_quote_id`。模拟盘实时个股与指数行情
改为在请求期间直接读取 iFinD `THS_RQ`，不再写入模拟盘数据库。

日线数据边界保持不变：个股 OHLCV 从只读 `stock_data.db` 获取，指数日线从
`stock_pool.db` 获取。成交记录仍保存成交日、成交价、成交金额和费用。

## 运行影响

- 首次启动会执行删表迁移，数据库文件大小不会立即缩小；需要释放磁盘空间时，可在停机并备份后手工执行 `VACUUM`。
- 仪表盘访问会触发一次 iFinD 实时行情请求。iFinD 不可用时，页面继续使用本地日线数据，响应中的 `realtime.error` 会说明原因。
- 实时行情接口和实时撮合不再提供可回查的行情快照 ID。

## 回滚

该迁移会删除实时行情快照，不能通过反向 SQL 恢复。回滚前必须停机，并用迁移前的
`paper_trading.db` 备份替换当前文件，同时回滚应用代码。不要仅删除
`schema_migration` 中的迁移记录。
