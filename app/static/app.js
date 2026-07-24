const state = {
  code: "300750.SZ",
  analysis: null,
  chartRange: 250,
  report: null,
  researchRun: null,
  assistant: null,
  ifindConfigured: false,
  factorSnapshot: null,
  researchOrigin: null,
  modelRun: null,
  modelValidation: null,
  currentShadow: null,
  signalMode: "current",
  decisionCases: [],
  selectedDecisionCase: null,
  decisionOutcomes: {},
  outcomeAttribution: null,
  paper: null,
  paperBenchmark: null,
  paperRealtimeTimer: null,
  paperRealtimeBusy: false,
  paperBatch: null,
  paperBatchTimer: null,
  activeWorkspace: "daily",
  activeView: "paper",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeUrl(value) {
  try {
    const url = new URL(String(value), window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) { return ""; }
}

const researchSignalLabels = {admit:"准入", reduce:"降权", defer:"暂缓", veto:"否决"};
const paperStatusLabels = {proposed:"待审批", approved:"已批准 · 待 THS_RQ 撮合", rejected:"已拒绝", filled:"已成交", cancelled:"已取消"};
const riskStatusLabels = {passed:"通过", rejected:"拒绝", selected:"已入选", eligible:"可入选", risk_rejected:"风险拒绝"};
const gateLabels = {
  factor_quality:"因子质量", observations:"有效样本", liquidity:"流动性",
  volatility:"60日波动", drawdown:"250日回撤", tradability:"当日可交易",
  research_assessment:"研究准入",
};
const rejectionReasonLabels = {
  factor_quality:"因子质量异常", observations:"有效样本不足", liquidity:"流动性不足",
  volatility:"波动率超限", drawdown:"历史回撤超限", tradability:"研究日不可交易",
  research_admit:"研究准入", research_reduce:"研究降权", research_defer:"研究暂缓",
  research_veto:"研究否决", max_positions_occupied:"目标持仓名额已满",
  industry_name_limit:"同一行业持仓数量达到上限", pair_correlation:"与已选股票相关性过高",
};

function formatPercent(value, {signed = false, digits = 2} = {}) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—";
  const number = Number(value);
  return `${signed && number > 0 ? "+" : ""}${(number * 100).toFixed(digits)}%`;
}

function formatConfidence(value) {
  return formatPercent(value, {digits: 1});
}

function formatMoney(value) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? "—"
    : `¥${Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2})}`;
}

function renderStatusBadge(value, label = null, title = "") {
  const safeClass = ["passed", "rejected", "selected", "eligible", "admit", "reduce", "defer", "veto", "proposed", "approved", "filled", "cancelled", "excluded", "current", "historical"].includes(value) ? value : "neutral";
  const text = label || researchSignalLabels[value] || riskStatusLabels[value] || paperStatusLabels[value] || value || "未知";
  return `<span class="status-badge ${safeClass}"${title ? ` title="${escapeHtml(title)}"` : ""}>${escapeHtml(text)}</span>`;
}

function renderSecurityIdentity(item, {date = null, rank = null, meta = null} = {}) {
  const name = item.security_name || item.name || item.security_code || item.code || "未知证券";
  const code = item.security_code || item.code || "—";
  const details = [code, date, meta].filter((value) => value !== null && value !== undefined && value !== "");
  return `<div class="security-identity"><b>${rank !== null && rank !== undefined ? `#${escapeHtml(rank)} ` : ""}${escapeHtml(name)}</b><small>${details.map(escapeHtml).join(" · ")}</small></div>`;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2800);
}

const workspaceViews = {
  daily: ["paper"],
  company: ["research", "theses"],
  lab: ["quant", "model"],
  audit: ["decision", "acceptance"],
};

function loadWorkspaceView(viewName) {
  if (viewName === "theses") loadTheses();
  else if (viewName === "quant") loadQuant();
  else if (viewName === "model") loadModelRun();
  else if (viewName === "decision") loadDecisionCases();
  else if (viewName === "paper") loadPaper();
  else if (viewName === "acceptance") loadAcceptance();
  else requestAnimationFrame(drawChart);
}

function activateWorkspace(workspace, requestedView = null) {
  const activeWorkspace = workspace in workspaceViews ? workspace : "daily";
  const views = workspaceViews[activeWorkspace];
  const viewName = views.includes(requestedView) ? requestedView : views[0];
  state.activeWorkspace = activeWorkspace;
  state.activeView = viewName;
  document.body.dataset.workspace = activeWorkspace;
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.workspace === activeWorkspace));
  $$("[data-workspace-tab]").forEach((tab) => {
    const visible = tab.dataset.workspaceTab === activeWorkspace;
    tab.hidden = !visible;
    tab.classList.toggle("active", visible && tab.dataset.view === viewName);
  });
  $("#workspace-tabs").hidden = views.length < 2;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${viewName}-view`));
  if (viewName === "paper") startPaperRealtimePolling();
  else { stopPaperRealtimePolling(); stopDailyBatchPolling(); }
  loadWorkspaceView(viewName);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      detail = body.detail?.message || body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function metric(id, value, suffix = "") {
  const el = $(id);
  el.textContent = value === null || value === undefined ? "—" : `${value}${suffix}`;
  el.classList.toggle("positive", typeof value === "number" && value > 0);
  el.classList.toggle("negative", typeof value === "number" && value < 0);
}

function evidencePreview(item) {
  const value = String(item.value ?? "");
  return item.category === "external_document" && value.length > 420
    ? `${value.slice(0, 420)}…`
    : value;
}

async function loadHealth() {
  try {
    const data = await api("/api/health");
    state.ifindConfigured = Boolean(data.ifind?.configured);
    $("#system-label").textContent = `${data.securities.toLocaleString()} 只股票 · ${data.llm_configured ? "AI 已就绪" : "确定性模式"}`;
    $("#ifind-state").textContent = state.ifindConfigured ? "iFinD · 已就绪" : `iFinD · ${data.ifind?.reason || "未配置"}`;
  } catch (error) {
    $("#system-label").textContent = "数据连接异常";
    $(".pulse").style.background = "var(--red)";
  }
}

async function loadAnalysis(code = state.code) {
  const cleanCode = code.trim().toUpperCase();
  const asOf = $("#as-of-input").value;
  document.body.classList.add("loading");
  try {
    const suffix = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
    const data = await api(`/api/stocks/${encodeURIComponent(cleanCode)}/analysis${suffix}`);
    state.code = data.security.code;
    state.analysis = data;
    state.report = null;
    state.researchRun = null;
    state.assistant = null;
    renderAnalysis(data);
    $("#security-search").value = data.security.code;
    $("#decision-code").value = data.security.code;
    $("#decision-as-of").value = data.as_of;
    $("#report-empty").hidden = false;
    $("#report-content").hidden = true;
    $("#report-mode").textContent = "待生成";
    $("#assistant-mode").textContent = "BETA";
    $("#assistant-empty").hidden = false;
    $("#assistant-result").hidden = true;
    $("#assistant-history-list").innerHTML = "<p>正在读取记录</p>";
    $("#run-history-list").innerHTML = "<p>正在读取运行</p>";
    $$(".quick-code").forEach((button) => button.classList.toggle("active", button.dataset.code === state.code));
    loadAssistantHistory();
    loadResearchRunHistory();
    if (state.ifindConfigured) loadIfindContext(data);
  } catch (error) {
    toast(error.message);
  } finally {
    document.body.classList.remove("loading");
  }
}

async function loadIfindContext(baseAnalysis) {
  try {
    const suffix = baseAnalysis.as_of ? `?as_of=${encodeURIComponent(baseAnalysis.as_of)}` : "";
    const context = await api(`/api/stocks/${encodeURIComponent(baseAnalysis.security.code)}/context${suffix}`);
    if (!state.analysis || state.analysis.security.code !== baseAnalysis.security.code) return;
    state.analysis.ifind_context = context;
    if (context.security?.name) {
      state.analysis.security.name = context.security.name;
      $("#security-name").textContent = context.security.name;
    }
    const persisted = await api(`/api/stocks/${encodeURIComponent(baseAnalysis.security.code)}/announcement-evidence?as_of=${encodeURIComponent(baseAnalysis.as_of)}&limit=5`);
    const metadataEvidence = context.announcements.slice(0, 5).map((item, index) => ({
      id: `ev-announcement-${index + 1}`,
      category: "external_document",
      label: "公开公告",
      value: item.title,
      as_of: item.published_at || item.date,
      source: `ifind://report/${item.sequence}`,
      method: "iFinD QuantAPI 公告查询；仅证明公告已发布，不代表已解析或支持任何投资结论",
    }));
    const external = persisted.items.length ? persisted.items : metadataEvidence;
    state.analysis.evidence = [
      ...state.analysis.evidence.filter((item) => !item.id.startsWith("ev-announcement-") && !item.id.startsWith("ev-doc-")),
      ...external,
    ];
    $("#evidence-count").textContent = state.analysis.evidence.length;
    renderEvidence(state.analysis.evidence);
  } catch (error) {
    $("#ifind-state").textContent = `iFinD · ${error.message}`;
  }
}

