const API_BASE = (() => {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("api");
  if (fromQuery) {
    localStorage.setItem("apiBase", fromQuery);
    return fromQuery;
  }
  const stored = localStorage.getItem("apiBase");
  if (stored) return stored;
  if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
    return "http://localhost:8000";
  }
  return `${window.location.protocol}//${window.location.hostname}:8000`;
})();

let currentSessionId = null;
let isLoading = false;

const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const sessionList = $("sessionList");
const queryInput = $("queryInput");
const sendBtn = $("sendBtn");
const roleSelect = $("roleSelect");
const llmSelect = $("llmSelect");
const newChatBtn = $("newChatBtn");
const servicesBtn = $("servicesBtn");
const servicesModal = $("servicesModal");
const closeServices = $("closeServices");
const addServiceForm = $("addServiceForm");
const serviceListEl = $("serviceList");
const statusDot = $("statusDot");
const statusText = $("statusText");

function setStatus(ok, text) {
  statusDot.classList.toggle("ok", ok);
  statusText.textContent = text;
}

async function api(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

async function checkHealth() {
  try {
    const data = await api("/");
    setStatus(true, `Online · Neo4j: ${data.neo4j_connected ? "on" : "off (in-memory)"}`);
  } catch (e) {
    setStatus(false, "Backend offline");
  }
}

async function loadSessions() {
  try {
    const sessions = await api("/sessions");
    sessionList.innerHTML = "";
    sessions.forEach((s) => {
      const li = document.createElement("li");
      if (s.id === currentSessionId) li.classList.add("active");
      li.innerHTML = `<span>${escapeHtml(s.title || "Untitled")}</span><span class="del" data-id="${s.id}">x</span>`;
      li.addEventListener("click", (e) => {
        if (e.target.classList.contains("del")) return;
        currentSessionId = s.id;
        loadMessages();
        renderSessions();
      });
      li.querySelector(".del").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete this chat?")) return;
        await api(`/sessions/${s.id}`, { method: "DELETE" });
        if (currentSessionId === s.id) {
          currentSessionId = null;
          messagesEl.innerHTML = emptyStateHtml();
        }
        loadSessions();
      });
      sessionList.appendChild(li);
    });
  } catch (e) {
    console.error(e);
  }
}

function renderSessions() {
  Array.from(sessionList.children).forEach((li) => {
    const id = li.querySelector(".del").dataset.id;
    li.classList.toggle("active", id === currentSessionId);
  });
}

function emptyStateHtml() {
  return `<div class="empty-state"><h2>Ask anything about your OData services</h2><p>Try: "Show top 5 customers from Germany" or "List all products in Beverages category"</p></div>`;
}

async function loadMessages() {
  if (!currentSessionId) {
    messagesEl.innerHTML = emptyStateHtml();
    return;
  }
  try {
    const msgs = await api(`/sessions/${currentSessionId}/messages`);
    renderMessages(msgs);
  } catch (e) {
    messagesEl.innerHTML = `<div class="empty-state">Failed to load messages: ${e.message}</div>`;
  }
}

