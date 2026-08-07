# 项目文档

本目录按“当前事实、专项参考、历史记录”分层。维护当前系统时，应优先阅读当前事实文档；历史记录只用于追溯设计背景，不能作为现有接口或运行规则的依据。

## 当前事实

- [APP_STRUCTURE.md](APP_STRUCTURE.md)：目录结构、模块职责和依赖边界。
- [ARCHITECTURE.md](ARCHITECTURE.md)：当前系统架构、数据库、核心数据流和已知限制。
- [OPERATIONS.md](OPERATIONS.md)：环境、启动、数据更新、研究批次、策略、回测、模拟撮合和备份恢复。
- [VUE3_MIGRATION.md](VUE3_MIGRATION.md)：Vue 已完成迁移后的前端结构与开发约束。

## 专项参考

- [IFIND_API_REFERENCE.md](IFIND_API_REFERENCE.md)：iFinD 接口、配额和数据口径。
- [FINANCIAL_EXTRACTION.md](FINANCIAL_EXTRACTION.md)：财务事实抽取合同与限制。

## 历史记录

- [STAGE_A.md](STAGE_A.md)、[STAGE_B_MVP.md](STAGE_B_MVP.md)：早期公告与财报助手阶段记录。
- [TASK_EVENT_TOOLRESULT_INTEGRATION.md](TASK_EVENT_TOOLRESULT_INTEGRATION.md)：运行事件账本的已实施范围和未实施提案。
- [baselines/APP_STRUCTURE_P0.md](baselines/APP_STRUCTURE_P0.md)：代码结构重构前基线。

## 维护原则

1. 代码、数据库迁移和 API 合同是事实源，文档不保存会快速失效的测试数量、样本 ID 或某日账户盈亏。
2. 已实现能力和规划能力必须分开描述；未落地设计不能写成当前功能。
3. 数据库归属、交易时段、审批与撮合规则发生变化时，必须同步更新架构与运维文档。
4. `.env`、账号、Token、运行数据库和下载文档不得写入 Git 或粘贴到文档。