async function syncAnnouncements(documentScope = "all") {
  if (!state.analysis) return;
  const financialOnly = documentScope === "financial_reports";
  const button = financialOnly ? $("#sync-financial-reports") : $("#sync-announcements");
  button.disabled = true;
  button.textContent = financialOnly ? "归档中" : "同步中";
  try {
    const asOf = $("#as-of-input").value || state.analysis.as_of;
    const result = await api(
      `/api/stocks/${encodeURIComponent(state.code)}/announcements/sync?as_of=${encodeURIComponent(asOf)}&lookback_days=${financialOnly ? 550 : 365}&download_limit=5&document_scope=${documentScope}`,
      { method: "POST" },
    );
    await loadIfindContext(state.analysis);
    toast(`${financialOnly ? "财报" : "公告"}元数据 ${result.metadata_upserted} 条 · PDF ${result.documents_parsed} 份`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = financialOnly ? "归档财报" : "同步公告";
  }
}

function renderAnalysis(data) {
  const { metrics } = data;
  $("#security-code").textContent = data.security.code;
  $("#security-name").textContent = data.security.name;
  $("#latest-close").textContent = metrics.close?.toFixed(2) ?? "—";
  $("#as-of").textContent = data.as_of;
  $("#as-of-input").max = data.coverage.end;
  const daily = $("#daily-return");
  daily.textContent = metrics.daily_return == null ? "—" : `${metrics.daily_return > 0 ? "+" : ""}${metrics.daily_return.toFixed(2)}%`;
  daily.className = `quote-change ${metrics.daily_return > 0 ? "positive" : metrics.daily_return < 0 ? "negative" : ""}`;
  metric("#m-return20", metrics.return_20d, "%");
  metric("#m-volatility", metrics.volatility_60d, "%");
  metric("#m-drawdown", metrics.max_drawdown_250d, "%");
  metric("#m-position", metrics.range_position_52w, "%");
  metric("#m-volume", metrics.volume_ratio_20d, "x");
  metric("#m-trend", metrics.trend);
  $("#evidence-count").textContent = data.evidence.length;
  $("#claim-count").textContent = data.claims.length;
  renderEvidence(data.evidence);
  renderClaims(data.claims);
  $("#uncertainty-list").innerHTML = data.uncertainties.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  drawChart();
}

function renderEvidence(items) {
  $("#evidence-list").innerHTML = items.map((item) => `
    <article class="evidence-item ${item.category === "external_document" ? "external-document" : ""}">
      <span class="type">${item.category === "market_fact" ? "行情事实" : item.category === "external_document" ? "iFinD · 公开文档" : "确定性计算"} · ${escapeHtml(item.id)}</span>
      <h3>${escapeHtml(item.label)}</h3>
      <span class="value">${escapeHtml(evidencePreview(item))}</span>
      <button type="button" class="evidence-source">来源 · ${escapeHtml(item.as_of)}</button>
      <div class="evidence-method" hidden>${escapeHtml(item.method)}<br>${safeUrl(item.source) ? `<a class="evidence-link" href="${escapeHtml(safeUrl(item.source))}" target="_blank" rel="noopener">打开原始公告</a>` : escapeHtml(item.source)}</div>
    </article>
  `).join("");
  $$(".evidence-source").forEach((button) => button.addEventListener("click", () => {
    const method = button.nextElementSibling;
    method.hidden = !method.hidden;
    button.textContent = method.hidden ? `来源 · ${state.analysis.as_of}` : "收起计算说明";
  }));
}

function renderClaims(items) {
  $("#claim-list").innerHTML = items.map((item) => `
    <article class="claim-item">
      <div class="claim-kind"><span class="${item.claim_type === "fact" ? "fact" : ""}">${item.claim_type === "fact" ? "事实" : "AI前推断"}</span><small class="confidence">置信度 ${Math.round(item.confidence * 100)}%</small></div>
      <div class="claim-main"><p>${escapeHtml(item.statement)}</p><span class="evidence-refs">证据 ${item.evidence_ids.map(escapeHtml).join(" · ")}</span></div>
      <div class="claim-check"><b>证伪条件</b><p>${escapeHtml(item.invalidating_conditions.join("；") || "尚未定义")}</p></div>
    </article>
  `).join("");
}

function movingAverage(values, period) {
  let sum = 0;
  return values.map((value, index) => {
    sum += value ?? 0;
    if (index >= period) sum -= values[index - period] ?? 0;
    return index >= period - 1 ? sum / period : null;
  });
}

function drawChart() {
  if (!state.analysis) return;
  const canvas = $("#price-chart");
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const all = state.analysis.series;
  const start = Math.max(0, all.dates.length - state.chartRange);
  const dates = all.dates.slice(start);
  const closes = all.close.slice(start).map(Number);
  const volumes = all.volume.slice(start).map((value) => Number(value || 0));
  const ma20 = movingAverage(closes, 20);
  const width = rect.width;
  const height = rect.height;
  const pad = { left: 12, right: 58, top: 12, bottom: 24 };
  const volumeHeight = 66;
  const plotBottom = height - pad.bottom - volumeHeight;
  const priceMin = Math.min(...closes);
  const priceMax = Math.max(...closes);
  const range = priceMax - priceMin || 1;
  const yMin = priceMin - range * 0.08;
  const yMax = priceMax + range * 0.08;
  const x = (i) => pad.left + (i / Math.max(1, dates.length - 1)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + ((yMax - value) / (yMax - yMin)) * (plotBottom - pad.top);

  ctx.clearRect(0, 0, width, height);
  ctx.font = "9px Bahnschrift";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const yy = pad.top + (i / 4) * (plotBottom - pad.top);
    const label = yMax - (i / 4) * (yMax - yMin);
    ctx.strokeStyle = "#e8ebe7";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke();
    ctx.fillStyle = "#777e79";
    ctx.fillText(label.toFixed(2), width - pad.right + 8, yy);
  }
  const drawLine = (values, color, lineWidth) => {
    ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.beginPath();
    let begun = false;
    values.forEach((value, i) => {
      if (value == null) return;
      if (!begun) { ctx.moveTo(x(i), y(value)); begun = true; } else ctx.lineTo(x(i), y(value));
    });
    ctx.stroke();
  };
  drawLine(ma20, "#b66a00", 1.2);
  drawLine(closes, "#008b83", 2);

  const maxVolume = Math.max(...volumes, 1);
  const barWidth = Math.max(1, (width - pad.left - pad.right) / dates.length * 0.62);
  volumes.forEach((value, i) => {
    const barHeight = (value / maxVolume) * (volumeHeight - 15);
    ctx.fillStyle = i > 0 && closes[i] >= closes[i - 1] ? "#d8483e55" : "#13866055";
    ctx.fillRect(x(i) - barWidth / 2, height - pad.bottom - barHeight, barWidth, barHeight);
  });
  const ticks = Math.min(5, dates.length);
  ctx.fillStyle = "#777e79";
  for (let i = 0; i < ticks; i += 1) {
    const index = Math.round((i / Math.max(1, ticks - 1)) * (dates.length - 1));
    ctx.fillText(dates[index]?.slice(2), x(index) - 16, height - 8);
  }
  canvas._chart = { dates, closes, volumes, x, y, pad, width, height };
}

function handleChartMove(event) {
  const canvas = $("#price-chart");
  const chart = canvas._chart;
  if (!chart) return;
  const rect = canvas.getBoundingClientRect();
  const mx = event.clientX - rect.left;
  const usable = chart.width - chart.pad.left - chart.pad.right;
  const index = Math.max(0, Math.min(chart.dates.length - 1, Math.round(((mx - chart.pad.left) / usable) * (chart.dates.length - 1))));
  const tooltip = $("#chart-tooltip");
  tooltip.innerHTML = `${escapeHtml(chart.dates[index])}<br>收盘 ${chart.closes[index].toFixed(2)}<br>成交量 ${(chart.volumes[index] / 1e6).toFixed(1)} 百万股`;
  tooltip.hidden = false;
  const left = Math.min(rect.width - 135, Math.max(5, mx + 12));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(8, chart.y(chart.closes[index]) - 28)}px`;
}

async function runResearch() {
  if (!state.analysis) return;
  const button = $("#run-research");
  button.disabled = true;
  button.lastChild.textContent = " 正在运行研究";
  try {
    const result = await api("/api/research-runs", {
      method: "POST",
      body: JSON.stringify({
        code: state.code,
        as_of: $("#as-of-input").value || null,
        depth: "quick",
        factor_snapshot_id: state.researchOrigin?.securityCode === state.code
          ? state.researchOrigin.factorSnapshotId
          : null,
      }),
    });
    state.report = result;
    state.researchRun = result;
    state.analysis = result.analysis;
    $("#security-name").textContent = result.analysis.security.name;
    $("#evidence-count").textContent = result.analysis.evidence.length;
    renderEvidence(result.analysis.evidence);
    renderCompanySnapshot(result);
    activateTab("report");
    await loadResearchRunHistory(result.run_id);
    toast(result.status === "completed" ? "公司研究已完成" : "公司研究已完成，存在证据缺口");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.lastChild.textContent = " 生成公司研究";
  }
}

function renderCompanySnapshot(result) {
  const snapshot = result.snapshot;
  $("#report-empty").hidden = true;
  $("#report-content").hidden = false;
  $("#report-mode").textContent = snapshot.status === "complete" ? "证据完整" : "部分覆盖";
  const section = (title, items) => items?.length ? `<section class="report-section"><h3>${title}</h3><ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>` : "";
  const coverageLabels = {market: "行情", announcements: "公告原文", financials: "财务事实", quant: "横截面"};
  const coverage = Object.entries(snapshot.coverage).map(([key, item]) => `
    <div class="coverage-item ${item.status}"><span>${escapeHtml(coverageLabels[key] || key)}</span><b>${item.status === "supported" ? "已覆盖" : item.status === "excluded" ? "质量排除" : "未覆盖"}</b></div>
  `).join("");
  const directions = {improved: "改善", weakened: "减弱", mixed: "分化", stable: "稳定", unknown: "未判断"};
  const financial = snapshot.financial_change.sections.map((item) => `
    <article class="snapshot-financial ${item.status}"><header><b>${escapeHtml(item.label)}</b><span>${escapeHtml(directions[item.direction] || "未判断")}</span></header><p>${escapeHtml(item.summary)}</p><small>${item.evidence_ids.length ? `证据 ${item.evidence_ids.map(escapeHtml).join(" · ")}` : "无可引用财务事实"}</small></article>
  `).join("");
  const announcements = snapshot.recent_announcements.length
    ? snapshot.recent_announcements.map((item) => `<li><b>${escapeHtml(item.title)}</b><span>${escapeHtml(item.published_at || "日期未知")} · ${escapeHtml(item.status)}</span></li>`).join("")
    : "<li><b>没有截止日内的持久化公告</b><span>请先手工同步公告</span></li>";
  const artifacts = (result.artifacts || []).map((item) => `${escapeHtml(item.artifact_type)} ${escapeHtml(item.snapshot_hash.slice(0, 12))}`).join(" · ");
  const quant = snapshot.quant_context;
  const quantLabels = {
    return_20d: "20日收益", return_60d: "60日收益", volatility_60d: "60日波动",
    avg_traded_value_20d: "20日均成交额", volume_ratio_20d: "量比",
    max_drawdown_250d: "250日最大回撤", range_position_52w: "52周位置",
  };
  const quantValue = (key, value) => key === "avg_traded_value_20d"
    ? turnoverValue(value)
    : key === "volume_ratio_20d"
      ? (value == null ? "—" : `${Number(value).toFixed(2)}x`)
      : percentValue(value);
  const quantPanel = quant?.status === "supported"
    ? `<section class="report-section"><h3>横截面量化上下文</h3>
        <div class="quant-context-meta"><span>快照 ${escapeHtml(quant.as_of)}</span><span>${escapeHtml(quant.factor_version)}</span><span>${quant.universe.passed.toLocaleString()} / ${quant.universe.total.toLocaleString()} 只通过质量门禁</span></div>
        <div class="quant-context-grid">${Object.entries(quant.factors).map(([key, value]) => {
          const rank = quant.ranks[key];
          return `<article><span>${escapeHtml(quantLabels[key] || key)}</span><b>${escapeHtml(quantValue(key, value))}</b><small>${rank ? `数值百分位 ${rank.percentile.toFixed(1)} · ${rank.rank}/${rank.universe}` : "未参与排名"}</small></article>`;
        }).join("")}</div>
        <p class="quant-context-note">仅提供价格与流动性横截面位置，不构成基本面结论或投资建议。来源 ${escapeHtml(quant.source === "candidate_pool" ? "候选池快照" : "截止日内最近兼容快照")} · ${escapeHtml(quant.snapshot_id)}</p>
      </section>`
    : `<section class="report-section"><h3>横截面量化上下文</h3><p class="quant-context-empty">${escapeHtml(quant?.limitations?.join("；") || "该历史运行尚未包含量化上下文。")}</p></section>`;
  $("#report-content").innerHTML = `
    <header class="report-header"><span class="eyebrow">RESEARCH RUN · ${escapeHtml(snapshot.workflow_version)}</span><h2>${escapeHtml(snapshot.security.name)}公司研究快照</h2><p>${escapeHtml(snapshot.executive_summary)}</p><div class="run-stamp"><span>RUN <b>${escapeHtml(result.run_id)}</b></span><span>截至 <b>${escapeHtml(snapshot.as_of)}</b></span><span>${escapeHtml(result.mode === "ai" ? "AI · SCHEMA VALIDATED" : "DETERMINISTIC FALLBACK")}</span></div></header>
    <div class="coverage-strip">${coverage}</div>
    ${section("观察到的变化", snapshot.observed_changes)}
    ${quantPanel}
    <section class="report-section"><h3>财报变化</h3><div class="snapshot-financial-grid">${financial}</div></section>
    <section class="report-section"><h3>最近公告</h3><ul class="snapshot-announcements">${announcements}</ul></section>
    ${section("反方审查", snapshot.counter_view)}
    ${section("证据与口径边界", snapshot.limitations)}
    ${section("下一步核验", snapshot.next_checks)}
    <div class="artifact-stamp">${artifacts}</div>
  `;
}

async function loadResearchRunHistory(activeId = null) {
  if (!state.code) return;
  try {
    const history = await api(`/api/stocks/${encodeURIComponent(state.code)}/research-runs?limit=20`);
    $("#run-history-count").textContent = history.items.length;
    const list = $("#run-history-list");
    if (!history.items.length) {
      list.innerHTML = "<p>暂无运行</p>";
      return;
    }
    const labels = {completed: "完整", completed_with_gaps: "部分覆盖", failed: "失败", running: "运行中"};
    list.innerHTML = history.items.map((item) => `
      <button type="button" class="history-item ${item.run_id === activeId ? "active" : ""}" data-run-id="${escapeHtml(item.run_id)}">
        <span>${escapeHtml(labels[item.status] || item.status)} · ${escapeHtml(item.workflow_version)}</span>
        <b>${escapeHtml(item.as_of)} 公司研究</b>
        <small>${escapeHtml(item.started_at.slice(0, 16).replace("T", " "))} · ${item.evidence_count} 条证据</small>
      </button>
    `).join("");
    $$("#run-history-list .history-item").forEach((button) => button.addEventListener("click", () => openResearchRun(button.dataset.runId)));
  } catch (error) {
    $("#run-history-list").innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

async function openResearchRun(runId) {
  try {
    const run = await api(`/api/research-runs/${encodeURIComponent(runId)}`);
    const snapshotArtifact = run.artifacts.find((item) => item.artifact_type === "company_snapshot");
    const evidenceArtifact = run.artifacts.find((item) => item.artifact_type === "evidence_pack");
    const quantArtifact = run.artifacts.find((item) => item.artifact_type === "quant_context");
    if (!snapshotArtifact) throw new Error("该研究运行没有公司快照");
    const result = {
      run_id: run.run_id,
      status: run.status,
      mode: snapshotArtifact.payload.generation.mode,
      snapshot: snapshotArtifact.payload.quant_context || !quantArtifact
        ? snapshotArtifact.payload
        : {...snapshotArtifact.payload, quant_context: quantArtifact.payload},
      evidence: evidenceArtifact?.payload.items || [],
      artifacts: run.artifacts,
    };
    state.researchRun = result;
    renderCompanySnapshot(result);
    $$("#run-history-list .history-item").forEach((button) => button.classList.toggle("active", button.dataset.runId === runId));
  } catch (error) {
    toast(error.message);
  }
}

async function askDocumentAssistant(event) {
  event.preventDefault();
  if (!state.analysis) return;
  const question = $("#assistant-question").value.trim();
  if (question.length < 2) return;
  const button = $("#ask-assistant");
  button.disabled = true;
  button.lastChild.textContent = " 正在核验原文";
  try {
    const result = await api("/api/document-assistant", {
      method: "POST",
      body: JSON.stringify({
        code: state.code,
        question,
        as_of: $("#as-of-input").value || null,
        scope: $("#assistant-scope").value,
      }),
    });
    state.assistant = result;
    renderAssistantResult(result);
    $("#assistant-mode").textContent = result.mode === "ai" ? "已引用" : "证据不足";
    await loadAssistantHistory(result.interaction_id);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.lastChild.textContent = " 提交问题";
  }
}

function snapshotStamp(result) {
  if (!result.interaction_id) return "";
  return `<div class="snapshot-stamp"><span>记录 <b>${escapeHtml(result.interaction_id.slice(0, 8))}</b></span><span>证据快照 <b>${escapeHtml(result.evidence_snapshot_hash.slice(0, 12))}</b></span><span>截止日 <b>${escapeHtml(result.as_of)}</b></span></div>`;
}

function renderEvidenceReferences(ids, evidenceById) {
  return ids.map((id) => {
    const item = evidenceById.get(id);
    if (!item) return `<span>${escapeHtml(id)}</span>`;
    const citation = item.citation || {};
    const url = safeUrl(item.source);
    const label = `${citation.title || "公告原文"} · p.${citation.page_start || "—"}`;
    return url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`
      : `<span>${escapeHtml(label)}</span>`;
  }).join("");
}

