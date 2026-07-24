# 阶段 A：证券主数据与公告证据垂直切片

> 状态日期：2026-07-21  
> 当前状态：首个真实垂直切片可运行，尚未完成 OCR、行业历史和财务事实

## 1. 当前能力

阶段 A 当前闭环：

```text
stock_data.db 个股表
→ security_master
→ iFinD THS_ReportQuery 公告元数据
→ PDF 原件下载
→ SHA-256 内容寻址与版本
→ pypdf 按页文本提取
→ 页内切块
→ 本地 PDF + 页码 + hash 引用
→ 公告证据 API / AI 研究输入
```

真实样本（宁德时代，截至 2026-07-20）：

- `security_master` 已初始化 4,897 条证券；
- 一年内公告元数据 226 条；
- 最近 5 份 PDF 已归档并建立版本；
- 3 份文本层通过质量检测，可以进入证据链；
- 2 份 H 股 PDF 字体映射异常，标记为 `ocr_required`，不会作为内容证据；
- 同步运行 ID：`0ebce19e-dcab-43ad-b3b9-b79df2abb334`。

## 2. 数据模型

数据保存在 `data/research_state.db`：

| 表 | 用途 |
|---|---|
| `security_master` | 证券代码、交易所、名称、行情覆盖日期以及待补充的上市/板块/行业字段 |
| `security_attribute_history` | 名称、行业等属性的观察历史与来源 |
| `announcement` | 公告元数据、iFinD sequence、发布时间、处理状态和当前文档版本 |
| `announcement_document` | PDF hash、版本、文件路径、字节数、页数和文本层状态 |
| `document_page` | 每页原始提取文本与页级 hash |
| `document_chunk` | 不跨页的文本块、页码、章节提示和文本 hash |
| `document_chunk_fts` | 公告文本全文索引，后续检索层使用 |
| `announcement_sync_run` | 每次同步的范围、数量、失败文档和状态 |

PDF 原件按内容寻址保存在：

```text
data/documents/<sha256前两位>/<sha256>.pdf
```

同一公告、同一 hash 重跑不会产生重复版本。源下载 URL 只在数据库内部保存，不通过公共 API 返回，其中可能包含短期有效 token。

## 3. 证券主数据边界

本地行情库可以可靠提供：

- 证券代码；
- 交易所后缀；
- 本地行情最早和最晚日期。

`first_price_date` 只是本地数据覆盖起点，不能冒充真实上市日期，因此 `listing_date` 当前保持空值。简称使用已验证的 iFinD `ths_stock_short_name_stock` 补充。

iFinD 公开手册不提供上市日期、板块和行业的稳定指标名，要求通过 SuperCommand 指标函数查询生成。相关字段已经预留，但在指标契约得到真实验证前不写入猜测值。

## 4. PDF 与引用规则

- 只下载 iFinD 允许域名下的 `http/https` 文件；
- 单文件默认上限 50 MiB；
- 文件头必须是 `%PDF-`；
- 每页独立提取和切块，引用不会跨页；
- 控制字符过多或语言字符分布异常时标记 `ocr_required`；
- `ocr_required`、下载失败或解析失败的文档不得进入内容证据；
- 公告标题只能证明文件发布，不能支持公告内容结论；
- 内容证据必须同时包含 `document_id`、`chunk_id`、页码和 SHA-256。

引用链接示例：

```text
/api/documents/<document_id>/file#page=2
```

## 5. 操作命令

初始化或刷新全量证券主数据：

```powershell
conda activate quant
python scripts/sync_research_data.py --bootstrap
```

同步单只证券一年公告并归档最近 5 份 PDF：

```powershell
python scripts/sync_research_data.py --code 300750.SZ --end-date 2026-07-20 --lookback-days 365 --download-limit 5
```

升级解析规则后重建已有 PDF 的页面和切块：

```powershell
python scripts/sync_research_data.py --code 300750.SZ --reprocess --download-limit 0
```

工作台也提供“同步公告”按钮。当前仍采用人工触发，不配置自动任务。

## 6. API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/stocks/{code}/announcements` | 查询持久化公告和文档处理状态 |
| `GET` | `/api/stocks/{code}/announcement-evidence` | 按证券、截止日和关键词查询页码证据 |
| `POST` | `/api/stocks/{code}/announcements/sync` | 人工触发公告增量同步和 PDF 归档 |
| `GET` | `/api/documents/{document_id}/file` | 打开本地归档 PDF，可使用 `#page=N` 定位 |

## 7. 后续增强项

为尽快验证公告与财报助手，上述完整性建设不再阻塞下一阶段。SuperCommand 主数据字段、OCR、FTS5 BM25/向量重排、扩大 PDF 回归集和完整 `financial_fact` point-in-time 模型均延期，按真实使用缺口逐项补齐。

当前已进入 [阶段 B 快速 MVP](STAGE_B_MVP.md)：使用现有可解析文档完成轻量召回、受控问答和原文页码引用。