function renderMessages(msgs) {
  messagesEl.innerHTML = "";
  if (!msgs.length) {
    messagesEl.innerHTML = emptyStateHtml();
    return;
  }
  msgs.forEach((m) => {
    if (m.role === "user") {
      addUserBubble(m.content, false);
    } else if (m.role === "assistant") {
      addAssistantBubble(m.content, m.result, false);
    }
  });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addUserBubble(text, scroll = true) {
  const div = document.createElement("div");
  div.className = "bubble user";
  div.textContent = text;
  messagesEl.appendChild(div);
  if (scroll) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addAssistantBubble(summary, result, scroll = true) {
  const div = document.createElement("div");
  div.className = "bubble assistant";
  div.textContent = summary || "Done.";
  if (result && result.llm) {
    const badge = document.createElement("div");
    badge.className = `llm-badge llm-${result.llm.provider}`;
    const provider = result.llm.provider || "unknown";
    const latency = result.llm.latency_ms != null ? `${result.llm.latency_ms}ms` : "?";
    const tokens = result.llm.tokens ? ` · ${result.llm.tokens} tokens` : "";
    const corrected = result.llm.corrected ? " · self-corrected" : "";
    badge.textContent = `LLM: ${provider} · ${latency}${tokens}${corrected}`;
    div.appendChild(badge);
  }
  if (result && result.table) {
    const tableWrap = renderTable(result.table);
    if (tableWrap) {
      const panel = buildResultPanel(result.table);
      div.appendChild(panel.panelEl);
      tableWrap.style.display = "";
      panel.tableView.appendChild(tableWrap);
      requestAnimationFrame(() => panel.renderGraph());
    }
  }
  if (result && result.tool_calls && result.tool_calls.length) {
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = result.tool_calls.map((t) => {
      if (t.type === "odata.query") {
        const correctedTag = t.corrected ? ` <span class="tool-pill corrected">corrected</span>` : "";
        return `<span class="tool-pill">${escapeHtml(t.service_id)}/${escapeHtml(t.entity_set)}</span> ${t.row_count} rows${correctedTag}<div class="url-line">${escapeHtml(t.url || "")}</div>`;
      }
      return `<span class="tool-pill">error</span> ${escapeHtml(t.error || "")}`;
    }).join("");
    div.appendChild(meta);
  }
  messagesEl.appendChild(div);
  if (scroll) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function tableToCsv(table) {
  const escape = (v) => {
    if (v == null) return "";
    let s = typeof v === "object" ? JSON.stringify(v) : String(v);
    if (/[",\n\r]/.test(s)) s = `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const head = table.columns.map(escape).join(",");
  const body = table.rows.map((r) => table.columns.map((c) => escape(r[c])).join(",")).join("\r\n");
  return head + "\r\n" + body;
}

function downloadCsv(table, label) {
  const csv = tableToCsv(table);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const safe = (label || "result").replace(/[^\w-]+/g, "_").slice(0, 40);
  a.download = `${safe}_${new Date().toISOString().slice(0,19).replace(/[:T]/g, "-")}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderTable(table) {
  if (!table || !table.columns || !table.rows || !table.rows.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "table-wrapper";
  const toolbar = document.createElement("div");
  toolbar.className = "table-toolbar";
  const csvBtn = document.createElement("button");
  csvBtn.className = "csv-btn";
  csvBtn.textContent = "Download CSV";
  csvBtn.title = `Export ${table.rows.length} rows to CSV`;
  csvBtn.addEventListener("click", () => downloadCsv(table, "odata_result"));
  toolbar.appendChild(csvBtn);
  wrap.appendChild(toolbar);
  const tableEl = document.createElement("table");
  const thead = document.createElement("thead");
  const trh = document.createElement("tr");
  table.columns.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    trh.appendChild(th);
  });
  thead.appendChild(trh);
  tableEl.appendChild(thead);
  const tbody = document.createElement("tbody");
  table.rows.forEach((row) => {
    const tr = document.createElement("tr");
    table.columns.forEach((c) => {
      const td = document.createElement("td");
      const v = row[c];
      td.textContent = typeof v === "object" && v !== null ? JSON.stringify(v) : (v ?? "");
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tableEl.appendChild(tbody);
  wrap.appendChild(tableEl);
  if (table.truncated || table.row_count > table.rows.length) {
    const note = document.createElement("div");
    note.className = "url-line";
    note.textContent = `Showing ${table.rows.length} of ${table.row_count} rows${table.total_count ? ` (total: ${table.total_count})` : ""}.`;
    wrap.appendChild(note);
  }
  return wrap;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function classifyColumns(table) {
  const cols = (table.columns || []).filter((c) => !c.startsWith("@odata.") && !c.startsWith("odata.") && c !== "odata.etag");
  const rows = table.rows || [];
  const info = {};
  for (const c of cols) {
    const values = rows.map((r) => r[c]).filter((v) => v != null && v !== "");
    const types = new Set(values.map((v) => typeof v));
    const numericVals = values.filter((v) => typeof v === "number" || (typeof v === "string" && v.trim() !== "" && !isNaN(parseFloat(v)) && isFinite(v)));
    const isNumeric = values.length > 0 && numericVals.length === values.length;
    const isNumericPure = values.length > 0 && [...types].every((t) => t === "number");
    const unique = new Set(values.map((v) => String(v)));
    const uniqueCount = unique.size;
    info[c] = {
      type: isNumericPure ? "numeric" : [...types].every((t) => t === "string") ? "string" : "mixed",
      uniqueCount,
      isNumeric,
      isLikelyID: /(^|[^a-z])(ID|Id|Code|Key)$/i.test(c) && uniqueCount >= Math.min(values.length, 2),
      isLikelyName: /(Name|Title|Company|Contact|Category|Status|Region|Country|City|ShipName|ProductName|CategoryName|Suppliers|Category|Customer|Employee|Manager|Owner)$/i.test(c),
      isDate: /(Date|Time|edAt|edOn)$/i.test(c),
      isLowCardinality: uniqueCount > 1 && uniqueCount <= 8,
      isMediumCardinality: uniqueCount > 8 && uniqueCount <= 30,
    };
  }
  return info;
}

function detectEntityGroups(cols, colInfo) {
  const groups = new Map();
  for (const c of cols) {
    const m = c.match(/^(.+?)(ID|Name|Code|Key|Title)$/i);
    if (m) {
      const entity = m[1];
      if (!groups.has(entity)) groups.set(entity, []);
      groups.get(entity).push(c);
    } else if (colInfo[c] && colInfo[c].isLikelyID) {
      const entity = c.replace(/(ID|Id|Code|Key)$/i, "");
      if (entity && !groups.has(entity)) groups.set(entity, [c]);
    }
  }
  return Array.from(groups.entries())
    .filter(([, cs]) => cs.length >= 1)
    .map(([entity, cs]) => ({ entity, columns: cs }));
}

function aggregateByCategory(rows, categoryCol, metricCol, isCountMetric) {
  const groups = new Map();
  for (const r of rows) {
    const key = r[categoryCol] == null ? "N/A" : String(r[categoryCol]);
    let val = 0;
    if (isCountMetric) {
      val = 1;
    } else if (metricCol) {
      const n = parseFloat(r[metricCol]);
      val = isNaN(n) ? 0 : n;
    }
    if (!groups.has(key)) groups.set(key, 0);
    groups.set(key, groups.get(key) + val);
  }
  return Array.from(groups.entries())
    .map(([label, value]) => ({ label: label.length > 30 ? label.slice(0, 30) + "..." : label, value }))
    .sort((a, b) => b.value - a.value);
}

function buildNetworkData(rows, entityGroups, cols, colInfo) {
  const nodes = [];
  const nodeSet = new Set();
  const edges = [];
  for (const r of rows) {
    const rowNodeIds = [];
    for (const { entity, columns } of entityGroups) {
      const nameCol = columns.find((c) => /(Name|Title|Code)/i.test(c)) || columns[0];
      const idCol = columns.find((c) => /(ID|Code|Key)/i.test(c)) || nameCol;
      const idVal = r[idCol];
      if (idVal == null || idVal === "") continue;
      const nodeId = `${entity}-${idVal}`;
      if (!nodeSet.has(nodeId)) {
        nodeSet.add(nodeId);
        const label = r[nameCol] != null ? String(r[nameCol]) : String(idVal);
        const displayLabel = label.length > 24 ? label.slice(0, 24) + "..." : label;
        nodes.push({ id: nodeId, label: displayLabel, group: entity });
      }
      rowNodeIds.push(nodeId);
    }
    for (let i = 0; i < rowNodeIds.length; i++) {
      for (let j = i + 1; j < rowNodeIds.length; j++) {
        edges.push({ from: rowNodeIds[i], to: rowNodeIds[j] });
      }
    }
  }
  const groupNames = entityGroups.map((g) => g.entity);
  return { nodes, edges, groups: groupNames };
}

function generateInsights(table, colInfo, pie, bar, network, entityGroups) {
  const insights = [];
  if (pie && pie.values.length > 0) {
    const total = pie.values.reduce((a, b) => a + b, 0);
    const topVal = pie.values[0];
    const topPct = total > 0 ? ((topVal / total) * 100).toFixed(0) : 0;
    insights.push(`"${pie.labels[0]}" leads with ${topVal} (${topPct}% of ${pie.metric})`);
    if (pie.values.length >= 3) {
      const top3 = pie.values.slice(0, 3).reduce((a, b) => a + b, 0);
      const top3Pct = total > 0 ? ((top3 / total) * 100).toFixed(0) : 0;
      insights.push(`Top 3 ${pie.category}s account for ${top3Pct}% of all ${pie.metric}`);
    }
  }
  if (network && network.nodes.length > 0 && network.edges.length > 0) {
    const degree = {};
    for (const n of network.nodes) degree[n.id] = 0;
    for (const e of network.edges) {
      degree[e.from] = (degree[e.from] || 0) + 1;
      degree[e.to] = (degree[e.to] || 0) + 1;
    }
    const ranked = network.nodes
      .map((n) => ({ ...n, degree: degree[n.id] || 0 }))
      .sort((a, b) => b.degree - a.degree);
    if (ranked[0] && ranked[0].degree > 0) {
      insights.push(`Hub: "${ranked[0].label}" connects to ${ranked[0].degree} other node${ranked[0].degree === 1 ? "" : "s"}`);
    }
    if (network.groups.length >= 2) {
      insights.push(`${network.groups.length} entity types linked: ${network.groups.join(" → ")}`);
    }
  }
  if (!insights.length && table.rows.length > 0) {
    insights.push(`${table.rows.length} row${table.rows.length === 1 ? "" : "s"} across ${table.columns.length} columns`);
  }
  return insights;
}

function analyzeTable(table) {
  const allCols = table.columns || [];
  const rows = table.rows || [];
  if (!allCols.length || !rows.length) {
    return { suggested: "table", reason: "No data to visualize", pie: null, bar: null, network: null, insights: [], colInfo: {}, entityGroups: [] };
  }
  const colInfo = classifyColumns(table);
  const cols = Object.keys(colInfo);
  const entityGroups = detectEntityGroups(cols, colInfo);
  const numericCols = cols.filter((c) => colInfo[c].isNumeric);
  const stringCols = cols.filter((c) => colInfo[c].type === "string" || colInfo[c].type === "mixed");
  const lowCardCols = stringCols.filter((c) => colInfo[c].isLowCardinality);
  const mediumCardCols = stringCols.filter((c) => colInfo[c].isMediumCardinality);
  const idCols = cols.filter((c) => colInfo[c].isLikelyID);
  const isCountMetric = (c) => /^count$|Count$|Quantity$/i.test(c);
  let pie = null, bar = null, network = null;
  let suggested = "table";
  let reason = "No suitable dimensions detected";
  if (lowCardCols.length > 0 && (numericCols.length > 0 || rows.length > 0)) {
    const cat = lowCardCols[0];
    const metric = numericCols[0] || null;
    const isCount = metric ? isCountMetric(metric) : true;
    const data = aggregateByCategory(rows, cat, metric, isCount);
    if (data.length >= 2 && data.length <= 8) {
      pie = { labels: data.map((d) => d.label), values: data.map((d) => d.value), category: cat, metric: metric || "count" };
    }
  }
  const catCol = lowCardCols[0] || mediumCardCols[0] || stringCols[0];
  if (catCol) {
    const metric = numericCols[0] || null;
    const isCount = metric ? isCountMetric(metric) : true;
    const data = aggregateByCategory(rows, catCol, metric, isCount);
    if (data.length > 0) {
      bar = { labels: data.map((d) => d.label), values: data.map((d) => d.value), category: catCol, metric: metric || "count" };
    }
  }
  if (entityGroups.length >= 2 && rows.length <= 200) {
    const net = buildNetworkData(rows, entityGroups, cols, colInfo);
    if (net.nodes.length >= 2 && net.edges.length > 0) {
      network = net;
    }
  }
  if (pie) {
    suggested = "pie";
    reason = `${pie.labels.length} ${pie.category} groups with ${pie.metric}`;
  } else if (bar) {
    suggested = "bar";
    reason = `${bar.labels.length} ${bar.category} values`;
  }
  if (network && (suggested === "table" || (network.nodes.length <= 30 && network.edges.length >= network.nodes.length / 2))) {
    suggested = "network";
    reason = `${entityGroups.length} related entities: ${entityGroups.map((g) => g.entity).join(", ")}`;
  }
  const insights = generateInsights(table, colInfo, pie, bar, network, entityGroups);
  return { suggested, reason, pie, bar, network, insights, colInfo, entityGroups };
}

const CHART_PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#06b6d4", "#84cc16", "#a855f7", "#facc15"];

function renderPieChart(canvas, data, isDark) {
  const textColor = isDark ? "#e2e8f0" : "#0f172a";
  const borderColor = isDark ? "#1e293b" : "#ffffff";
  return new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: data.labels,
      datasets: [{
        data: data.values,
        backgroundColor: data.labels.map((_, i) => CHART_PALETTE[i % CHART_PALETTE.length]),
        borderColor,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "55%",
      plugins: {
        legend: { position: "right", labels: { color: textColor, font: { size: 12 }, boxWidth: 12, padding: 8 } },
        title: { display: true, text: `${data.metric} by ${data.category}`, color: textColor, font: { size: 13, weight: "600" } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = total > 0 ? ((ctx.parsed / total) * 100).toFixed(1) : 0;
              return `${ctx.label}: ${ctx.parsed} (${pct}%)`;
            },
          },
        },
      },
    },
  });
}

function renderBarChart(canvas, data, isDark) {
  const textColor = isDark ? "#e2e8f0" : "#0f172a";
  const gridColor = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
  const horizontal = data.labels.length > 6;
  const colors = data.labels.map((_, i) => CHART_PALETTE[i % CHART_PALETTE.length]);
  return new Chart(canvas, {
    type: "bar",
    data: {
      labels: data.labels,
      datasets: [{
        label: data.metric,
        data: data.values,
        backgroundColor: colors,
        borderColor: colors.map((c) => c),
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: horizontal ? "y" : "x",
      plugins: {
        legend: { display: false },
        title: { display: true, text: `${data.metric} by ${data.category}`, color: textColor, font: { size: 13, weight: "600" } },
        tooltip: {},
      },
      scales: {
        x: { ticks: { color: textColor }, grid: { color: gridColor } },
        y: { ticks: { color: textColor }, grid: { color: gridColor }, beginAtZero: true },
      },
    },
  });
}

function renderNetworkGraph(container, data, isDark) {
  container.innerHTML = "";
  const bgColor = isDark ? "#1e293b" : "#ffffff";
  container.style.background = bgColor;
  container.style.height = "400px";
  const groupColors = {};
  data.groups.forEach((g, i) => {
    groupColors[g] = CHART_PALETTE[i % CHART_PALETTE.length];
  });
  const nodesArr = data.nodes.map((n) => ({
    id: n.id,
    label: n.label,
    color: {
      background: groupColors[n.group] || "#94a3b8",
      border: isDark ? "#0b1220" : "#ffffff",
      highlight: { background: groupColors[n.group], border: "#fbbf24" },
    },
    font: { color: isDark ? "#e2e8f0" : "#0f172a", size: 12, face: "Inter, system-ui, sans-serif" },
    title: `${n.group}: ${n.label}`,
    shape: "dot",
    size: 18,
    borderWidth: 2,
  }));
  const edgesArr = data.edges.map((e, i) => ({
    id: `e${i}`,
    from: e.from,
    to: e.to,
    color: { color: isDark ? "rgba(255,255,255,0.25)" : "rgba(0,0,0,0.18)" },
    smooth: { type: "continuous" },
  }));
  const nodes = new vis.DataSet(nodesArr);
  const edges = new vis.DataSet(edgesArr);
  return new vis.Network(container, { nodes, edges }, {
    physics: {
      enabled: true,
      stabilization: { iterations: 200, fit: true },
      barnesHut: { gravitationalConstant: -3500, centralGravity: 0.2, springLength: 120, springConstant: 0.04, damping: 0.5 },
    },
    interaction: { hover: true, tooltipDelay: 100, navigationButtons: false, keyboard: false },
    nodes: { shape: "dot" },
    edges: { width: 1, smooth: { type: "continuous" } },
  });
}

const resultPanels = new Set();

function renderAnalyzeResults(result) {
  if (result.error) return `<div class="analyze-error">${escapeHtml(result.error)}</div>`;
  let html = `<div class="analyze-header"><span class="analyze-badge">${result.row_count} rows</span> <span class="analyze-badge">${result.numeric_columns.length} numeric</span> <span class="analyze-badge">${Object.keys(result.algorithms).length} algorithms</span></div>`;
  html += `<div class="analyze-summary">${escapeHtml(result.summary || "")}</div>`;
  const algos = result.algorithms;
  if (algos.summary_statistics && algos.summary_statistics.columns) {
    html += `<div class="analyze-card"><div class="analyze-card-title">Summary Statistics</div><div class="analyze-card-desc">${escapeHtml(algos.summary_statistics.description)}</div>`;
    html += `<div class="analyze-table-wrap"><table class="analyze-table"><thead><tr><th>Column</th><th>Count</th><th>Mean</th><th>Median</th><th>Std</th><th>Min</th><th>Max</th><th>Q1</th><th>Q3</th><th>Skew</th></tr></thead><tbody>`;
    for (const [col, s] of Object.entries(algos.summary_statistics.columns)) {
      html += `<tr><td><strong>${escapeHtml(col)}</strong></td><td>${s.count}</td><td>${s.mean}</td><td>${s.median}</td><td>${s.std}</td><td>${s.min}</td><td>${s.max}</td><td>${s.q1}</td><td>${s.q3}</td><td>${s.skewness}</td></tr>`;
    }
    html += `</tbody></table></div></div>`;
  }
  if (algos.anomaly_detection) {
    const ad = algos.anomaly_detection;
    html += `<div class="analyze-card"><div class="analyze-card-title">Anomaly Detection</div><div class="analyze-card-desc">${escapeHtml(ad.description)} — Method: ${escapeHtml(ad.method)} (threshold: ${ad.threshold})</div>`;
    html += `<div class="analyze-stat">Found <strong>${ad.anomaly_count}</strong> anomalous row${ad.anomaly_count === 1 ? "" : "s"}</div>`;
    if (ad.anomalies && ad.anomalies.length > 0) {
      html += `<div class="analyze-table-wrap"><table class="analyze-table"><thead><tr><th>Row #</th><th>Column</th><th>Value</th><th>Z-Score</th></tr></thead><tbody>`;
      for (const a of ad.anomalies) {
        const entries = Object.entries(a.deviations);
        for (let i = 0; i < entries.length; i++) {
          const [col, dev] = entries[i];
          const rowSpan = i === 0 ? ` rowspan="${entries.length}"` : "";
          if (i === 0) html += `<tr><td${rowSpan}><strong>${a.row_index}</strong></td>`;
          html += `<td>${escapeHtml(col)}</td><td>${dev.value}</td><td class="z-score">${dev.z_score}</td></tr>`;
        }
      }
      html += `</tbody></table></div>`;
    }
    html += `</div>`;
  }
  if (algos.correlation_analysis) {
    const ca = algos.correlation_analysis;
    html += `<div class="analyze-card"><div class="analyze-card-title">Correlation Analysis</div><div class="analyze-card-desc">${escapeHtml(ca.description)} — Method: ${escapeHtml(ca.method)}</div>`;
    if (ca.top_pairs && ca.top_pairs.length > 0) {
      html += `<div class="analyze-table-wrap"><table class="analyze-table"><thead><tr><th>Column A</th><th>Column B</th><th>Correlation</th><th>Strength</th></tr></thead><tbody>`;
      for (const p of ca.top_pairs) {
        const cls = p.correlation > 0.7 ? "corr-strong-pos" : p.correlation > 0.4 ? "corr-mod-pos" : p.correlation < -0.7 ? "corr-strong-neg" : p.correlation < -0.4 ? "corr-mod-neg" : "";
        html += `<tr><td>${escapeHtml(p.column_a)}</td><td>${escapeHtml(p.column_b)}</td><td class="${cls}">${p.correlation}</td><td>${escapeHtml(p.strength)}</td></tr>`;
      }
      html += `</tbody></table></div>`;
    }
    html += `</div>`;
  }
  if (algos.clustering) {
    const cl = algos.clustering;
    html += `<div class="analyze-card"><div class="analyze-card-title">K-Means Clustering</div><div class="analyze-card-desc">${escapeHtml(cl.description)} — Method: ${escapeHtml(cl.method)}</div>`;
    html += `<div class="analyze-stat">K = ${cl.k} clusters</div>`;
    for (const [name, c] of Object.entries(cl.clusters)) {
      html += `<div class="cluster-block"><div class="cluster-title">${escapeHtml(name)} <span class="cluster-size">(${c.size} rows)</span></div>`;
      html += `<div class="cluster-centroid">Centroid: ${Object.entries(c.centroid).map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(", ")}</div>`;
      if (c.sample_rows && c.sample_rows.length > 0) {
        html += `<div class="cluster-samples">Samples: ${c.sample_rows.map((r) => JSON.stringify(r)).join(" · ")}</div>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
  }
  if (algos.feature_importance) {
    const fi = algos.feature_importance;
    html += `<div class="analyze-card"><div class="analyze-card-title">Feature Importance</div><div class="analyze-card-desc">${escapeHtml(fi.description)}</div>`;
    html += `<div class="analyze-table-wrap"><table class="analyze-table"><thead><tr><th>Column</th><th>Importance</th><th>Bar</th></tr></thead><tbody>`;
    const maxImp = Math.max(...Object.values(fi.importance));
    for (const [col, imp] of Object.entries(fi.importance)) {
      const pct = maxImp > 0 ? (imp / maxImp) * 100 : 0;
      html += `<tr><td>${escapeHtml(col)}</td><td>${imp}</td><td><div class="importance-bar"><div class="importance-fill" style="width:${pct}%"></div></div></td></tr>`;
    }
    html += `</tbody></table></div>`;
    html += `<div class="analyze-stat">Distribution: mean distance = ${fi.distribution.mean_distance}, std = ${fi.distribution.std_distance}</div>`;
    html += `</div>`;
  }
  return html;
}

function renderTrainResults(result) {
  if (result.error) return `<div class="analyze-error">${escapeHtml(result.error)}</div>`;
  let html = "";
  if (result.results) {
    html += `<div class="analyze-header"><span class="analyze-badge">Best: ${escapeHtml(ALGO_NAMES[result.best_algorithm] || result.best_algorithm)}</span> <span class="analyze-badge">Score: ${result.best_score}</span> <span class="analyze-badge">${result.algorithms_tested} tested</span></div>`;
    for (const [algo, res] of Object.entries(result.results)) {
      if (res.error) { html += `<div class="analyze-card"><div class="analyze-card-title">${escapeHtml(ALGO_NAMES[algo] || algo)}</div><div class="analyze-card-desc error">${escapeHtml(res.error)}</div></div>`; continue; }
      html += renderSingleTrainResult(algo, res, algo === result.best_algorithm);
    }
  } else {
    html += renderSingleTrainResult(result.algorithm_key, result, true);
  }
  return html;
}

const ALGO_NAMES = { decision_tree:"Decision Tree", random_forest:"Random Forest", xgboost:"XGBoost", catboost:"CatBoost", logistic_regression:"Logistic Regression", knn:"K-Nearest Neighbors", svm:"Support Vector Machine", gradient_boosting:"Gradient Boosting", ada_boost:"Ada Boost", extra_trees:"Extra Trees", naive_bayes:"Naive Bayes" };

function renderSingleTrainResult(algo, res, isBest) {
  let html = `<div class="analyze-card${isBest ? " best-algo" : ""}"><div class="analyze-card-title">${escapeHtml(ALGO_NAMES[algo] || algo)}${isBest ? ' <span class="best-badge">BEST</span>' : ""}</div>`;
  html += `<div class="analyze-card-desc">${res.task_type} · ${res.sample_count} samples · Target: ${escapeHtml(res.target_column)}</div>`;
  const m = res.metrics;
  if (res.task_type === "classification") {
    html += `<div class="metrics-row"><div class="metric"><span class="metric-val">${m.accuracy}</span><span class="metric-lbl">Accuracy</span></div><div class="metric"><span class="metric-val">${m.precision}</span><span class="metric-lbl">Precision</span></div><div class="metric"><span class="metric-val">${m.recall}</span><span class="metric-lbl">Recall</span></div><div class="metric"><span class="metric-val">${m.f1}</span><span class="metric-lbl">F1</span></div></div>`;
  } else {
    html += `<div class="metrics-row"><div class="metric"><span class="metric-val">${m.r2}</span><span class="metric-lbl">R²</span></div><div class="metric"><span class="metric-val">${m.mae}</span><span class="metric-lbl">MAE</span></div><div class="metric"><span class="metric-val">${m.rmse}</span><span class="metric-lbl">RMSE</span></div></div>`;
  }
  if (res.confusion_matrix) {
    html += `<div class="analyze-subtitle">Confusion Matrix</div>`;
    const labels = res.confusion_matrix_labels || [];
    html += `<div class="cm-wrap"><table class="cm-table"><thead><tr><th></th>`;
    for (const l of labels) html += `<th>${escapeHtml(String(l))}</th>`;
    html += `</tr></thead><tbody>`;
    for (let i = 0; i < res.confusion_matrix.length; i++) {
      html += `<tr><th>${escapeHtml(labels[i] || String(i))}</th>`;
      for (let j = 0; j < res.confusion_matrix[i].length; j++) {
        const v = res.confusion_matrix[i][j];
        const cls = i === j ? "cm-diag" : (v > 0 ? "cm-err" : "");
        html += `<td class="${cls}">${v}</td>`;
      }
      html += `</tr>`;
    }
    html += `</tbody></table></div>`;
  }
  if (res.feature_importance && res.feature_importance.length > 0) {
    html += `<div class="analyze-subtitle">Feature Importance</div><div class="analyze-table-wrap"><table class="analyze-table"><thead><tr><th>Column</th><th>Importance</th><th></th></tr></thead><tbody>`;
    const maxImp = res.feature_importance[0].importance || 1;
    for (const fi of res.feature_importance) {
      const pct = maxImp > 0 ? (fi.importance / maxImp) * 100 : 0;
      html += `<tr><td>${escapeHtml(fi.column)}</td><td>${fi.importance}</td><td><div class="importance-bar"><div class="importance-fill" style="width:${pct}%"></div></div></td></tr>`;
    }
    html += `</tbody></table></div>`;
  }
  html += `</div>`;
  return html;
}

function renderCleanResults(result) {
  let html = `<div class="analyze-header"><span class="analyze-badge">${result.report.original_rows} → ${result.report.final_rows} rows</span> <span class="analyze-badge">${result.report.original_columns} → ${result.report.final_columns} cols</span></div>`;
  if (result.report.steps.length === 0) {
    html += `<div class="analyze-card"><div class="analyze-card-title">No Changes</div><div class="analyze-card-desc">Data was already clean.</div></div>`;
  } else {
    html += `<div class="analyze-card"><div class="analyze-card-title">Cleaning Steps</div>`;
    html += `<div class="analyze-table-wrap"><table class="analyze-table"><thead><tr><th>Step</th><th>Details</th></tr></thead><tbody>`;
    for (const s of result.report.steps) {
      let detail = "";
      if (s.step === "remove_duplicates") detail = `Removed ${s.removed} duplicate rows`;
      else if (s.step === "drop_missing") detail = `Dropped ${s.dropped} rows with missing values`;
      else if (s.step.startsWith("fill_")) detail = `Filled ${s.filled} missing values in ${s.column} with ${s.value} (${s.step.replace("fill_", "")})`;
      else if (s.step === "remove_outliers") detail = `Removed ${s.removed} outliers from ${s.column} using ${s.method}`;
      else if (s.step === "normalize") detail = `Normalized ${s.column} using ${s.method}`;
      else if (s.step === "encode") detail = `Encoded ${s.column} → ${s.categories} binary columns`;
      else detail = JSON.stringify(s);
      html += `<tr><td><strong>${escapeHtml(s.step)}</strong></td><td>${escapeHtml(detail)}</td></tr>`;
    }
    html += `</tbody></table></div></div>`;
  }
  return html;
}

const ALGO_OPTIONS = [
  { value: "decision_tree", label: "Decision Tree" },
  { value: "random_forest", label: "Random Forest" },
  { value: "xgboost", label: "XGBoost" },
  { value: "catboost", label: "CatBoost" },
  { value: "logistic_regression", label: "Logistic Regression" },
  { value: "knn", label: "K-Nearest Neighbors" },
  { value: "svm", label: "Support Vector Machine" },
  { value: "gradient_boosting", label: "Gradient Boosting" },
  { value: "ada_boost", label: "Ada Boost" },
  { value: "extra_trees", label: "Extra Trees" },
  { value: "naive_bayes", label: "Naive Bayes" },
];

function renderAnalyzePanel(container, table) {
  const cols = (table.columns || []).filter(c => !c.startsWith("@odata."));
  const numCols = cols.filter(c => {
    const vals = (table.rows || []).slice(0, 20).map(r => r[c]);
    return vals.some(v => v !== null && v !== "" && !isNaN(Number(v)));
  });
  const catCols = cols.filter(c => !numCols.includes(c));

  container.innerHTML = `
    <div class="ml-controls">
      <div class="ml-row">
        <div class="ml-group">
          <label class="ml-label">Analysis Type</label>
          <select id="mlType" class="ml-select">
            <option value="auto">Auto (Unsupervised)</option>
            <option value="clean">Clean Data</option>
            <option value="train">Supervised: Train Model</option>
            <option value="compare">Supervised: Compare All</option>
          </select>
        </div>
        <div class="ml-group ml-predict-opts">
          <label class="ml-label">Algorithm</label>
          <select id="mlAlgo" class="ml-select">
            ${ALGO_OPTIONS.map(a => `<option value="${a.value}"${a.value === "random_forest" ? " selected" : ""}>${a.label}</option>`).join("")}
          </select>
        </div>
        <div class="ml-group ml-predict-opts">
          <label class="ml-label">Target Column</label>
          <select id="mlTarget" class="ml-select">
            ${cols.map(c => `<option value="${c}">${c}</option>`).join("")}
          </select>
        </div>
      </div>
      <div class="ml-row ml-clean-opts" style="display:none">
        <div class="ml-group">
          <label class="ml-label">Handle Missing</label>
          <select id="mlMissing" class="ml-select">
            <option value="drop">Drop Rows</option>
            <option value="mean">Fill Mean</option>
            <option value="median">Fill Median</option>
            <option value="mode">Fill Mode</option>
            <option value="zero">Fill Zero</option>
          </select>
        </div>
        <div class="ml-group">
          <label class="ml-label"><input type="checkbox" id="mlOutliers"> Remove Outliers</label>
        </div>
        <div class="ml-group">
          <label class="ml-label"><input type="checkbox" id="mlNormalize"> Normalize</label>
        </div>
        <div class="ml-group">
          <label class="ml-label"><input type="checkbox" id="mlEncode" checked> Encode Categorical</label>
        </div>
      </div>
      <div class="ml-row ml-compare-opts" style="display:none">
        <div class="ml-group">
          <label class="ml-label">Algorithms to Compare</label>
          <div class="ml-checkbox-group">
            ${ALGO_OPTIONS.map(a => `<label class="ml-check"><input type="checkbox" value="${a.value}" class="ml-algo-check"${["decision_tree","random_forest","logistic_regression","xgboost","gradient_boosting"].includes(a.value) ? " checked" : ""}> ${a.label}</label>`).join("")}
          </div>
        </div>
      </div>
      <button id="mlRunBtn" class="ml-run-btn">Run Analysis</button>
    </div>
    <div id="mlResults" class="ml-results"></div>
  `;

  const typeSelect = container.querySelector("#mlType");
  const predictOpts = container.querySelectorAll(".ml-predict-opts");
  const cleanOpts = container.querySelector(".ml-clean-opts");
  const compareOpts = container.querySelector(".ml-compare-opts");
  const runBtn = container.querySelector("#mlRunBtn");
  const resultsDiv = container.querySelector("#mlResults");

  typeSelect.addEventListener("change", () => {
    const v = typeSelect.value;
    predictOpts.forEach(el => el.style.display = (v === "train") ? "" : "none");
    cleanOpts.style.display = v === "clean" ? "" : "none";
    compareOpts.style.display = v === "compare" ? "" : "none";
  });

  runBtn.addEventListener("click", async () => {
    const type = typeSelect.value;
    runBtn.disabled = true;
    runBtn.textContent = "Running...";
    resultsDiv.innerHTML = `<div class="analyze-loading">Processing...</div>`;
    try {
      let result;
      if (type === "auto") {
        result = await api("/analyze", { method: "POST", body: { table } });
        resultsDiv.innerHTML = renderAnalyzeResults(result);
      } else if (type === "clean") {
        const opts = {
          handle_missing: container.querySelector("#mlMissing").value,
          remove_outliers: container.querySelector("#mlOutliers").checked,
          normalize: container.querySelector("#mlNormalize").checked,
          encode_categorical: container.querySelector("#mlEncode").checked,
        };
        result = await api("/ml/clean", { method: "POST", body: { table, options: opts } });
        resultsDiv.innerHTML = renderCleanResults(result);
      } else if (type === "train") {
        const algo = container.querySelector("#mlAlgo").value;
        const target = container.querySelector("#mlTarget").value;
        result = await api("/ml/train", { method: "POST", body: { table, target_column: target, algorithm: algo } });
        resultsDiv.innerHTML = renderTrainResults(result);
      } else if (type === "compare") {
        const target = container.querySelector("#mlTarget").value;
        const checked = Array.from(container.querySelectorAll(".ml-algo-check:checked")).map(cb => cb.value);
        if (checked.length === 0) throw new Error("Select at least one algorithm");
        result = await api("/ml/train", { method: "POST", body: { table, target_column: target, compare: true, algorithms: checked } });
        resultsDiv.innerHTML = renderTrainResults(result);
      }
    } catch (e) {
      resultsDiv.innerHTML = `<div class="analyze-error">${escapeHtml(e.message)}</div>`;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = "Run Analysis";
    }
  });

  // Auto-run the default (auto) analysis
  runBtn.click();
}

function buildResultPanel(table) {
  const analysis = analyzeTable(table);
  const panelEl = document.createElement("div");
  panelEl.className = "result-panel";
  const tabsEl = document.createElement("div");
  tabsEl.className = "result-tabs";
  const tableTab = document.createElement("button");
  tableTab.className = "result-tab active";
  tableTab.dataset.view = "table";
  tableTab.textContent = "Table";
  const graphTab = document.createElement("button");
  graphTab.className = "result-tab";
  graphTab.dataset.view = "graph";
  const hasGraph = !!(analysis.pie || analysis.bar || analysis.network);
  graphTab.textContent = hasGraph ? "Graph" : "Graph (n/a)";
  graphTab.disabled = !hasGraph;
  if (!hasGraph) graphTab.title = "No suitable dimensions for visualization";
  const analyzeTab = document.createElement("button");
  analyzeTab.className = "result-tab";
  analyzeTab.dataset.view = "analyze";
  analyzeTab.textContent = "Analyze";
  analyzeTab.title = "Run ML analysis on this data";
  tabsEl.appendChild(tableTab);
  tabsEl.appendChild(graphTab);
  tabsEl.appendChild(analyzeTab);
  panelEl.appendChild(tabsEl);
  const tableView = document.createElement("div");
  tableView.className = "result-view result-view-table";
  const graphView = document.createElement("div");
  graphView.className = "result-view result-view-graph hidden";
  const graphTypeTabs = document.createElement("div");
  graphTypeTabs.className = "graph-type-tabs";
  const types = [
    { id: "auto", label: "Auto", enabled: true, available: true },
    { id: "pie", label: "Pie", enabled: !!analysis.pie, available: !!analysis.pie },
    { id: "bar", label: "Bar", enabled: !!analysis.bar, available: !!analysis.bar },
    { id: "network", label: "Network", enabled: !!(analysis.network && analysis.network.nodes.length > 0), available: !!(analysis.network && analysis.network.nodes.length > 0) },
  ];
  let activeType = "auto";
  for (const t of types) {
    const btn = document.createElement("button");
    btn.className = "graph-type-tab" + (t.id === "auto" ? " active" : "");
    btn.dataset.type = t.id;
    btn.textContent = t.label;
    btn.disabled = !t.available;
    btn.title = t.available ? `Switch to ${t.label}` : `${t.label} not available for this data`;
    graphTypeTabs.appendChild(btn);
  }
  graphView.appendChild(graphTypeTabs);
  const graphInner = document.createElement("div");
  graphInner.className = "graph-inner";
  const canvas = document.createElement("canvas");
  canvas.className = "graph-canvas";
  const networkEl = document.createElement("div");
  networkEl.className = "graph-network";
  graphInner.appendChild(canvas);
  graphInner.appendChild(networkEl);
  graphView.appendChild(graphInner);
  if (analysis.insights && analysis.insights.length) {
    const insightsEl = document.createElement("div");
    insightsEl.className = "graph-insights";
    insightsEl.innerHTML = `<span class="insights-label">Insights:</span> ${analysis.insights.map(escapeHtml).join(" · ")}`;
    graphView.appendChild(insightsEl);
  }
  const reasonEl = document.createElement("div");
  reasonEl.className = "graph-reason";
  reasonEl.textContent = `Auto-detected: ${analysis.suggested} — ${analysis.reason}`;
  graphView.appendChild(reasonEl);
  panelEl.appendChild(tableView);
  panelEl.appendChild(graphView);

  const analyzeView = document.createElement("div");
  analyzeView.className = "result-view result-view-analyze hidden";
  analyzeView.innerHTML = `<div class="analyze-loading">Click "Analyze" to run ML algorithms on this data...</div>`;
  panelEl.appendChild(analyzeView);
  let chartInstance = null;
  let networkInstance = null;
  function destroyCharts() {
    if (chartInstance) {
      try { chartInstance.destroy(); } catch {}
      chartInstance = null;
    }
    if (networkInstance) {
      try { networkInstance.destroy(); } catch {}
      networkInstance = null;
    }
    networkEl.innerHTML = "";
    graphInner.querySelectorAll(".graph-empty").forEach((el) => el.remove());
  }
  function getActiveType() {
    if (activeType === "auto") return analysis.suggested;
    return activeType;
  }
  function renderGraph() {
    destroyCharts();
    canvas.style.display = "none";
    networkEl.style.display = "none";
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const type = getActiveType();
    if (type === "pie" && analysis.pie) {
      canvas.style.display = "block";
      try { chartInstance = renderPieChart(canvas, analysis.pie, isDark); } catch (e) { console.error("Pie chart failed", e); }
    } else if (type === "bar" && analysis.bar) {
      canvas.style.display = "block";
      try { chartInstance = renderBarChart(canvas, analysis.bar, isDark); } catch (e) { console.error("Bar chart failed", e); }
    } else if (type === "network" && analysis.network && analysis.network.nodes.length > 0) {
      networkEl.style.display = "block";
      try { networkInstance = renderNetworkGraph(networkEl, analysis.network, isDark); } catch (e) { console.error("Network graph failed", e); }
    } else {
      const empty = document.createElement("div");
      empty.className = "graph-empty";
      empty.textContent = "This data shape isn't ideal for visualization. Try the table view.";
      graphInner.appendChild(empty);
    }
  }
  const panelState = { panelEl, renderGraph, destroyCharts };
  resultPanels.add(panelState);
  tableTab.addEventListener("click", () => {
    tableTab.classList.add("active");
    graphTab.classList.remove("active");
    tableView.classList.remove("hidden");
    graphView.classList.add("hidden");
    destroyCharts();
  });
  graphTab.addEventListener("click", () => {
    if (graphTab.disabled) return;
    graphTab.classList.add("active");
    tableTab.classList.remove("active");
    graphView.classList.remove("hidden");
    tableView.classList.add("hidden");
    requestAnimationFrame(() => renderGraph());
  });
  graphTypeTabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".graph-type-tab");
    if (!btn || btn.disabled) return;
    graphTypeTabs.querySelectorAll(".graph-type-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeType = btn.dataset.type;
    requestAnimationFrame(() => renderGraph());
  });
  let analyzeLoading = false;
  analyzeTab.addEventListener("click", () => {
    analyzeTab.classList.add("active");
    tableTab.classList.remove("active");
    graphTab.classList.remove("active");
    analyzeView.classList.remove("hidden");
    tableView.classList.add("hidden");
    graphView.classList.add("hidden");
    if (!analyzeView.dataset.loaded) {
      renderAnalyzePanel(analyzeView, table);
      analyzeView.dataset.loaded = "1";
    }
  });
  return { panelEl, tableView, renderGraph };
}

function rerenderAllCharts() {
  for (const p of resultPanels) {
    const graphView = p.panelEl.querySelector(".result-view-graph");
    if (graphView && !graphView.classList.contains("hidden")) {
      try { p.renderGraph(); } catch (e) { console.error("Rerender failed", e); }
    }
  }
}

const themeToggle = $("themeToggle");
const THEME_KEY = "theme";
function getStoredTheme() {
  return localStorage.getItem(THEME_KEY);
}
function getSystemTheme() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(theme) {
  const t = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
  if (themeToggle) {
    themeToggle.setAttribute("aria-pressed", t === "dark" ? "true" : "false");
    themeToggle.title = t === "dark" ? "Switch to light mode" : "Switch to dark mode";
  }
}
function initTheme() {
  const urlTheme = new URLSearchParams(window.location.search).get("theme");
  if (urlTheme === "light" || urlTheme === "dark") {
    return;
  }
  const stored = getStoredTheme();
  applyTheme(stored || getSystemTheme());
}
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  const next = current === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}
if (themeToggle) {
  themeToggle.addEventListener("click", toggleTheme);
}
initTheme();
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    const urlTheme = new URLSearchParams(window.location.search).get("theme");
    if (!getStoredTheme() && !urlTheme) applyTheme(e.matches ? "dark" : "light");
  });
}
const themeObserver = new MutationObserver(() => {
  setTimeout(rerenderAllCharts, 50);
});
themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

async function send() {
  if (isLoading) return;
  const q = queryInput.value.trim();
  if (!q) return;
  isLoading = true;
  sendBtn.disabled = true;
  sendBtn.textContent = "Sending...";
  if (messagesEl.querySelector(".empty-state")) messagesEl.innerHTML = "";
  addUserBubble(q);
  queryInput.value = "";
  try {
    const resp = await api("/chat", {
      method: "POST",
      body: {
        query: q,
        session_id: currentSessionId,
        user_role: roleSelect.value,
      },
    });
    currentSessionId = resp.session_id;
    addAssistantBubble(resp.summary, {
      table: resp.table,
      tool_calls: resp.tool_calls,
      llm: {
        provider: resp.llm_provider,
        latency_ms: resp.llm_latency_ms,
        tokens: resp.llm_tokens,
        corrected: (resp.tool_calls || []).some((t) => t.corrected),
      },
    });
    loadSessions();
  } catch (e) {
    addAssistantBubble("Error: " + e.message, null);
  } finally {
    isLoading = false;
    sendBtn.disabled = false;
    sendBtn.textContent = "Send";
    queryInput.focus();
  }
}

sendBtn.addEventListener("click", send);
queryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
newChatBtn.addEventListener("click", () => {
  currentSessionId = null;
  messagesEl.innerHTML = emptyStateHtml();
  renderSessions();
  queryInput.focus();
});
roleSelect.addEventListener("change", () => localStorage.setItem("userRole", roleSelect.value));
const storedRole = localStorage.getItem("userRole");
if (storedRole) roleSelect.value = storedRole;

const LLM_STORAGE_KEY = "llmOptionId";
async function loadLlmConfig() {
  try {
    const cfg = await api("/llm/config");
    if (!llmSelect) return;
    const stored = localStorage.getItem(LLM_STORAGE_KEY);
    llmSelect.innerHTML = "";
    cfg.options.forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt.id;
      o.textContent = opt.label + (opt.available ? "" : "  (unavailable)");
      o.disabled = !opt.available;
      o.title = opt.available ? opt.label : (opt.reason || "Unavailable");
      llmSelect.appendChild(o);
    });
    const customOpt = document.createElement("option");
    customOpt.value = "custom";
    customOpt.textContent = `Custom: ${cfg.current.provider} / ${cfg.current.model}`;
    customOpt.title = "Currently active custom config";
    llmSelect.appendChild(customOpt);
    let chosen = stored && cfg.options.find((o) => o.id === stored && o.available) ? stored : cfg.current.id;
    if (chosen === "custom" || !cfg.options.find((o) => o.id === chosen)) {
      customOpt.selected = true;
    } else {
      llmSelect.value = chosen;
    }
  } catch (e) {
    console.error("Failed to load LLM config", e);
    if (llmSelect) {
      llmSelect.innerHTML = "<option value=''>LLM config unavailable</option>";
    }
  }
}
if (llmSelect) {
  llmSelect.addEventListener("change", async () => {
    const id = llmSelect.value;
    if (!id) return;
    try {
      const r = await api("/llm/config", { method: "POST", body: { id } });
      localStorage.setItem(LLM_STORAGE_KEY, id);
      setStatus(true, `LLM → ${r.provider}/${r.model}`);
      setTimeout(() => checkHealth(), 1500);
    } catch (e) {
      alert("Failed to switch LLM: " + e.message);
      loadLlmConfig();
    }
  });
  loadLlmConfig();
}

servicesBtn.addEventListener("click", () => {
  servicesModal.classList.remove("hidden");
  loadServices();
});
closeServices.addEventListener("click", () => servicesModal.classList.add("hidden"));
servicesModal.addEventListener("click", (e) => {
  if (e.target === servicesModal) servicesModal.classList.add("hidden");
});

async function loadServices() {
  try {
    const services = await api("/services");
    serviceListEl.innerHTML = "";
    if (!services.length) {
      const li = document.createElement("li");
      li.textContent = "No services registered yet.";
      serviceListEl.appendChild(li);
      return;
    }
    let health = { services: [] };
    try {
      health = await api("/services/health");
    } catch (e) {
      console.warn("Health probe failed", e);
    }
    const healthById = Object.fromEntries((health.services || []).map((h) => [h.id, h]));
    services.forEach((s) => {
      const h = healthById[s.id];
      const status = h ? h.status : "unknown";
      const latency = h && h.latency_ms != null ? `${h.latency_ms}ms` : "?";
      const dotClass = `health-dot health-${status}`;
      const li = document.createElement("li");
      li.innerHTML = `
        <div>
          <div class="svc-header">
            <span class="${dotClass}" title="${status}${h && h.http_status ? ` (HTTP ${h.http_status})` : ""}"></span>
            <strong>${escapeHtml(s.name)}</strong>
            <span class="health-label">${escapeHtml(status)} · ${escapeHtml(latency)}</span>
          </div>
          <div class="url-line">${escapeHtml(s.base_url)}</div>
          <div>${s.entity_sets.map((e) => `<span class="tool-pill">${escapeHtml(e)}</span>`).join("")}</div>
          ${h && h.error ? `<div class="url-line health-error">${escapeHtml(h.error)}</div>` : ""}
        </div>
      `;
      const del = document.createElement("button");
      del.className = "delete";
      del.textContent = "Remove";
      del.addEventListener("click", async () => {
        if (!confirm(`Remove ${s.name}?`)) return;
        await api(`/services/${s.id}`, { method: "DELETE" });
        loadServices();
      });
      li.appendChild(del);
      serviceListEl.appendChild(li);
    });
  } catch (e) {
    serviceListEl.innerHTML = `<li>Failed to load services: ${e.message}</li>`;
  }
}

addServiceForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    id: $("svcId").value.trim(),
    name: $("svcName").value.trim(),
    base_url: $("svcUrl").value.trim(),
    description: $("svcDesc").value.trim(),
  };
  if (!payload.id || !payload.name || !payload.base_url) return;
  try {
    await api("/services", { method: "POST", body: payload });
    addServiceForm.reset();
    loadServices();
  } catch (e) {
    alert("Failed to register: " + e.message);
  }
});

checkHealth();
loadSessions();