function renderAssistantSources(evidence) {
  if (!evidence.length) return "";
  const citations = evidence.map((item) => {
    const citation = item.citation || {};
    const url = safeUrl(item.source);
    return `<article class="assistant-citation">
      <div><span>${escapeHtml(item.as_of || "日期未知")}</span><h4>${escapeHtml(citation.title || item.label)}</h4></div>
      <p>${escapeHtml(evidencePreview(item))}</p>
      ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">打开 p.${escapeHtml(citation.page_start || "—")}</a>` : ""}
    </article>`;
  }).join("");
  return `<section class="assistant-sources"><div class="assistant-section-title"><b>本次证据快照</b><span>${evidence.length} 个原文片段</span></div>${citations}</section>`;
}

function renderAssistantResult(result) {
  const answer = result.answer;
  const evidenceById = new Map(result.evidence.map((item) => [item.id, item]));
  const claims = answer.claims.map((claim) => {
    const references = renderEvidenceReferences(claim.evidence_ids, evidenceById);
    return `<article class="assistant-claim"><p>${escapeHtml(claim.statement)}</p><div class="assistant-refs">${references}</div></article>`;
  }).join("");
  const limitations = answer.limitations.length
    ? `<div class="assistant-limit"><strong>证据边界</strong><ul>${answer.limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`
    : "";
  $("#assistant-empty").hidden = true;
  $("#assistant-result").hidden = false;
  $("#assistant-result").innerHTML = `
    <header class="assistant-answer ${answer.status === "answered" ? "answered" : "insufficient"}">
      <span>${answer.status === "answered" ? "ANSWERED · CITATIONS VALIDATED" : "INSUFFICIENT EVIDENCE"}</span>
      <h3>${escapeHtml(answer.answer)}</h3>
    </header>
    ${snapshotStamp(result)}
    <div class="assistant-claims">${claims}</div>
    ${limitations}
    ${renderAssistantSources(result.evidence)}
  `;
}

async function generateFinancialTemplate() {
  if (!state.analysis) return;
  const button = $("#generate-financial-template");
  button.disabled = true;
  button.textContent = "正在生成";
  try {
    const result = await api("/api/financial-change-template", {
      method: "POST",
      body: JSON.stringify({code: state.code, as_of: $("#as-of-input").value || null}),
    });
    state.assistant = result;
    renderFinancialTemplate(result);
    $("#assistant-mode").textContent = result.mode === "deterministic" ? "程序已核算" : result.mode === "ai" ? "模板已生成" : "证据不足";
    await loadAssistantHistory(result.interaction_id);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "生成变化模板";
  }
}

function renderFinancialTemplate(result) {
  const template = result.template;
  const evidenceById = new Map(result.evidence.map((item) => [item.id, item]));
  const facts = result.financial_facts || result.evidence.filter((item) => item.financial_fact).map((item) => item.financial_fact);
  const directions = {improved: "改善", weakened: "减弱", mixed: "分化", stable: "稳定", unknown: "未判断"};
  const sections = template.sections.map((section) => `
    <article class="financial-section ${section.status === "supported" ? "" : "not-covered"}">
      <header><h4>${escapeHtml(section.label)}</h4><span class="direction-tag ${escapeHtml(section.direction)}">${escapeHtml(directions[section.direction] || "未判断")}</span></header>
      <p>${escapeHtml(section.summary)}</p>
      <div class="assistant-refs">${renderEvidenceReferences(section.evidence_ids, evidenceById)}</div>
    </article>
  `).join("");
  const limitations = template.limitations.length
    ? `<div class="assistant-limit"><strong>模板边界</strong><ul>${template.limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`
    : "";
  const formatValue = (value, unit) => {
    if (value === null || value === undefined) return "—";
    if (unit === "元") return `${(Number(value) / 1e8).toLocaleString("zh-CN", {maximumFractionDigits: 2})}亿元`;
    return `${Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 4})}${unit}`;
  };
  const factLedger = facts.length ? `
    <section class="financial-fact-ledger">
      <div class="assistant-section-title"><b>程序化财务事实</b><span>${facts.length} 项 · DECIMAL VERIFIED</span></div>
      <div class="fact-ledger-head"><span>指标</span><span>本期</span><span>比较期</span><span>变化</span></div>
      ${facts.map((fact) => `<div class="fact-ledger-row">
        <div><b>${escapeHtml(fact.metric_label)}</b><small>${escapeHtml(fact.period_end)} · p.${escapeHtml(fact.source_page)}</small></div>
        <strong>${escapeHtml(formatValue(fact.current_value, fact.normalized_unit))}</strong>
        <span>${escapeHtml(formatValue(fact.comparison_value, fact.normalized_unit))}<small>${escapeHtml(fact.comparison_period_end)}</small></span>
        <em class="${Number(fact.change_pct) > 0 ? "up" : Number(fact.change_pct) < 0 ? "down" : ""}">${fact.change_pct == null ? "—" : `${escapeHtml(Number(fact.change_pct).toFixed(2))}%`}</em>
      </div>`).join("")}
    </section>` : "";
  $("#assistant-empty").hidden = true;
  $("#assistant-result").hidden = false;
  $("#assistant-result").innerHTML = `
    <header class="financial-template-head"><span>FINANCIAL CHANGE · FIXED SCHEMA</span><h3>${escapeHtml(template.period_summary)}</h3><p>${escapeHtml(template.overall_assessment)}</p></header>
    ${snapshotStamp(result)}
    ${factLedger}
    <div class="financial-sections">${sections}</div>
    ${limitations}
    ${renderAssistantSources(result.evidence)}
  `;
}

async function loadAssistantHistory(activeId = null) {
  if (!state.code) return;
  try {
    const history = await api(`/api/stocks/${encodeURIComponent(state.code)}/assistant-history?limit=30`);
    $("#history-count").textContent = history.items.length;
    const list = $("#assistant-history-list");
    if (!history.items.length) {
      list.innerHTML = "<p>暂无记录</p>";
      return;
    }
    list.innerHTML = history.items.map((item) => `
      <button type="button" class="history-item ${item.interaction_id === activeId ? "active" : ""}" data-id="${escapeHtml(item.interaction_id)}">
        <span>${item.interaction_type === "financial_change_template" ? "财报模板" : "文档问答"} · ${escapeHtml(item.mode === "deterministic" ? "程序核算" : item.mode)}</span>
        <b>${escapeHtml(item.question)}</b>
        <small>${escapeHtml(item.created_at.slice(0, 16).replace("T", " "))} · ${item.evidence_count} 条证据</small>
      </button>
    `).join("");
    $$(".history-item").forEach((button) => button.addEventListener("click", () => openAssistantHistory(button.dataset.id)));
  } catch (error) {
    $("#assistant-history-list").innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

async function openAssistantHistory(interactionId) {
  try {
    const item = await api(`/api/research-interactions/${encodeURIComponent(interactionId)}`);
    const base = {
      mode: item.mode,
      evidence: item.evidence,
      as_of: item.as_of,
      interaction_id: item.interaction_id,
      evidence_snapshot_hash: item.evidence_snapshot_hash,
      created_at: item.created_at,
    };
    if (item.interaction_type === "financial_change_template") {
      renderFinancialTemplate({...base, template: item.result});
    } else {
      renderAssistantResult({...base, answer: item.result});
    }
    $$(".history-item").forEach((button) => button.classList.toggle("active", button.dataset.id === interactionId));
  } catch (error) {
    toast(error.message);
  }
}

function activateTab(tab) {
  $$(".research-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${tab}`));
  if (tab === "assistant") loadAssistantHistory();
  if (tab === "report") loadResearchRunHistory(state.researchRun?.run_id || null);
}

async function searchSecurities(query) {
  const box = $("#search-results");
  if (!query.trim()) { box.hidden = true; return; }
  try {
    const result = await api(`/api/securities?q=${encodeURIComponent(query)}&limit=8`);
    box.innerHTML = result.items.map((item) => `<button class="search-result" data-code="${escapeHtml(item.code)}"><b>${escapeHtml(item.name)}</b><span>${escapeHtml(item.code)}</span></button>`).join("");
    box.hidden = result.items.length === 0;
    $$(".search-result").forEach((button) => button.addEventListener("click", () => {
      box.hidden = true;
      $("#as-of-input").value = "";
      state.researchOrigin = null;
      activateWorkspace("company", "research");
      loadAnalysis(button.dataset.code);
    }));
  } catch (_) { box.hidden = true; }
}

