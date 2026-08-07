# 阶段 B 快速 MVP：公告与财报助手（历史记录）

> 本文记录早期 MVP 的实现边界，后续检索、研究和前端能力可能已经变化。当前事实见 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [OPERATIONS.md](OPERATIONS.md)。

> 状态日期：2026-07-21  
> 当前状态：文档问答、财报变化模板、研究历史与证据快照均已实现

## 目标

先验证“用户问题 → 已归档原文 → 带页码回答”的产品闭环，不等待完整证券主数据、OCR、复杂检索或 `financial_fact` 建模。

当前链路：

```text
股票 + 截止日 + 问题
→ 限定已归档且文本层通过的公告
→ 轻量关键词扩展和相关片段排序
→ 同一文档最多保留两个片段
→ LLM 受控回答
→ Evidence ID、PDF 页码和 SHA-256 引用校验
→ 工作台展示回答与原文链接
```

工作台的“归档财报”会在 550 天公告范围内优先下载年度报告、半年度报告、季度报告、业绩预告和业绩快报，避免被更近的一般公告挤占下载名额。它仍是人工触发。

## 财报变化模板

`POST /api/financial-change-template` 生成固定五栏模板：

1. 经营规模；
2. 盈利质量；
3. 现金流与资本开支；
4. 资产负债与营运；
5. 股东回报。

模型不得自行计算同比、环比、比率或金额差。只有原文明确披露比较口径时才允许描述变化。每栏只能是 `supported` 或 `not_covered`；有结论时必须引用原文 Evidence ID，未覆盖时必须使用空引用。

模板现在优先使用 [`financial-regex-v1`](FINANCIAL_EXTRACTION.md) 程序化事实：程序识别报告期、区分流量与时点指标的比较期、统一元/千元/万元/亿元，并使用 `Decimal` 计算绝对变化和变化率。存在程序事实时模板模式为 `deterministic`，不调用模型计算数字。

## 问答记录与证据快照

每次普通问答和财报模板运行都会写入：

| 表 | 用途 |
|---|---|
| `research_interaction` | 股票、类型、问题、截止日、模式、状态、结构化结果、模型元数据和快照哈希 |
| `research_interaction_evidence` | 当次使用的完整证据副本，包括摘录、标题、页码、文档 ID、块 ID、SHA-256 和本地链接 |

证据快照采用规范化 JSON 计算 SHA-256。历史回放读取快照表，不重新执行检索，因此后续文档重新解析或召回规则变化不会改写旧记录。

历史接口：

- `GET /api/stocks/{code}/assistant-history`：按证券列出研究记录；
- `GET /api/research-interactions/{interaction_id}`：读取结果和完整证据快照。

## 当前 API

`POST /api/document-assistant`

请求示例：

```json
{
  "code": "300750.SZ",
  "question": "最近财报披露的营业收入和净利润情况如何？",
  "as_of": "2026-07-20",
  "scope": "financial_reports"
}
```

`scope` 支持：

- `all`：所有已解析公告；
- `financial_reports`：年度报告、半年度报告、季度报告、业绩预告和业绩快报；
- `announcements`：排除上述财报与业绩公告的一般公告。

## 可信度边界

- 只检索本地已归档、已解析的 PDF；
- `ocr_required`、下载失败和未下载文档不会进入回答；
- 每项内容结论必须引用输入中真实存在的 Evidence ID；
- 引用绑定 `document_id`、`chunk_id`、页码与 SHA-256；
- 无匹配证据时直接返回 `insufficient_evidence`；
- 模型不可用或引用校验失败时返回 `evidence_only`，不生成内容结论；
- 当前检索是面向 MVP 的词项扩展和确定性排序，不代表完整语义检索。

## 明确延期

以下能力不阻塞 MVP：

1. SuperCommand 上市日期、板块和行业指标契约；
2. OCR 引擎或线上 OCR API；
3. FTS5 BM25、向量检索和重排；
4. 大规模异常 PDF 回归集；
5. 完整 `financial_fact` point-in-time 模型。

## 下一轮产品优先级

1. 支持按公告列表选择或归档指定财报，而不是只下载最近若干份；
2. 在真实问题上记录“证据命中、引用正确、拒答正确”三项指标；
3. 扩充跨行表格、调整前后口径的受控解析，并建立人工确认队列；
4. 自动选择当前期与独立可比前期文档，支持不仅依赖财报内置比较列的跨报告验证；
5. 当 OCR 文档对高频问题造成明显缺口时，再选择本地 OCR 或线上 API。