async function loadTheses() {
  try {
    const result = await api("/api/theses");
    $("#thesis-count").textContent = result.items.length;
    const list = $("#thesis-list");
    if (!result.items.length) {
      list.innerHTML = `<div class="empty-state"><span class="empty-index">00</span><h2>暂无研究论点</h2><p>从研究台选择一只股票并建立第一条可证伪论点。</p></div>`;
      return;
    }
    list.innerHTML = result.items.map((item) => `
      <article class="thesis-item" data-id="${escapeHtml(item.id)}">
        <div class="thesis-meta"><b>${escapeHtml(item.security_code)}</b><br>${escapeHtml(item.horizon)}<br>${escapeHtml(item.updated_at.slice(0, 10))}</div>
        <div class="thesis-main"><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.core_claim)}</p>${item.invalidating_conditions.length ? `<p><b>证伪：</b>${escapeHtml(item.invalidating_conditions.join("；"))}</p>` : ""}</div>
        <div class="thesis-actions">
          <select aria-label="论点状态">${["观察","验证中","基本成立","证据减弱","已经证伪","研究终止"].map((status) => `<option ${status === item.status ? "selected" : ""}>${status}</option>`).join("")}</select>
          <button type="button" data-action="delete">删除论点</button>
        </div>
        <section class="monitor-strip">
          <div class="monitor-stats">
            <span>监控基线 <b>${item.monitor.baseline ? escapeHtml(item.monitor.baseline.as_of) : "未设置"}</b></span>
            <span>CLAIM <b>${item.monitor.claim_count}</b></span>
            <span>待确认 <b class="${item.monitor.pending_evaluations ? "pending" : ""}">${item.monitor.pending_evaluations}</b></span>
          </div>
          <div class="monitor-actions">
            <button type="button" data-action="baseline">${item.monitor.baseline ? "重置为最新运行" : "设置最新运行为基线"}</button>
            <button type="button" data-action="check" ${item.monitor.baseline ? "" : "disabled"}>检查更新</button>
            <button type="button" data-action="detail" ${item.monitor.baseline ? "" : "disabled"}>查看评估</button>
          </div>
          <div class="monitor-detail" hidden></div>
        </section>
      </article>
    `).join("");
    $$(".thesis-item").forEach((item) => {
      item.querySelector("select").addEventListener("change", async (event) => {
        await api(`/api/theses/${item.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: event.target.value }) });
        toast("论点状态已更新");
      });
      item.querySelector('[data-action="delete"]').addEventListener("click", async () => {
        await api(`/api/theses/${item.dataset.id}`, { method: "DELETE" });
        await loadTheses();
        toast("论点已删除");
      });
      item.querySelector('[data-action="baseline"]').addEventListener("click", () => setMonitorBaseline(item.dataset.id));
      item.querySelector('[data-action="check"]').addEventListener("click", () => checkMonitor(item.dataset.id));
      item.querySelector('[data-action="detail"]').addEventListener("click", () => showMonitorDetail(item.dataset.id));
    });
  } catch (error) { toast(error.message); }
}

function evidenceValue(item) {
  if (!item) return "无";
  const date = item.period_end || item.as_of || "日期未知";
  return `${escapeHtml(String(item.value || "未提供"))} · ${escapeHtml(String(date))}`;
}

function renderMonitorDetail(detail) {
  if (!detail.claims.length) return `<p class="monitor-empty">当前基线没有可监控的确定性 Claim。</p>`;
  const evaluations = detail.evaluations.map((evaluation) => {
    const changes = evaluation.changes.length
      ? evaluation.changes.map((change) => `
          <li>
            <b>${escapeHtml(change.business_key)}</b><span>${escapeHtml(change.change_type)}</span>
            <small>基线：${evidenceValue(change.baseline)}</small>
            <small>当前：${evidenceValue(change.current)}</small>
          </li>`).join("")
      : `<li class="no-change">证据指纹未发生变化</li>`;
    const decision = evaluation.status === "confirmed"
      ? `<span class="confirmed-verdict">已确认 · ${escapeHtml(evaluation.confirmed_verdict)}</span>`
      : evaluation.status === "pending"
        ? `<div class="verdict-actions">${["增强", "维持", "减弱", "证伪", "无法判断"].map((verdict) => `<button type="button" data-evaluation-id="${escapeHtml(evaluation.evaluation_id)}" data-verdict="${verdict}">${verdict}</button>`).join("")}</div>`
        : `<span class="superseded-verdict">已被新基线取代</span>`;
    return `
      <article class="monitor-evaluation">
        <header><b>${escapeHtml(evaluation.statement)}</b><span class="suggested-verdict">建议 · ${escapeHtml(evaluation.suggested_verdict)}</span></header>
        <p>${escapeHtml(evaluation.rationale)}</p>
        <ul>${changes}</ul>
        ${decision}
      </article>`;
  }).join("");
  const claims = detail.claims.map((claim) => `<li><b>${escapeHtml(claim.statement)}</b><span>最近判断：${escapeHtml(claim.last_verdict || "尚未确认")}</span></li>`).join("");
  return `
    <div class="monitor-baseline">基线运行 <b>${escapeHtml(detail.thesis.monitor.baseline.run_id.slice(0, 8))}</b> · 截止 ${escapeHtml(detail.thesis.monitor.baseline.as_of)} · 工作流 ${escapeHtml(detail.thesis.monitor.baseline.workflow_version)}</div>
    <ul class="monitor-claims">${claims}</ul>
    ${evaluations || `<p class="monitor-empty">尚无更新评估。完成新的公司研究运行后点击“检查更新”。</p>`}
  `;
}

async function showMonitorDetail(thesisId) {
  const card = document.querySelector(`.thesis-item[data-id="${CSS.escape(thesisId)}"]`);
  if (!card) return;
  const box = card.querySelector(".monitor-detail");
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<p class="monitor-empty loading">正在读取监控快照…</p>`;
  try {
    const detail = await api(`/api/theses/${thesisId}/monitor`);
    box.innerHTML = renderMonitorDetail(detail);
    box.querySelectorAll("[data-evaluation-id]").forEach((button) => button.addEventListener("click", async () => {
      try {
        await api(`/api/claim-evaluations/${button.dataset.evaluationId}`, {
          method: "PATCH",
          body: JSON.stringify({ verdict: button.dataset.verdict }),
        });
        toast(`已确认：${button.dataset.verdict}`);
        box.hidden = true;
        await loadTheses();
        await showMonitorDetail(thesisId);
      } catch (error) { toast(error.message); }
    }));
  } catch (error) {
    box.hidden = true;
    toast(error.message);
  }
}

async function setMonitorBaseline(thesisId) {
  try {
    const result = await api(`/api/theses/${thesisId}/monitor/baseline`, { method: "POST", body: "{}" });
    await loadTheses();
    toast(`监控基线已建立，导入 ${result.claims_imported} 条 Claim`);
    await showMonitorDetail(thesisId);
  } catch (error) { toast(error.message); }
}

async function checkMonitor(thesisId) {
  try {
    const result = await api(`/api/theses/${thesisId}/monitor/check`, { method: "POST", body: "{}" });
    await loadTheses();
    toast(`已生成 ${result.evaluations.length} 条待确认评估`);
    await showMonitorDetail(thesisId);
  } catch (error) { toast(error.message); }
}

async function saveThesis(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/theses", {
      method: "POST",
      body: JSON.stringify({
        code: state.code,
        title: form.get("title"),
        core_claim: form.get("core_claim"),
        horizon: form.get("horizon"),
        status: form.get("status"),
        invalidating_conditions: String(form.get("invalidating") || "").split("\n").map((line) => line.trim()).filter(Boolean),
      }),
    });
    $("#thesis-dialog").close();
    event.currentTarget.reset();
    await loadTheses();
    toast("研究论点已保存");
  } catch (error) { toast(error.message); }
}

function percentValue(value) {
  return formatPercent(value);
}

function turnoverValue(value) {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) / 100000000).toFixed(2)}亿`;
}

function renderQuantSummary(snapshot) {
  const summary = $("#quant-summary");
  if (!snapshot) {
    summary.innerHTML = `<div><span>快照状态</span><strong>尚未生成</strong><small>手工运行</small></div><div><span>正常排名</span><strong>—</strong><small>通过质量门禁</small></div><div><span>质量隔离</span><strong>—</strong><small>不参与排名</small></div><div><span>因子版本</span><strong>—</strong><small>可复现口径</small></div>`;
    return;
  }
  summary.innerHTML = `
    <div><span>快照截止日</span><strong>${escapeHtml(snapshot.as_of)}</strong><small>${escapeHtml(snapshot.status)}</small></div>
    <div><span>正常排名</span><strong>${Number(snapshot.passed_securities).toLocaleString()}</strong><small>通过质量门禁</small></div>
    <div><span>质量隔离</span><strong>${Number(snapshot.excluded_securities).toLocaleString()}</strong><small>不参与正常排名</small></div>
    <div><span>因子版本</span><strong>${escapeHtml(snapshot.factor_version)}</strong><small>${escapeHtml(snapshot.snapshot_id.slice(0, 8))}</small></div>`;
}

function factorQuery() {
  const form = new FormData($("#factor-filters"));
  const params = new URLSearchParams({
    q: String(form.get("q") || ""),
    exchange: String(form.get("exchange")),
    quality_status: String(form.get("quality_status")),
    sort: String(form.get("sort")),
    direction: String(form.get("direction")),
    limit: "100",
  });
  const percentages = ["min_return_20d", "min_return_60d", "max_volatility_60d"];
  percentages.forEach((name) => {
    const value = String(form.get(name) || "").trim();
    if (value) params.set(name, String(Number(value) / 100));
  });
  const turnover = String(form.get("min_turnover") || "").trim();
  if (turnover) params.set("min_avg_traded_value_20d", String(Number(turnover) * 100000000));
  return params;
}

const qualityLabels = {
  no_history: "无历史行情",
  no_valid_close: "无有效收盘",
  latest_close_missing: "最新收盘缺失",
  stale_latest_trade: "行情已过期",
  insufficient_observations: "样本不足251期",
  incomplete_ohlcv: "OHLCV字段缺失",
  invalid_ohlcv: "OHLC边界异常",
  invalid_vwap: "VWAP边界异常",
  unadjusted_price_jump: "未复权价格跳变",
  latest_suspended_or_incomplete: "最新日停牌或字段不全",
  insufficient_liquidity_samples: "成交额样本不足",
  incomplete_factor_set: "因子不完整",
};

function bindFactorActions(root) {
  [...root.querySelectorAll("[data-factor-action]")].forEach((button) => button.addEventListener("click", async () => {
    try {
      if (button.dataset.factorAction === "add") {
        await api("/api/research-candidates", {
          method: "POST",
          body: JSON.stringify({ code: button.dataset.code, snapshot_id: state.factorSnapshot.snapshot_id }),
        });
        toast("已加入研究候选池");
      } else {
        await api(`/api/research-candidates/${encodeURIComponent(button.dataset.code)}`, { method: "DELETE" });
        toast("已移出研究候选池");
      }
      await Promise.all([loadFactorRows(), loadCandidates()]);
    } catch (error) { toast(error.message); }
  }));
}

async function loadFactorRows() {
  if (!state.factorSnapshot) return;
  const body = $("#factor-table-body");
  body.innerHTML = `<tr><td colspan="8" class="loading">正在筛选因子快照…</td></tr>`;
  try {
    const result = await api(`/api/factor-snapshots/${encodeURIComponent(state.factorSnapshot.snapshot_id)}/securities?${factorQuery()}`);
    $("#factor-result-count").textContent = `显示 ${result.items.length} / ${result.total.toLocaleString()} 只证券`;
    if (!result.items.length) {
      body.innerHTML = `<tr><td colspan="8">当前条件下没有证券</td></tr>`;
      return;
    }
    body.innerHTML = result.items.map((item) => {
      const quality = item.quality_status === "passed"
        ? renderStatusBadge("passed", "正常")
        : renderStatusBadge("excluded", "质量异常", item.quality_reasons.map((reason) => qualityLabels[reason] || reason).join("；"));
      const action = item.quality_status !== "passed"
        ? quality
        : `<button type="button" data-factor-action="${item.in_candidate_pool ? "remove" : "add"}" data-code="${escapeHtml(item.security_code)}">${item.in_candidate_pool ? "移出候选" : "加入候选"}</button>`;
      return `<tr>
        <td>${renderSecurityIdentity(item, {date:item.latest_trade_date || "无日期"})}</td>
        <td class="${Number(item.return_20d) >= 0 ? "up" : "down"}">${percentValue(item.return_20d)}</td>
        <td class="${Number(item.return_60d) >= 0 ? "up" : "down"}">${percentValue(item.return_60d)}</td>
        <td>${percentValue(item.volatility_60d)}</td>
        <td>${turnoverValue(item.avg_traded_value_20d)}</td>
        <td class="down">${percentValue(item.max_drawdown_250d)}</td>
        <td>${percentValue(item.range_position_52w)}</td>
        <td>${action}</td>
      </tr>`;
    }).join("");
    bindFactorActions(body);
  } catch (error) {
    body.innerHTML = `<tr><td colspan="8">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function loadCandidates() {
  try {
    const result = await api("/api/research-candidates");
    $("#candidate-count").textContent = result.items.length;
    $("#candidate-pool-count").textContent = result.items.length;
    const list = $("#candidate-list");
    if (!result.items.length) {
      list.innerHTML = `<p>暂无候选证券</p>`;
      return;
    }
    list.innerHTML = result.items.map((item) => `
      <article class="candidate-item">
        ${renderSecurityIdentity(item, {date:item.as_of})}
        <dl><dt>60日</dt><dd class="${Number(item.return_60d) >= 0 ? "up" : "down"}">${percentValue(item.return_60d)}</dd><dt>波动</dt><dd>${percentValue(item.volatility_60d)}</dd></dl>
        <div class="candidate-actions"><button type="button" data-candidate-open="${escapeHtml(item.security_code)}" data-snapshot-id="${escapeHtml(item.source_snapshot_id)}">进入研究</button><button type="button" data-factor-action="remove" data-code="${escapeHtml(item.security_code)}">移除</button></div>
      </article>`).join("");
    bindFactorActions(list);
    $$('[data-candidate-open]').forEach((button) => button.addEventListener("click", () => {
      state.researchOrigin = {
        securityCode: button.dataset.candidateOpen,
        factorSnapshotId: button.dataset.snapshotId,
      };
      activateWorkspace("company", "research");
      loadAnalysis(button.dataset.candidateOpen);
    }));
  } catch (error) { toast(error.message); }
}

async function loadQuant() {
  try {
    const snapshot = await api("/api/factor-snapshots/latest");
    state.factorSnapshot = snapshot;
    $("#quant-as-of").value = snapshot.as_of;
    renderQuantSummary(snapshot);
    await Promise.all([loadFactorRows(), loadCandidates()]);
  } catch (error) {
    state.factorSnapshot = null;
    renderQuantSummary(null);
    $("#factor-table-body").innerHTML = `<tr><td colspan="8">尚未生成全市场因子快照</td></tr>`;
    await loadCandidates();
  }
}

async function generateFactorSnapshot() {
  const button = $("#generate-factor-snapshot");
  button.disabled = true;
  button.textContent = "计算中";
  try {
    const asOf = $("#quant-as-of").value || null;
    const snapshot = await api("/api/factor-snapshots", {
      method: "POST",
      body: JSON.stringify({ as_of: asOf }),
    });
    state.factorSnapshot = snapshot;
    $("#quant-as-of").value = snapshot.as_of;
    renderQuantSummary(snapshot);
    await Promise.all([loadFactorRows(), loadCandidates()]);
    toast(snapshot.reused ? "已加载相同输入的历史快照" : `快照完成：${snapshot.passed_securities} 只进入排名`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "生成快照"; }
}

function modelMetric(metrics, key, percent = false) {
  const value = Number(metrics?.[key]);
  if (!Number.isFinite(value)) return "—";
  return percent ? `${(value * 100).toFixed(2)}%` : value.toFixed(4);
}

function renderModelRun(model, validation, currentShadow) {
  state.modelRun = model;
  state.modelValidation = validation;
  state.currentShadow = currentShadow;
  const snapshot = model.prediction_snapshot;
  const boundary = $("#model-boundary");
  const currentReady = currentShadow?.status === "current_shadow_ready";
  const modelLimitations = currentReady ? [
    "历史预测和实现标签仅覆盖 2017-01-03 至 2020-07-31，只用于滚动 OOS 复核与历史回放。",
    "当前推理使用独立固化的 iFinD / 本地行情输入；数据来源和指纹与历史 Qlib 数据分别追踪。",
    "滚动 OOS 复核评价冻结模型的连续样本外窗口，不是滚动重训，也不能覆盖所有市场阶段。",
    "当前信号尚无实现标签，统一标记为待评价；不得进入候选池、决策准入或交易建议。",
  ] : model.limitations;
  boundary.className = `model-boundary ${currentReady ? "current" : "historical"}`;
  boundary.innerHTML = currentReady ? `
    <div><span>MODEL ADMISSION</span><strong>当前 Shadow 已就绪 · 观察专用</strong></div>
    <p>信号截止 ${escapeHtml(currentShadow.as_of)} · ${currentShadow.signal_count}/${currentShadow.universe_size} 只<br>候选池与决策准入：禁止</p>` : `
    <div><span>MODEL ADMISSION</span><strong>已接纳 · 仅限历史 Shadow 验证</strong></div>
    <p>预测覆盖 ${escapeHtml(snapshot.start_date)} — ${escapeHtml(snapshot.end_date)}<br>当前研究准入：禁止</p>`;
  $("#model-nav-status").textContent = currentReady ? "NOW" : "HIST";
  $("#model-nav-status").className = `acceptance-nav-status ${currentReady ? "passed" : "historical"}`;
  const validationMetrics = validation?.metrics || {};
  $("#model-validation-state").textContent = validation?.status === "passed"
    ? `${validationMetrics.window_count} 个滚动 OOS 窗口 · ${(Number(validationMetrics.positive_window_ratio) * 100).toFixed(1)}% 为正`
    : "滚动 OOS 门禁未通过";
  $("#model-summary").innerHTML = `
    <div><span>滚动 Rank IC</span><strong>${Number(validationMetrics.mean_rank_ic ?? model.metrics["Rank IC"]).toFixed(4)}</strong><small>冻结模型样本外</small></div>
    <div><span>正向窗口</span><strong>${Number.isFinite(Number(validationMetrics.positive_window_ratio)) ? `${(Number(validationMetrics.positive_window_ratio) * 100).toFixed(1)}%` : "—"}</strong><small>${validationMetrics.window_count || "—"} 个 63日窗口</small></div>
    <div><span>当前覆盖</span><strong>${currentReady ? `${(Number(currentShadow.coverage) * 100).toFixed(1)}%` : "—"}</strong><small>${currentReady ? `${currentShadow.signal_count} 只当前成分` : "尚未通过当前门禁"}</small></div>
    <div><span>当前特征</span><strong>${currentReady ? currentShadow.feature_count : "—"}</strong><small>Alpha158 · CPS:2</small></div>`;
  $("#signal-as-of").min = snapshot.start_date;
  $("#signal-as-of").max = snapshot.end_date;
  if (!$("#signal-as-of").value) $("#signal-as-of").value = snapshot.end_date;
  const currentButton = $('[data-signal-mode="current"]');
  currentButton.disabled = !currentReady;
  state.signalMode = currentReady ? "current" : "historical";
  syncSignalMode();
  const config = model.config || {};
  const artifacts = model.artifacts || [];
  $("#model-provenance-content").innerHTML = `
    <dl class="model-contract">
      <dt>Run ID</dt><dd><code>${escapeHtml(model.model_run_id)}</code></dd>
      <dt>模型</dt><dd>${escapeHtml(model.model_class)} · ${escapeHtml(model.feature_set)}</dd>
      <dt>股票池</dt><dd>${escapeHtml(String(model.universe).toUpperCase())} 历史成分${currentReady ? " / 当前沪深300" : ""}</dd>
      <dt>训练</dt><dd>${escapeHtml(model.train_start)} — ${escapeHtml(model.train_end)}</dd>
      <dt>验证</dt><dd>${escapeHtml(model.valid_start)} — ${escapeHtml(model.valid_end)}</dd>
      <dt>测试</dt><dd>${escapeHtml(model.test_start)} — ${escapeHtml(model.test_end)}</dd>
      <dt>框架</dt><dd>${escapeHtml(model.framework)}</dd>
      <dt>交易日</dt><dd>${Number(config.prediction_trading_days || 0).toLocaleString()}</dd>
      <dt>当前数据</dt><dd>${currentReady ? `${escapeHtml(currentShadow.data_start)} — ${escapeHtml(currentShadow.data_end)}` : "未通过"}</dd>
      <dt>当前指纹</dt><dd><code>${currentReady ? escapeHtml(shortHash(currentShadow.data_fingerprint)) : "—"}</code></dd>
    </dl>
    <div class="artifact-ledger"><b>归档产物</b>${artifacts.map((item) => `<p><span>${escapeHtml(item.artifact_type)}</span><code>${escapeHtml(shortHash(item.sha256))}</code></p>`).join("")}</div>
    ${currentReady ? `<div class="model-limits"><b>当前数据门禁</b><ol>${currentShadow.gates.map((item) => `<li>${item.passed ? "通过" : "拒绝"} · ${escapeHtml(item.name)}：${escapeHtml(String(item.observed))}</li>`).join("")}</ol></div>` : ""}
    <div class="model-limits"><b>强制边界</b><ol>${modelLimitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol></div>`;
}

function syncSignalMode() {
  $$('[data-signal-mode]').forEach((button) => button.classList.toggle("active", button.dataset.signalMode === state.signalMode));
  $("#signal-date-field").classList.toggle("mode-hidden", state.signalMode === "current");
  $("#signal-table-title").textContent = state.signalMode === "current" ? "当前 Shadow Signal" : "历史 Shadow Signal";
}

async function loadModelSignals() {
  if (!state.modelRun) return;
  const form = new FormData($("#signal-filters"));
  const params = new URLSearchParams({
    q: form.get("q") || "",
    sort: form.get("sort") || "rank",
    direction: form.get("direction") || "asc",
    limit: "100",
  });
  if (state.signalMode === "historical") {
    params.set("as_of", form.get("as_of") || state.modelRun.prediction_snapshot.end_date);
  }
  try {
    const current = state.signalMode === "current";
    const path = current
      ? `/api/current-shadow/${encodeURIComponent(state.currentShadow.snapshot_id)}/signals?${params}`
      : `/api/model-runs/${encodeURIComponent(state.modelRun.model_run_id)}/signals?${params}`;
    const data = await api(path);
    const asOf = current ? data.snapshot.as_of : data.as_of;
    $("#signal-result-count").textContent = `${data.total.toLocaleString()} 只证券 · ${asOf}`;
    const body = $("#signal-table-body");
    if (!data.items.length) {
      body.innerHTML = `<tr><td colspan="6">该日期没有模型信号，请选择实际交易日</td></tr>`;
      return;
    }
    body.innerHTML = data.items.map((item) => `
      <tr>
        <td><b>#${item.cross_section_rank}</b><small>/ ${item.cross_section_size}</small></td>
        <td>${renderSecurityIdentity(item)}</td>
        <td class="${item.score >= 0 ? "up" : "down"}">${Number(item.score).toFixed(6)}</td>
        <td>${Number(item.percentile).toFixed(2)}%</td>
        <td class="${item.realized_label == null ? "" : Number(item.realized_label) >= 0 ? "up" : "down"}">${item.realized_label == null ? (current ? "待评价" : "—") : `${(Number(item.realized_label) * 100).toFixed(3)}%`}</td>
        <td>${renderStatusBadge(current ? "current" : "historical", current ? "CURRENT SHADOW" : "HISTORICAL")}</td>
      </tr>`).join("");
  } catch (error) { toast(error.message); }
}

async function loadModelRun() {
  try {
    const response = await api("/api/model-runs/latest");
    if (!response.item) throw new Error("尚未导入 Qlib 模型运行");
    const [validationResponse, currentResponse] = await Promise.all([
      api(`/api/model-runs/${encodeURIComponent(response.item.model_run_id)}/validation`),
      api("/api/current-shadow/latest"),
    ]);
    renderModelRun(response.item, validationResponse.item, currentResponse.item);
    await loadModelSignals();
  } catch (error) {
    state.modelRun = null;
    $("#signal-table-body").innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
  }
}

const decisionStatusLabels = {
  excluded: "规则排除",
  insufficient_evidence: "证据不足",
  watch: "继续观察",
  research_required: "退回研究",
  eligible_for_review: "可人工审查",
};

const reviewStatusLabels = {
  pending: "待审批",
  approve_for_tracking: "进入 Shadow 跟踪",
  return_for_research: "已退回研究",
  reject: "已拒绝",
  policy_superseded: "策略已替代",
};

const outcomeStatusLabels = {
  pending: "周期未满",
  completed: "评价完成",
  data_rejected: "数据拒绝",
};

function percent(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${(number * 100).toFixed(2)}%` : "—";
}

function decisionScore(item, key) {
  const value = Number(item.scores?.[key]?.score);
  return Number.isFinite(value) ? value.toFixed(1) : "—";
}

function renderDecisionSummary(items) {
  const pending = items.filter((item) => item.review_status === "pending").length;
  const eligible = items.filter((item) => item.rule_status === "eligible_for_review").length;
  const tracking = items.filter((item) => item.review_status === "approve_for_tracking").length;
  $("#decision-count").textContent = items.length;
  $("#decision-summary").innerHTML = `
    <div><span>案例总数</span><strong>${items.length}</strong><small>不可变快照</small></div>
    <div><span>待人工审批</span><strong>${pending}</strong><small>规则已先行</small></div>
    <div><span>可进入审查</span><strong>${eligible}</strong><small>不等于交易许可</small></div>
    <div><span>已进入跟踪</span><strong>${tracking}</strong><small>M8 Shadow</small></div>`;
}

function renderDecisionCaseList(items) {
  const list = $("#decision-case-list");
  $("#decision-list-count").textContent = items.length;
  if (!items.length) {
    list.innerHTML = "<p>暂无决策案例</p>";
    return;
  }
  list.innerHTML = items.map((item) => `
    <button type="button" class="decision-case-item ${state.selectedDecisionCase?.case_id === item.case_id ? "active" : ""}" data-decision-case="${escapeHtml(item.case_id)}">
      <span class="decision-status ${escapeHtml(item.rule_status)}">${escapeHtml(decisionStatusLabels[item.rule_status] || item.rule_status)}</span>
      <b>${escapeHtml(item.security_name)} <small>${escapeHtml(item.security_code)}</small></b>
      <em>${escapeHtml(item.as_of)} · ${item.decision_horizon_days}日 / ${escapeHtml(item.benchmark_name)}</em>
      <i>${escapeHtml(outcomeStatusLabels[state.decisionOutcomes[item.case_id]?.status] || reviewStatusLabels[item.review_status] || item.review_status)}</i>
    </button>`).join("");
  $$('[data-decision-case]').forEach((button) => button.addEventListener("click", () => {
    state.selectedDecisionCase = state.decisionCases.find((item) => item.case_id === button.dataset.decisionCase);
    renderDecisionCaseList(state.decisionCases);
    renderDecisionDetail(state.selectedDecisionCase);
  }));
}

function renderDecisionDetail(item) {
  const target = $("#decision-detail");
  if (!item) {
    target.innerHTML = `<div class="decision-empty"><span>M7</span><h2>尚未选择决策案例</h2><p>案例必须绑定同一截止日的研究、证据、量化上下文与当前 Shadow。</p></div>`;
    return;
  }
  const artifacts = item.snapshot?.source_refs?.artifacts || {};
  const review = item.reviews?.[item.reviews.length - 1];
  const canApprove = item.rule_status === "eligible_for_review" && item.review_status === "pending";
  const pending = item.review_status === "pending" && item.policy_current !== false;
  const outcome = state.decisionOutcomes[item.case_id];
  const outcomeFailed = outcome?.gates?.filter((gate) => !gate.passed) || [];
  const outcomePanel = outcome?.status === "completed" ? `
    <div class="outcome-metrics">
      <div><span>证券收益</span><strong class="${outcome.security_return >= 0 ? "positive" : "negative"}">${percent(outcome.security_return)}</strong><small>${escapeHtml(outcome.entry_date)} → ${escapeHtml(outcome.exit_date)}</small></div>
      <div><span>基准收益</span><strong>${percent(outcome.benchmark_return)}</strong><small>${escapeHtml(item.benchmark_name)}</small></div>
      <div><span>超额收益</span><strong class="${outcome.excess_return >= 0 ? "positive" : "negative"}">${percent(outcome.excess_return)}</strong><small>证券收益 − 基准</small></div>
      <div><span>最大不利波动</span><strong class="negative">${percent(outcome.maximum_adverse_excursion)}</strong><small>相对入场前复权收盘</small></div>
    </div>` : outcome?.status === "pending" ? `
    <div class="outcome-progress"><div><b>${outcome.metrics.observed_trading_days} / ${outcome.metrics.required_trading_days} 交易日</b><span>剩余 ${outcome.metrics.remaining_trading_days} 日</span></div><progress max="1" value="${outcome.metrics.progress}"></progress><p>完整周期尚未结束，不展示部分收益。</p></div>` : outcome ? `
    <div class="outcome-rejected"><b>行情数据合同未通过</b><p>${outcomeFailed.map((gate) => escapeHtml(`${gate.name}: ${gate.detail}`)).join("<br>")}</p></div>` : `<div class="outcome-progress"><p>尚未创建结果快照。评价会通过 iFinD 获取 CPS:2 前复权证券与基准日线。</p></div>`;
  target.innerHTML = `
    <header class="decision-detail-head">
      <div><span class="decision-status ${escapeHtml(item.rule_status)}">${escapeHtml(decisionStatusLabels[item.rule_status] || item.rule_status)}</span><h2>${escapeHtml(item.security_name)} · ${escapeHtml(item.security_code)}</h2><p>${escapeHtml(item.as_of)} · ${item.decision_horizon_days}交易日 · 基准 ${escapeHtml(item.benchmark_name)}</p></div>
      <code title="案例快照哈希">${escapeHtml(shortHash(item.snapshot_hash))}</code>
    </header>
    <div class="decision-score-grid">
      <div><span>吸引力</span><strong>${decisionScore(item, "attractiveness")}</strong><small>Shadow / 动量</small></div>
      <div><span>证据可信度</span><strong>${decisionScore(item, "evidence_confidence")}</strong><small>事实 / 引用 / 时点</small></div>
      <div><span>风险严重度</span><strong>${decisionScore(item, "risk_severity")}</strong><small>越高风险越大</small></div>
    </div>
    <section class="decision-outcome">
      <header><div><span>DECISION OUTCOME</span><b>周期结果</b></div><div><strong class="outcome-status ${escapeHtml(outcome?.status || "empty")}">${escapeHtml(outcomeStatusLabels[outcome?.status] || "尚未评价")}</strong><button type="button" class="btn secondary" id="evaluate-decision-outcome">评价结果</button></div></header>
      ${outcomePanel}
      ${outcome ? `<p class="outcome-contract">${escapeHtml(outcome.evaluation_version)} · ${escapeHtml(outcome.adjustment)} / ${escapeHtml(outcome.ifind_params)} · ${escapeHtml(shortHash(outcome.data_fingerprint))}</p>` : ""}
    </section>
    <div class="decision-detail-grid">
      <section class="decision-gates"><header><span>POLICY GATES</span><b>确定性准入门禁</b></header><div>${item.gates.map((gate) => `
        <div class="decision-gate ${gate.passed ? "passed" : "failed"}"><i>${gate.passed ? "PASS" : "STOP"}</i><b>${escapeHtml(gate.name)}</b><span>${escapeHtml(String(gate.observed ?? "—"))}</span><small>${escapeHtml(gate.detail)}</small></div>`).join("")}</div></section>
      <aside class="decision-contract"><header><span>FROZEN SOURCES</span><b>案例数据合同</b></header>
        <dl><dt>策略</dt><dd>${escapeHtml(item.policy_version)}</dd><dt>ResearchRun</dt><dd><code>${escapeHtml(shortHash(item.research_run_id))}</code></dd><dt>Evidence</dt><dd><code>${escapeHtml(shortHash(artifacts.evidence_pack?.snapshot_hash))}</code></dd><dt>Company</dt><dd><code>${escapeHtml(shortHash(artifacts.company_snapshot?.snapshot_hash))}</code></dd><dt>Quant</dt><dd><code>${escapeHtml(shortHash(artifacts.quant_context?.snapshot_hash))}</code></dd><dt>Thesis</dt><dd><code>${escapeHtml(shortHash(item.thesis_id))}</code></dd><dt>Shadow</dt><dd><code>${escapeHtml(shortHash(item.shadow_snapshot_id))}</code></dd></dl>
      </aside>
    </div>
    <section class="decision-review">
      <header><div><span>HUMAN APPROVAL</span><b>人工审批</b></div><strong>${escapeHtml(reviewStatusLabels[item.review_status] || item.review_status)}</strong></header>
      ${review ? `<div class="decision-review-record"><b>${escapeHtml(review.reviewer)}</b><span>${escapeHtml(new Date(review.created_at).toLocaleString("zh-CN", {hour12:false}))}</span><p>${escapeHtml(review.note)}</p></div>` : pending ? `
        <div class="decision-review-form"><label>审批人<input id="decision-reviewer" value="human" maxlength="80"></label><label>审批说明<textarea id="decision-review-note" rows="3" maxlength="1000" placeholder="记录证据、风险和退回原因" required></textarea></label><div class="decision-review-actions">
          ${canApprove ? `<button type="button" class="btn primary" data-decision-review="approve_for_tracking">进入 Shadow 跟踪</button>` : ""}
          <button type="button" class="btn secondary" data-decision-review="return_for_research">退回研究</button>
          <button type="button" class="btn danger" data-decision-review="reject">拒绝案例</button>
        </div></div>` : `<div class="decision-review-record"><b>策略版本已替代</b><p>该快照只保留用于审计，必须使用当前策略重新建立案例后才能审批。</p></div>`}
      <p class="decision-boundary">人工批准只授权进入 M8 Shadow 评价，不授权建仓、调仓或下单。</p>
    </section>`;
  if (pending) {
    $$('[data-decision-review]').forEach((button) => button.addEventListener("click", () => submitDecisionReview(item.case_id, button.dataset.decisionReview)));
  }
  $("#evaluate-decision-outcome")?.addEventListener("click", () => evaluateDecisionOutcome(item.case_id));
}

async function loadDecisionCases(selectedId = state.selectedDecisionCase?.case_id) {
  try {
    const [result, outcomes, attribution] = await Promise.all([
      api("/api/decision-cases?limit=100"),
      api("/api/decision-outcomes?limit=500"),
      api("/api/decision-outcomes/attribution"),
    ]);
    state.decisionOutcomes = {};
    outcomes.items.forEach((outcome) => { if (!state.decisionOutcomes[outcome.case_id]) state.decisionOutcomes[outcome.case_id] = outcome; });
    state.outcomeAttribution = attribution;
    state.decisionCases = result.items;
    state.selectedDecisionCase = result.items.find((item) => item.case_id === selectedId) || result.items[0] || null;
    renderDecisionSummary(result.items);
    renderDecisionCaseList(result.items);
    renderDecisionDetail(state.selectedDecisionCase);
    renderOutcomeAttribution(attribution);
  } catch (error) { toast(error.message); }
}

async function evaluateDecisionOutcome(caseId) {
  const button = $("#evaluate-decision-outcome");
  if (button) { button.disabled = true; button.textContent = "读取行情中"; }
  try {
    const result = await api(`/api/decision-cases/${encodeURIComponent(caseId)}/outcome`, {
      method: "POST", body: JSON.stringify({}),
    });
    await loadDecisionCases(caseId);
    toast(outcomeStatusLabels[result.status] || result.status);
  } catch (error) { toast(error.message); }
  finally { if (button) { button.disabled = false; button.textContent = "评价结果"; } }
}

function renderOutcomeAttribution(result) {
  const overall = result?.overall || {};
  const counts = result?.counts || {};
  $("#outcome-attribution-count").textContent = `${counts.completed || 0} 个完整案例`;
  $("#outcome-attribution-summary").innerHTML = `
    <div><span>已完成 / 待成熟 / 拒绝</span><strong>${counts.completed || 0} / ${counts.pending || 0} / ${counts.data_rejected || 0}</strong></div>
    <div><span>平均证券收益</span><strong>${percent(overall.mean_security_return)}</strong></div>
    <div><span>平均超额收益</span><strong>${percent(overall.mean_excess_return)}</strong></div>
    <div><span>超额为正比例</span><strong>${percent(overall.positive_excess_ratio)}</strong></div>
    <div><span>平均 MAE</span><strong>${percent(overall.mean_maximum_adverse_excursion)}</strong></div>`;
  const dimensionLabels = {rule_status:"规则状态", review_status:"人工审批", decision_horizon_days:"决策周期", policy_version:"策略版本", benchmark_code:"基准", attractiveness_bucket:"吸引力", evidence_confidence_bucket:"证据可信度", risk_severity_bucket:"风险严重度"};
  const rows = Object.entries(result?.groups || {}).flatMap(([dimension, groups]) => groups.map((group) => `
    <tr><td>${escapeHtml(dimensionLabels[dimension] || dimension)}</td><td>${escapeHtml(group.key)}</td><td>${group.count}</td><td>${percent(group.mean_excess_return)}</td><td>${percent(group.positive_excess_ratio)}</td><td>${percent(group.mean_maximum_adverse_excursion)}</td><td><span class="sample-flag ${group.reliable ? "reliable" : "low"}">${group.reliable ? "可观察" : "低样本"}</span></td></tr>`));
  $("#outcome-attribution-body").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="7">完整周期结果不足，暂无可归因分组</td></tr>`;
  $("#outcome-attribution-boundary").textContent = result?.boundary || "仅使用完整周期结果。";
}

async function createDecisionCase(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "规则核验中";
  try {
    const item = await api("/api/decision-cases", {
      method: "POST",
      body: JSON.stringify({
        code: form.get("code"),
        as_of: form.get("as_of"),
        decision_horizon: form.get("decision_horizon"),
        benchmark_code: form.get("benchmark_code"),
      }),
    });
    await loadDecisionCases(item.case_id);
    toast(item.reused ? "相同数据合同的案例已存在" : `规则结果：${decisionStatusLabels[item.rule_status] || item.rule_status}`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "建立案例"; }
}

async function submitDecisionReview(caseId, decision) {
  const note = $("#decision-review-note")?.value.trim();
  const reviewer = $("#decision-reviewer")?.value.trim() || "human";
  if (!note || note.length < 2) { toast("请填写审批说明"); return; }
  try {
    await api(`/api/decision-cases/${encodeURIComponent(caseId)}/reviews`, {
      method: "POST",
      body: JSON.stringify({decision, reviewer, note}),
    });
    await loadDecisionCases(caseId);
    toast(reviewStatusLabels[decision] || decision);
  } catch (error) { toast(error.message); }
}

function shortHash(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 10)}…${text.slice(-6)}` : "—";
}

function renderAcceptance(result) {
  state.acceptance = result;
  const passed = result.status === "passed";
  const status = $("#acceptance-status");
  status.className = `acceptance-status ${passed ? "passed" : "failed"}`;
  status.innerHTML = `
    <div><span>ACCEPTANCE STATE</span><strong>${passed ? "全部门禁通过" : "存在未通过门禁"}</strong></div>
    <p>清单 ${escapeHtml(result.manifest_version)} · ${escapeHtml(shortHash(result.manifest_hash))}<br>数据指纹 ${escapeHtml(shortHash(result.data_fingerprint))}</p>`;
  const navStatus = $("#acceptance-nav-status");
  navStatus.textContent = passed ? "PASS" : "FAIL";
  navStatus.className = `acceptance-nav-status ${passed ? "passed" : "failed"}`;
  $("#acceptance-summary").innerHTML = `
    <div><span>归档 PDF</span><strong>${Number(result.summary.documents).toLocaleString()}</strong><small>${result.summary.parsed_documents} 可解析 · ${result.summary.ocr_required_documents} OCR 隔离</small></div>
    <div><span>金标文档</span><strong>${result.document_samples.filter((item) => item.passed).length} / ${result.summary.document_samples}</strong><small>状态、页数与原文</small></div>
    <div><span>金标事实</span><strong>${result.golden_facts.filter((item) => item.exact).length} / ${result.summary.golden_facts}</strong><small>指标、数值、比较期与页码</small></div>
    <div><span>失败门禁</span><strong class="${result.summary.failed_metrics ? "negative" : "positive"}">${result.summary.failed_metrics}</strong><small>12 项质量指标</small></div>`;

  const passedMetrics = result.metrics.filter((item) => item.passed).length;
  $("#acceptance-metric-count").textContent = `${passedMetrics} / ${result.metrics.length}`;
  $("#acceptance-metrics").innerHTML = result.metrics.map((item) => `
    <div class="acceptance-metric ${item.passed ? "passed" : "failed"}">
      <span class="gate-mark">${item.passed ? "PASS" : "FAIL"}</span>
      <b>${escapeHtml(item.label)}</b>
      <span>${item.numerator.toLocaleString()} / ${item.denominator.toLocaleString()}</span>
      <strong>${(Number(item.value) * 100).toFixed(2)}%</strong>
      <small>门槛 ${(Number(item.threshold) * 100).toFixed(0)}%</small>
    </div>`).join("");

  $("#acceptance-documents").innerHTML = result.document_samples.map((item) => `
    <div class="acceptance-check ${item.passed ? "passed" : "failed"}">
      <i>${item.passed ? "✓" : "!"}</i><div><b>${escapeHtml(item.label)}</b><small>${escapeHtml(item.security_code || "文档缺失")} · ${escapeHtml(item.actual_status)}</small></div>
      <span>${item.terms.filter((term) => term.found).length}/${item.terms.length} 原文</span>
    </div>`).join("");
  $("#acceptance-facts").innerHTML = result.golden_facts.map((item) => `
    <div class="acceptance-check ${item.exact ? "passed" : "failed"}">
      <i>${item.exact ? "✓" : "!"}</i><div><b>${escapeHtml(item.metric_code)}</b><small>${escapeHtml(shortHash(item.document_sha256))} · p.${escapeHtml(item.expected.source_page)}</small></div>
      <span>${item.exact ? escapeHtml(item.expected.current_value) : "不一致"}</span>
    </div>`).join("");
  $("#acceptance-gaps").innerHTML = result.known_gaps.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("");
}

function renderAcceptanceHistory(items) {
  $("#acceptance-history-count").textContent = items.length;
  const list = $("#acceptance-history-list");
  if (!items.length) {
    list.innerHTML = "<p>暂无验收记录</p>";
    return;
  }
  list.innerHTML = items.map((item) => `
    <button type="button" class="acceptance-history-item ${state.acceptance?.run_id === item.run_id ? "active" : ""}" data-acceptance-run="${escapeHtml(item.run_id)}">
      <span class="${item.status}">${item.status === "passed" ? "PASS" : "FAIL"}</span>
      <b>${escapeHtml(item.manifest_version)}</b>
      <small>${escapeHtml(new Date(item.finished_at).toLocaleString("zh-CN", { hour12: false }))}</small>
      <code>${escapeHtml(shortHash(item.snapshot_hash))}</code>
    </button>`).join("");
  $$('[data-acceptance-run]').forEach((button) => button.addEventListener("click", async () => {
    try {
      renderAcceptance(await api(`/api/evidence-acceptance-runs/${encodeURIComponent(button.dataset.acceptanceRun)}`));
      renderAcceptanceHistory(items);
    } catch (error) { toast(error.message); }
  }));
}

async function loadAcceptance() {
  try {
    const [latest, history] = await Promise.all([
      api("/api/evidence-acceptance-runs/latest"),
      api("/api/evidence-acceptance-runs?limit=20"),
    ]);
    if (latest.item) renderAcceptance(latest.item);
    renderAcceptanceHistory(history.items);
  } catch (error) { toast(error.message); }
}

async function runEvidenceAcceptance() {
  const button = $("#run-evidence-acceptance");
  button.disabled = true;
  button.innerHTML = "正在核验";
  try {
    const result = await api("/api/evidence-acceptance-runs", { method: "POST", body: "{}" });
    renderAcceptance(result);
    await loadAcceptance();
    toast(result.reused ? "数据未变化，已复用相同验收快照" : result.status === "passed" ? "M5 真实样本与证据验收通过" : `验收未通过：${result.failed_metrics.join("、")}`);
  } catch (error) { toast(error.message); }
  finally {
    button.disabled = false;
    button.innerHTML = `<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"></path></svg>运行验收`;
  }
}

function paperMoney(value) {
  return formatMoney(value);
}

function paperPct(value) {
  return formatPercent(value, {signed:true});
}

const dailyBatchStepLabels = {
  market_data:"日线补齐", factor_snapshot:"因子快照", current_shadow:"Current Shadow",
  candidate_research:"候选研究", holdings_review:"持仓复核", order_proposals:"订单提案",
};
const dailyBatchStatusLabels = {queued:"等待执行", running:"执行中", completed:"已完成", failed:"失败", pending:"等待"};

function renderPaperWorkflow(data = state.paper, batch = state.paperBatch) {
  if (!data) return;
  const relevantBatch = batch?.as_of === data.as_of ? batch : null;
  const latestRun = data.runs?.[0];
  const orders = latestRun?.orders || [];
  const proposed = orders.filter((order) => order.status === "proposed").length;
  const approved = orders.filter((order) => order.status === "approved").length;
  const filled = orders.filter((order) => order.status === "filled").length;
  const market = latestRun?.market_summary;
  const batchActive = ["queued", "running"].includes(relevantBatch?.status);
  const batchFailed = relevantBatch?.status === "failed";
  const hasResearch = Boolean(market);
  const steps = {
    batch: {
      status: batchFailed ? "failed" : batchActive ? "running" : latestRun || relevantBatch?.status === "completed" ? "completed" : "pending",
      detail: batchFailed ? "需要处理失败步骤" : batchActive ? "批次执行中" : latestRun ? "研究批次已生成" : "等待运行完整批次",
    },
    research: {
      status: hasResearch ? "completed" : batchActive ? "running" : "pending",
      detail: hasResearch ? `${Number(market.risk_passed_count || 0)} 只通过风险门禁` : batchActive ? "等待市场扫描" : "等待研究结果",
    },
    approval: {
      status: proposed ? "attention" : latestRun ? "completed" : "pending",
      detail: proposed ? `${proposed} 笔等待人工决定` : latestRun ? (orders.length ? "没有待审批提案" : "本日无需调仓") : "等待订单提案",
    },
    monitor: {
      status: data.positions.length || filled || approved ? "active" : latestRun && !proposed ? "completed" : "pending",
      detail: data.positions.length ? `${data.positions.length} 只持仓 · ${filled} 笔已成交` : approved ? `${approved} 笔等待撮合` : latestRun ? "当前没有持仓" : "等待组合状态",
    },
  };
  Object.entries(steps).forEach(([name, item]) => {
    const button = document.querySelector(`[data-paper-step="${name}"]`);
    if (!button) return;
    button.className = item.status;
    button.querySelector("small").textContent = item.detail;
    button.querySelector("i").textContent = item.status === "completed" ? "✓" : {batch:"1", research:"2", approval:"3", monitor:"4"}[name];
  });

  let title = "先运行今日完整研究批次";
  let nextLabel = "查看研究批次";
  let nextTarget = "paper-batch-status";
  if (batchActive) {
    title = "今日研究批次正在执行";
    nextLabel = "查看执行进度";
  } else if (batchFailed) {
    title = "今日研究批次存在失败步骤";
    nextLabel = "查看失败步骤";
  } else if (proposed) {
    title = `需要人工审核 ${proposed} 笔交易提案`;
    nextLabel = "前往人工审批";
    nextTarget = "paper-order-panel";
  } else if (approved) {
    title = `${approved} 笔提案已批准，等待模拟撮合`;
    nextLabel = "查看交易提案";
    nextTarget = "paper-order-panel";
  } else if (data.positions.length) {
    title = `今日研究已完成，监控 ${data.positions.length} 只持仓`;
    nextLabel = "查看当前持仓";
    nextTarget = "paper-position-panel";
  } else if (latestRun) {
    title = "今日研究已完成，当前无需调仓";
    nextLabel = "查看市场复核";
    nextTarget = "paper-market-panel";
  }
  const workflow = $("#paper-workflow");
  workflow.className = `paper-workflow ${batchFailed ? "failed" : proposed ? "attention" : batchActive ? "running" : latestRun ? "ready" : "empty"}`;
  $("#paper-workflow-title").textContent = title;
  $("#paper-workflow-meta").textContent = `${data.as_of} · 研究、人工审批与模拟成交相互独立`;
  const next = $("#paper-workflow-next");
  next.textContent = nextLabel;
  next.dataset.paperJump = nextTarget;
}

function dailyBatchStepDetail(step) {
  const result = step.result || {};
  if (step.error) return step.error;
  if (step.step_name === "market_data" && step.status === "completed") {
    const benchmark = result.benchmark_refresh;
    return `写入 ${Number(result.rows_inserted || 0).toLocaleString()} 行${benchmark?.status && benchmark.status !== "failed" ? ` · ${Number(benchmark.benchmark_count || 0)} 个指数` : benchmark?.status === "failed" ? " · 指数待重试" : ""}`;
  }
  if (step.step_name === "factor_snapshot" && step.status === "completed") return `通过 ${Number(result.passed_securities || 0).toLocaleString()} / ${Number(result.total_securities || 0).toLocaleString()}`;
  if (step.step_name === "current_shadow" && step.status === "completed") return `${Number(result.signal_count || 0)} 个信号 · ${paperPct(result.coverage)}`;
  if (step.step_name === "candidate_research" && step.status === "completed") return `完成 ${Number(result.completed || 0)} 家`;
  if (step.step_name === "holdings_review" && step.status === "completed") return `复核 ${Number(result.position_count || 0)} 只持仓`;
  if (step.step_name === "order_proposals" && step.status === "completed") return `${Number(result.order_count || 0)} 笔待人工审批`;
  return dailyBatchStatusLabels[step.status] || step.status;
}

function renderDailyBatch(batch) {
  state.paperBatch = batch;
  const container = $("#paper-batch-status");
  const header = container.querySelector("header");
  const steps = $("#paper-batch-steps");
  const button = $("#paper-batch-run");
  container.className = `paper-batch-status ${escapeHtml(batch?.status || "empty")}`;
  if (!batch) {
    header.innerHTML = `<strong>DAILY BATCH</strong><span>尚未运行完整日批次</span><small>人工审批不在自动步骤内</small>`;
    steps.innerHTML = "";
    button.disabled = false;
    button.textContent = "运行完整批次";
    renderPaperWorkflow(state.paper, null);
    return;
  }
  const done = batch.steps.filter((step) => step.status === "completed").length;
  const status = dailyBatchStatusLabels[batch.status] || batch.status;
  header.innerHTML = `<strong>DAILY BATCH</strong><span>${escapeHtml(batch.as_of)} · ${escapeHtml(status)} · ${done}/${batch.steps.length}</span><small>${batch.error ? escapeHtml(batch.error) : `批次 ${escapeHtml(batch.batch_id.slice(0, 8))} · 人工审批独立保留`}</small>`;
  steps.innerHTML = batch.steps.map((step, index) => `<div class="paper-batch-step ${escapeHtml(step.status)}"><i>${step.status === "completed" ? "✓" : index + 1}</i><div><b>${escapeHtml(dailyBatchStepLabels[step.step_name] || step.step_name)}</b><small>${escapeHtml(dailyBatchStepDetail(step))}</small></div></div>`).join("");
  const active = ["queued", "running"].includes(batch.status);
  button.disabled = active;
  button.textContent = active ? `执行中 ${done}/${batch.steps.length}` : batch.status === "failed" ? "继续失败批次" : "运行完整批次";
  renderPaperWorkflow(state.paper, batch);
}

function stopDailyBatchPolling() {
  clearInterval(state.paperBatchTimer);
  state.paperBatchTimer = null;
}

async function refreshDailyBatch(batchId, {notify = false} = {}) {
  try {
    const batch = await api(`/api/paper/daily-batches/${encodeURIComponent(batchId)}`);
    renderDailyBatch(batch);
    if (["completed", "failed"].includes(batch.status)) {
      stopDailyBatchPolling();
      await loadPaper({loadBatch:false});
      if (notify) toast(batch.status === "completed" ? "每日研究批次已完成，订单等待人工审批" : `每日研究批次失败：${batch.error}`);
    }
  } catch (error) {
    stopDailyBatchPolling();
    if (notify) toast(error.message);
  }
}

function startDailyBatchPolling(batchId) {
  stopDailyBatchPolling();
  state.paperBatchTimer = setInterval(() => refreshDailyBatch(batchId, {notify:true}), 2000);
}

async function loadDailyBatch(asOf) {
  try {
    const result = await api(`/api/paper/daily-batches/latest?as_of=${encodeURIComponent(asOf)}`);
    renderDailyBatch(result.item);
    if (result.item && ["queued", "running"].includes(result.item.status)) startDailyBatchPolling(result.item.batch_id);
  } catch (_) { renderDailyBatch(null); }
}

async function runFullDailyBatch() {
  const button = $("#paper-batch-run");
  button.disabled = true;
  try {
    const batch = await api("/api/paper/daily-batches", {method:"POST", body:JSON.stringify({
      as_of:$("#paper-as-of").value, top_n:5, hold_rank_buffer:30,
      shadow_lookback_days:240, shadow_batch_size:20,
      research_lookback_days:730, financial_download_limit:2,
      announcement_download_limit:3, depth:"quick",
      target_gross_exposure:0.50, max_position_weight:0.12,
      max_industry_weight:0.20, max_pair_correlation:0.85,
    })});
    renderDailyBatch(batch);
    if (["queued", "running"].includes(batch.status)) startDailyBatchPolling(batch.batch_id);
    toast(batch.started ? "完整日批次已启动" : batch.status === "completed" ? "已复用完成的同日批次" : "该批次正在执行");
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

function formatGateValue(gate) {
  if (gate.observed === null || gate.observed === undefined || gate.observed === "") return "缺失";
  if (gate.name === "factor_quality") return gate.observed === "passed" ? "正常" : String(gate.observed);
  if (gate.name === "tradability") return gate.observed === "tradable" ? "可交易" : String(gate.observed);
  if (["volatility", "drawdown"].includes(gate.name)) return formatPercent(gate.observed);
  if (gate.name === "liquidity") return turnoverValue(gate.observed);
  if (typeof gate.observed === "boolean") return gate.observed ? "是" : "否";
  return String(gate.observed);
}

function formatGateExpected(gate) {
  if (gate.name === "factor_quality") return "正常";
  if (gate.name === "observations") return `至少 ${String(gate.expected || "").replace(">=", "").trim()} 个交易日`;
  if (gate.name === "liquidity") {
    const threshold = Number(String(gate.expected || "").replace(">=", "").trim());
    return Number.isFinite(threshold) ? `至少 ${turnoverValue(threshold)}` : String(gate.expected || "—");
  }
  if (gate.name === "volatility") {
    const ceiling = Number(String(gate.expected || "").split("<=").at(-1)?.trim());
    return Number.isFinite(ceiling) ? `大于 0 且不超过 ${formatPercent(ceiling)}` : String(gate.expected || "—");
  }
  if (gate.name === "drawdown") {
    const floor = Number(String(gate.expected || "").replace(">=", "").trim());
    return Number.isFinite(floor) ? `不低于 ${formatPercent(floor)}` : String(gate.expected || "—");
  }
  if (gate.name === "tradability") return "研究日 OHLC 和成交量有效";
  return String(gate.expected ?? "—");
}

function closeDecisionTrace() {
  const drawer = $("#decision-trace-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  $("#decision-trace-backdrop").hidden = true;
}

function openDecisionTrace(code, orderId = null) {
  const latestRun = state.paper?.runs?.[0];
  const market = latestRun?.market_summary || {};
  const evaluation = (market.candidate_evaluations || []).find((item) => item.security_code === code) || {};
  const selected = (market.top_candidates || []).find((item) => item.security_code === code) || {};
  const order = (latestRun?.orders || []).find((item) => item.order_id === orderId)
    || (latestRun?.orders || []).find((item) => item.security_code === code)
    || {};
  const item = {...evaluation, ...selected};
  const assessment = item.research_assessment || order.reason?.research_assessment || {};
  const signal = item.research_status || assessment.signal || "defer";
  const riskGates = (item.gates || []).filter((gate) => gate.name !== "research_assessment");
  const reasons = item.rejection_reasons || order.reason?.rejection_reasons || [];
  const invalidating = assessment.invalidating_conditions || [];
  const researchReasons = assessment.reasons || [];
  const name = item.security_name || order.security_name || code;
  const rank = item.cross_section_rank ?? order.reason?.shadow_rank ?? null;
  const riskStatus = item.risk_status || (riskGates.length ? (riskGates.every((gate) => gate.passed) ? "passed" : "rejected") : "neutral");
  const portfolioStatus = item.portfolio_status || (selected.security_code ? "selected" : item.decision_status) || "—";

  $("#decision-trace-title").textContent = `${name} · 决策依据`;
  $("#decision-trace-content").innerHTML = `
    <section class="trace-security">
      ${renderSecurityIdentity({security_name:name, security_code:code}, {rank})}
      <div>${renderStatusBadge(riskStatus)}${renderStatusBadge(signal)}</div>
    </section>
    <section class="trace-section">
      <header><span>RISK GATES</span><b>确定性风险门禁</b><small>${riskGates.filter((gate) => gate.passed).length} / ${riskGates.length} 通过</small></header>
      <div class="trace-gates">${riskGates.length ? riskGates.map((gate) => `
        <div class="trace-gate"><i class="${gate.passed ? "passed" : "rejected"}">${gate.passed ? "✓" : "×"}</i><b>${escapeHtml(gateLabels[gate.name] || gate.name)}</b><span>${escapeHtml(formatGateValue(gate))}</span><small>${escapeHtml(formatGateExpected(gate))}</small></div>`).join("") : `<p>该历史记录未保存逐项风险门禁。</p>`}</div>
    </section>
    <section class="trace-section">
      <header><span>RESEARCH ASSESSMENT</span><b>基本面研究信号</b><small>${renderStatusBadge(signal)}</small></header>
      <div class="trace-metrics">
        <div><span>证据置信度</span><b>${formatConfidence(assessment.evidence_confidence)}</b></div>
        <div><span>权重乘数</span><b>${assessment.weight_multiplier == null ? "—" : `${Number(assessment.weight_multiplier).toFixed(2)}x`}</b></div>
        <div><span>重大负面</span><b>${Number(assessment.material_negative_count || 0)}</b></div>
        <div><span>催化剂</span><b>${Number(assessment.catalyst_count || 0)}</b></div>
      </div>
      <div class="trace-copy"><b>判断理由</b>${researchReasons.length ? `<ul>${researchReasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : `<p>该记录未保存研究理由。</p>`}</div>
      <div class="trace-copy"><b>失效条件</b>${invalidating.length ? `<ul>${invalidating.map((condition) => `<li>${escapeHtml(condition)}</li>`).join("")}</ul>` : `<p>未提供可审计失效条件，研究信号按暂缓处理。</p>`}</div>
    </section>
    <section class="trace-section">
      <header><span>PORTFOLIO CONTROL</span><b>组合约束</b><small>${renderStatusBadge(portfolioStatus)}</small></header>
      <dl class="trace-contract">
        <dt>目标权重</dt><dd>${formatPercent(item.target_weight ?? order.target_weight)}</dd>
        <dt>研究前权重</dt><dd>${formatPercent(item.pre_research_weight)}</dd>
        <dt>一级行业</dt><dd>${escapeHtml(item.industry_l1 || order.reason?.industry_l1 || "行业待补充")}</dd>
        <dt>最高相关性</dt><dd>${formatPercent(item.maximum_selected_correlation ?? order.reason?.maximum_selected_correlation)}</dd>
        <dt>组合状态</dt><dd>${escapeHtml(riskStatusLabels[portfolioStatus] || portfolioStatus)}</dd>
      </dl>
      ${reasons.length ? `<div class="trace-copy rejected"><b>未通过原因</b><ul>${reasons.map((reason) => `<li>${escapeHtml(rejectionReasonLabels[reason] || reason)}</li>`).join("")}</ul></div>` : ""}
    </section>
    <div class="trace-actions"><button id="trace-open-company" type="button" class="btn primary">进入公司研究</button></div>
    <p class="trace-boundary">这里展示的是生成提案时保存的审计快照。研究信号只能约束确定性风险引擎，不能绕过风险门禁、直接设置仓位或自动批准订单。</p>`;
  const drawer = $("#decision-trace-drawer");
  $("#decision-trace-backdrop").hidden = false;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#close-decision-trace").focus();
  $("#trace-open-company").addEventListener("click", () => {
    closeDecisionTrace();
    $("#as-of-input").value = market.as_of || latestRun?.as_of || "";
    state.researchOrigin = null;
    activateWorkspace("company", "research");
    loadAnalysis(code);
  });
}

function bindDecisionTraceActions(root = document) {
  root.querySelectorAll("[data-decision-trace-code]").forEach((button) => {
    button.addEventListener("click", () => openDecisionTrace(button.dataset.decisionTraceCode, button.dataset.decisionTraceOrder || null));
  });
}

const paperBenchmarkColors = {
  portfolio:"#171c19", "000001.SH":"#c34f43", "399001.SZ":"#28746a",
  "399006.SZ":"#b47716", "000300.SH":"#6474a8",
};

function drawPaperBenchmarkChart() {
  const comparison = state.paperBenchmark;
  const canvas = $("#paper-benchmark-chart");
  if (!comparison?.portfolio || !canvas || $("#paper-benchmark-view").hidden) return;
  const dates = comparison.portfolio.points.map((item) => item.date);
  if (!dates.length) return;
  const series = [
    {code:"portfolio", name:"模拟组合", points:comparison.portfolio.points},
    ...comparison.benchmarks.filter((item) => item.points?.length),
  ];
  const values = series.flatMap((item) => item.points.map((point) => Number(point.cumulative_return))).filter(Number.isFinite);
  if (!values.length) return;
  const width = Math.max(280, canvas.parentElement.clientWidth);
  const height = 230;
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const pad = {left:54, right:18, top:18, bottom:30};
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  let minValue = Math.min(0, ...values);
  let maxValue = Math.max(0, ...values);
  const spread = Math.max(0.01, maxValue - minValue);
  minValue -= spread * 0.12;
  maxValue += spread * 0.12;
  const x = (date) => pad.left + (dates.indexOf(date) / Math.max(1, dates.length - 1)) * plotWidth;
  const y = (value) => pad.top + ((maxValue - value) / (maxValue - minValue)) * plotHeight;

  ctx.font = "9px Bahnschrift, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= 4; index += 1) {
    const value = maxValue - ((maxValue - minValue) * index / 4);
    const py = y(value);
    ctx.strokeStyle = value === 0 ? "#9ba39d" : "#e4e8e3";
    ctx.lineWidth = value === 0 ? 1.2 : 1;
    ctx.beginPath(); ctx.moveTo(pad.left, py); ctx.lineTo(width - pad.right, py); ctx.stroke();
    ctx.fillStyle = "#777f79";
    ctx.fillText(`${(value * 100).toFixed(1)}%`, pad.left - 7, py);
  }
  series.forEach((item) => {
    ctx.strokeStyle = paperBenchmarkColors[item.code] || "#555";
    ctx.lineWidth = item.code === "portfolio" ? 2.4 : 1.6;
    ctx.beginPath();
    item.points.forEach((point, index) => {
      const px = x(point.date);
      const py = y(Number(point.cumulative_return));
      if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();
    const latest = item.points.at(-1);
    ctx.fillStyle = paperBenchmarkColors[item.code] || "#555";
    ctx.beginPath(); ctx.arc(x(latest.date), y(Number(latest.cumulative_return)), item.code === "portfolio" ? 3 : 2.3, 0, Math.PI * 2); ctx.fill();
  });
  const labelIndexes = [...new Set([0, Math.floor((dates.length - 1) / 2), dates.length - 1])];
  ctx.fillStyle = "#777f79";
  ctx.textBaseline = "top";
  labelIndexes.forEach((index) => {
    ctx.textAlign = index === 0 ? "left" : index === dates.length - 1 ? "right" : "center";
    ctx.fillText(dates[index].slice(5), pad.left + (index / Math.max(1, dates.length - 1)) * plotWidth, height - 20);
  });
}

function renderPaperBenchmark(comparison) {
  state.paperBenchmark = comparison || null;
  const empty = $("#paper-benchmark-empty");
  const view = $("#paper-benchmark-view");
  if (!comparison?.portfolio) {
    $("#paper-nav-count").textContent = "0 日";
    empty.hidden = false;
    view.hidden = true;
    const message = comparison?.limitations?.[0] || "运行完整批次或在手动工具中更新指数基准。";
    empty.querySelector("span").textContent = message;
    return;
  }
  $("#paper-nav-count").textContent = `${comparison.portfolio.points.length} 日`;
  empty.hidden = true;
  view.hidden = false;
  const items = [
    {code:"portfolio", name:"模拟组合", latest_return:comparison.portfolio.latest_return, excess_return:null, status:"complete"},
    ...comparison.benchmarks,
  ];
  $("#paper-benchmark-summary").innerHTML = items.map((item) => `
    <article class="${escapeHtml(item.status || "partial")}">
      <span><i style="background:${paperBenchmarkColors[item.code] || "#555"}"></i>${escapeHtml(item.name)}</span>
      <b class="${item.latest_return == null ? "" : Number(item.latest_return) >= 0 ? "positive" : "negative"}">${formatPercent(item.latest_return, {signed:true})}</b>
      <small>${item.code === "portfolio" ? `起点 ${escapeHtml(comparison.inception_date)}` : item.excess_return == null ? "缺少共同起点" : `组合超额 ${formatPercent(item.excess_return, {signed:true})}`}</small>
    </article>`).join("");
  const incomplete = comparison.benchmarks.filter((item) => item.status !== "complete").map((item) => item.name);
  $("#paper-benchmark-note").textContent = `收益期起点 ${comparison.inception_date} · 截止 ${comparison.as_of} · 成交前净值对齐指数前收盘价${incomplete.length ? ` · 覆盖不足：${incomplete.join("、")}` : ""}`;
  requestAnimationFrame(drawPaperBenchmarkChart);
}

function renderPaper(data) {
  state.paper = data;
  const latestNav = data.nav_history.at(-1);
  const summary = $("#paper-summary").children;
  summary[0].querySelector("strong").textContent = paperMoney(data.nav);
  summary[1].querySelector("strong").textContent = paperPct(data.cumulative_return);
  summary[1].querySelector("strong").className = data.cumulative_return >= 0 ? "positive" : "negative";
  summary[2].querySelector("strong").textContent = paperMoney(data.account.cash);
  summary[3].querySelector("strong").textContent = paperPct(1 - data.cash_weight);
  summary[4].querySelector("strong").textContent = paperPct(latestNav?.drawdown ?? 0);
  const realtime = data.realtime || {};
  const realtimeStatus = $("#paper-realtime-status");
  realtimeStatus.querySelector("span").textContent = realtime.latest_quote_time
    ? `${realtime.quote_count} 个快照 · 最新 ${String(realtime.latest_quote_time).replace("T", " ")}`
    : "尚未获取实时快照";
  realtimeStatus.querySelector("small").textContent = realtime.high_frequency_used ? "数据合同异常" : "仅 THS_RQ · 未使用高频数据";
  $("#paper-as-of").value = data.as_of;
  $("#paper-position-count").textContent = `${data.positions.length} 只`;
  $("#paper-position-body").innerHTML = data.positions.length ? data.positions.map((item) => `
    <tr><td class="paper-security">${renderSecurityIdentity(item)}</td>
    <td>${Number(item.quantity).toLocaleString()} / ${Number(item.available_quantity).toLocaleString()}</td><td class="paper-price"><b>${paperMoney(item.close)}</b><small>${item.price_source === "iFinD THS_RQ" ? `THS_RQ · ${escapeHtml(item.quote_time || "—")}` : "日线收盘价"}</small></td>
    <td>${paperMoney(item.market_value)}</td><td>${paperPct(item.weight)}</td>
    <td class="${Number(item.unrealized_pnl) >= 0 ? "positive" : "negative"}">${paperMoney(item.unrealized_pnl)}</td>
    <td>${item.shadow_rank ? `#${item.shadow_rank}` : "—"}</td></tr>`).join("") : `<tr><td colspan="7">暂无持仓，审批首批提案后等待下一交易日开盘撮合</td></tr>`;

  const latestRun = data.runs[0];
  const orders = latestRun?.orders || [];
  $("#paper-order-count").textContent = `${orders.length} 笔`;
  $("#paper-run-state").textContent = latestRun ? `${latestRun.as_of} · ${latestRun.status}` : "等待运行";
  $("#paper-order-list").innerHTML = orders.length ? orders.map((order) => `
    <article class="paper-order ${order.side}">
      <span class="paper-order-side">${order.side === "buy" ? "买入" : "卖出"}</span>
      <div class="paper-order-main">${renderSecurityIdentity(order)}
      <small>${Number(order.quantity).toLocaleString()} 股 · 参考 ${paperMoney(order.reference_price)} · Shadow #${escapeHtml(order.reason.shadow_rank ?? "—")}</small>
      <small>目标 ${paperPct(order.target_weight)} · 波动 ${paperPct(order.reason.volatility_60d)} · ${escapeHtml(order.reason.industry_l1 || "行业待补充 / 相关性代理")}</small>
      <small>研究 ${renderStatusBadge(order.reason.research_assessment?.signal || "defer")} · 证据置信度 ${formatConfidence(order.reason.research_assessment?.evidence_confidence)}</small>
      <button type="button" class="decision-trace-link" data-decision-trace-code="${escapeHtml(order.security_code)}" data-decision-trace-order="${escapeHtml(order.order_id)}">查看决策依据</button></div>
      ${order.status === "proposed" ? `<div class="paper-order-actions"><button type="button" title="批准模拟订单" data-paper-order="${escapeHtml(order.order_id)}" data-paper-decision="approve">✓</button><button type="button" title="拒绝模拟订单" data-paper-order="${escapeHtml(order.order_id)}" data-paper-decision="reject">×</button></div>` : `<span class="paper-status ${escapeHtml(order.status)}">${escapeHtml(paperStatusLabels[order.status] || order.status)}</span>`}
    </article>`).join("") : `<p>${latestRun ? "本批次没有需要调仓的订单" : "运行今日研究后生成提案"}</p>`;
  $$('[data-paper-order]').forEach((button) => button.addEventListener("click", () => reviewPaperOrder(button.dataset.paperOrder, button.dataset.paperDecision)));

  const market = latestRun?.market_summary;
  const riskRejected = market ? (market.candidate_evaluations || []).filter((item) => item.risk_status === "rejected").length : 0;
  const industryControl = market?.industry_control;
  const researchControl = market?.research_control;
  const researchCounts = researchControl?.counts || {};
  $("#paper-market-summary").innerHTML = market ? `
    <p class="paper-market-meta">截止 ${escapeHtml(market.as_of)} · ${Number(market.signal_count).toLocaleString()} / ${Number(market.universe_size).toLocaleString()} 只有效信号 · 调仓前净值 ${paperMoney(market.nav_before_orders)}</p>
    <div class="paper-risk-strip">
      <span>风险门禁通过 <b>${escapeHtml(market.risk_passed_count ?? "—")}</b></span>
      <span>风险拒绝 <b>${riskRejected}</b></span>
      <span>最终入选 <b>${escapeHtml(market.selected_count ?? market.top_candidates.length)}</b></span>
      <span>研究准入 / 降权 <b>${Number(researchCounts.admit || 0)} / ${Number(researchCounts.reduce || 0)}</b></span>
      <span>行业控制 <b>${industryControl?.status === "enforced" ? "正式行业上限" : "相关性代理"}</b></span>
    </div>
    <div class="paper-candidates">${market.top_candidates.map((item) => `<article>${renderSecurityIdentity(item, {rank:item.cross_section_rank})}<span>目标 ${formatPercent(item.target_weight)} · 波动 ${formatPercent(item.volatility_60d)}</span><span>${renderStatusBadge(item.research_status || "defer")} 置信度 ${formatConfidence(item.research_assessment?.evidence_confidence)}</span><span>回撤 ${formatPercent(item.max_drawdown_250d)}</span><button type="button" class="decision-trace-link" data-decision-trace-code="${escapeHtml(item.security_code)}">查看决策依据</button></article>`).join("")}</div>
    ${researchControl ? `<p class="paper-risk-note">ResearchAssessment ${escapeHtml(researchControl.policy_version)} · 暂缓 ${Number(researchCounts.defer || 0)} · 否决 ${Number(researchCounts.veto || 0)} · 缺失或过期一律暂缓</p>` : ""}
    <p class="paper-risk-note">${escapeHtml(industryControl?.note || "风险预算策略按波动率倒数分配，并执行单票与集中度上限。")}</p>` : `<p>尚无当日研究批次</p>`;
  renderPaperBenchmark(data.benchmark_comparison);
  $("#paper-nav-history").innerHTML = data.nav_history.length ? data.nav_history.slice().reverse().map((item) => `
    <div class="paper-nav-row"><span>${escapeHtml(item.trading_date)}</span><span>${paperMoney(item.nav)}</span><span class="${item.daily_return >= 0 ? "positive" : "negative"}">${paperPct(item.daily_return)}</span><span>${paperPct(item.drawdown)}</span></div>`).join("") : `<p>尚无净值记录</p>`;
  renderPaperWorkflow(data, state.paperBatch);
  bindDecisionTraceActions($("#paper-view"));
}

function renderPaperResearchTargets(data) {
  const container = $("#paper-research-status");
  if (!data?.targets?.length) {
    container.innerHTML = `<strong>RESEARCH ASSESSMENT</strong><span>尚无量化门禁通过的研究目标</span>`;
    return;
  }
  container.innerHTML = `<strong>RESEARCH ASSESSMENT</strong>${data.targets.map((item) => {
    const assessment = item.current_assessment || {};
    const signal = item.current_research_status || "defer";
    return `<div class="paper-research-item ${escapeHtml(signal)}"><b>#${escapeHtml(item.shadow_rank)} ${escapeHtml(item.security_name)} · ${escapeHtml(researchSignalLabels[signal] || signal)}</b><small>置信度 ${formatConfidence(assessment.evidence_confidence)} · 负面 ${Number(assessment.material_negative_count || 0)} · 催化 ${Number(assessment.catalyst_count || 0)}</small><button type="button" class="decision-trace-link" data-decision-trace-code="${escapeHtml(item.security_code)}">决策依据</button></div>`;
  }).join("")}`;
  bindDecisionTraceActions(container);
}

async function loadPaper({loadBatch = true} = {}) {
  try {
    const dashboard = await api("/api/paper/dashboard");
    renderPaper(dashboard);
    if (loadBatch) await loadDailyBatch($("#paper-as-of").value || dashboard.as_of);
    try {
      renderPaperResearchTargets(await api(`/api/paper/research-targets?as_of=${encodeURIComponent(dashboard.as_of)}&limit=5&hold_rank_buffer=30`));
    } catch (_) { renderPaperResearchTargets(null); }
  }
  catch (error) { toast(error.message); }
}

async function runPaperDaily() {
  const button = $("#paper-run");
  button.disabled = true;
  try {
    const asOf = $("#paper-as-of").value;
    const run = await api("/api/paper/daily-runs", {method:"POST", body:JSON.stringify({
      as_of:asOf, top_n:5, hold_rank_buffer:30, target_gross_exposure:0.50,
      max_position_weight:0.12, max_industry_weight:0.20, max_pair_correlation:0.85,
    })});
    await loadPaper();
    toast(run.reused ? "已复用当日不可变研究批次" : `已生成 ${run.orders.length} 笔待审批提案`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

async function assessPaperCandidates() {
  const button = $("#paper-assess");
  button.disabled = true;
  button.textContent = "研究进行中";
  try {
    const result = await api("/api/paper/research-assessments", {method:"POST", body:JSON.stringify({
      as_of:$("#paper-as-of").value, limit:5, hold_rank_buffer:30,
      lookback_days:730, financial_download_limit:2, announcement_download_limit:3, depth:"quick",
    })});
    const counts = result.results.reduce((acc, item) => {
      const signal = item.assessment?.signal || "failed";
      acc[signal] = (acc[signal] || 0) + 1;
      return acc;
    }, {});
    await loadPaper();
    toast(`候选研究完成 ${result.completed}/${result.results.length} · 准入 ${counts.admit || 0} · 降权 ${counts.reduce || 0} · 暂缓 ${counts.defer || 0} · 否决 ${counts.veto || 0}`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "研究候选"; }
}

async function reviewPaperOrder(orderId, decision) {
  try {
    await api(`/api/paper/orders/${encodeURIComponent(orderId)}/${decision}`, {method:"POST", body:JSON.stringify({reviewer:"human", note:decision === "approve" ? "人工批准模拟盘订单" : "人工拒绝模拟盘订单"})});
    await loadPaper();
    toast(decision === "approve" ? "已批准，将按下一可用开盘价撮合" : "已拒绝模拟订单");
    if (decision === "approve") await syncPaperRealtime({settle:true, notify:false});
  } catch (error) { toast(error.message); }
}

async function settlePaper() {
  const button = $("#paper-settle");
  button.disabled = true;
  try {
    const result = await api("/api/paper/settle/realtime", {method:"POST", body:"{}"});
    await loadPaper();
    toast(result.filled.length ? `已按 THS_RQ 开盘价模拟成交 ${result.filled.length} 笔` : "实时快照已更新，暂无可成交订单");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

async function refreshPaperRealtime() {
  const button = $("#paper-refresh");
  button.disabled = true;
  try {
    const result = await api("/api/paper/realtime-quotes/refresh", {method:"POST", body:"{}"});
    await loadPaper();
    toast(`已保存 ${result.quotes.length} 个 THS_RQ 实时快照`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

async function refreshPaperBenchmarks() {
  const button = $("#paper-benchmark-refresh");
  button.disabled = true;
  button.textContent = "正在更新指数";
  try {
    const result = await api("/api/paper/benchmarks/refresh", {
      method:"POST",
      body:JSON.stringify({as_of:$("#paper-as-of").value || null}),
    });
    await loadPaper();
    toast(`已更新 ${result.benchmark_count} 个指数 · ${Number(result.rows_written).toLocaleString()} 行`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "更新指数基准"; }
}

async function syncPaperRealtime({settle = true, notify = false} = {}) {
  if (state.paperRealtimeBusy || !$("#paper-view").classList.contains("active")) return;
  state.paperRealtimeBusy = true;
  try {
    const endpoint = settle ? "/api/paper/settle/realtime" : "/api/paper/realtime-quotes/refresh";
    const result = await api(endpoint, {method:"POST", body:"{}"});
    await loadPaper();
    if (notify && result.filled?.length) toast(`已自动模拟成交 ${result.filled.length} 笔`);
  } catch (error) {
    if (notify) toast(error.message);
  } finally { state.paperRealtimeBusy = false; }
}

function startPaperRealtimePolling() {
  clearInterval(state.paperRealtimeTimer);
  syncPaperRealtime({settle:true, notify:true});
  state.paperRealtimeTimer = setInterval(() => syncPaperRealtime({settle:true, notify:false}), 60000);
}

function stopPaperRealtimePolling() {
  clearInterval(state.paperRealtimeTimer);
  state.paperRealtimeTimer = null;
}

function bindEvents() {
  let searchTimer;
  $("#security-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchSecurities(event.target.value), 180);
  });
  $("#security-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { $("#search-results").hidden = true; $("#as-of-input").value = ""; state.researchOrigin = null; activateWorkspace("company", "research"); loadAnalysis(event.target.value); }
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".search-wrap")) $("#search-results").hidden = true;
  });
  $$(".quick-code").forEach((button) => button.addEventListener("click", () => { $("#as-of-input").value = ""; state.researchOrigin = null; activateWorkspace("company", "research"); loadAnalysis(button.dataset.code); }));
  $("#as-of-input").addEventListener("change", () => loadAnalysis(state.code));
  $$(".segmented button").forEach((button) => button.addEventListener("click", () => {
    $$(".segmented button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active"); state.chartRange = Number(button.dataset.range); drawChart();
  }));
  $$(".research-tabs button").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab)));
  $("#run-research").addEventListener("click", runResearch);
  $("#sync-announcements").addEventListener("click", () => syncAnnouncements("all"));
  $("#sync-financial-reports").addEventListener("click", () => syncAnnouncements("financial_reports"));
  $("#generate-financial-template").addEventListener("click", generateFinancialTemplate);
  $("#assistant-form").addEventListener("submit", askDocumentAssistant);
  $$(".question-presets button").forEach((button) => button.addEventListener("click", () => {
    $("#assistant-question").value = button.dataset.question;
    if (button.dataset.scope) $("#assistant-scope").value = button.dataset.scope;
    activateTab("assistant");
    $("#assistant-question").focus();
  }));
  $("#create-thesis").addEventListener("click", () => $("#thesis-dialog").showModal());
  $("#close-dialog").addEventListener("click", () => $("#thesis-dialog").close());
  $("#cancel-dialog").addEventListener("click", () => $("#thesis-dialog").close());
  $("#close-decision-trace").addEventListener("click", closeDecisionTrace);
  $("#decision-trace-backdrop").addEventListener("click", closeDecisionTrace);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#decision-trace-drawer").classList.contains("open")) closeDecisionTrace();
  });
  $("#thesis-form").addEventListener("submit", saveThesis);
  $("#generate-factor-snapshot").addEventListener("click", generateFactorSnapshot);
  $("#run-evidence-acceptance").addEventListener("click", runEvidenceAcceptance);
  $("#paper-run").addEventListener("click", runPaperDaily);
  $("#paper-batch-run").addEventListener("click", runFullDailyBatch);
  $("#paper-assess").addEventListener("click", assessPaperCandidates);
  $("#paper-refresh").addEventListener("click", refreshPaperRealtime);
  $("#paper-settle").addEventListener("click", settlePaper);
  $("#paper-benchmark-refresh").addEventListener("click", refreshPaperBenchmarks);
  $("#paper-tools").addEventListener("click", (event) => {
    if (event.target.closest("button")) $("#paper-tools").open = false;
  });
  $$('[data-paper-jump]').forEach((button) => button.addEventListener("click", () => {
    document.getElementById(button.dataset.paperJump)?.scrollIntoView({behavior:"smooth", block:"start"});
  }));
  $("#factor-filters").addEventListener("submit", (event) => { event.preventDefault(); loadFactorRows(); });
  $("#signal-filters").addEventListener("submit", (event) => { event.preventDefault(); loadModelSignals(); });
  $("#decision-create-form").addEventListener("submit", createDecisionCase);
  $$('[data-signal-mode]').forEach((button) => button.addEventListener("click", () => {
    if (button.disabled) return;
    state.signalMode = button.dataset.signalMode;
    syncSignalMode();
    loadModelSignals();
  }));
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => activateWorkspace(button.dataset.workspace)));
  $$("[data-workspace-tab]").forEach((button) => button.addEventListener("click", () => {
    activateWorkspace(button.dataset.workspaceTab, button.dataset.view);
  }));
  const canvas = $("#price-chart");
  canvas.addEventListener("mousemove", handleChartMove);
  canvas.addEventListener("mouseleave", () => $("#chart-tooltip").hidden = true);
  new ResizeObserver(() => drawChart()).observe($(".chart-wrap"));
  new ResizeObserver(() => drawPaperBenchmarkChart()).observe($(".paper-benchmark-chart-wrap"));
}

async function bootstrap() {
  bindEvents();
  await Promise.all([loadHealth(), loadTheses(), loadCandidates(), loadDecisionCases()]);
  await loadAnalysis();
  activateWorkspace("daily", "paper");
}

bootstrap();
