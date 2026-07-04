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
let currentPaginationSession = null;
let currentTableData = null;

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
  const token = localStorage.getItem("admin_token");
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, {
    headers,
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

function addAssistantBubble(summary, result, scroll = true, paginationInfo = null) {
  const div = document.createElement("div");
  div.className = "bubble assistant";
  div.textContent = summary || "Done.";
  try {
  if (result && result.llm) {
    const badge = document.createElement("div");
    badge.className = `llm-badge llm-${result.llm.provider}`;
    const provider = result.llm.provider || "unknown";
    const latency = result.llm.latency_ms != null ? `${result.llm.latency_ms}ms` : "?";
    const tokens = result.llm.tokens ? ` · ${result.llm.tokens} tokens` : "";
    const corrected = result.llm.corrected ? " · self-corrected" : "";
    const cached = result.cached ? " · ⚡ cached" : "";
    const intent = result.llm.intent ? ` · ${result.llm.intent}` : "";
    badge.textContent = `LLM: ${provider} · ${latency}${tokens}${corrected}${cached}${intent}`;
    div.appendChild(badge);
  }
  const isExtremumAnswer = summary && (summary.startsWith("Least:") || summary.startsWith("Most:"));
  if (result && result.table && !isExtremumAnswer) {
    const tableWrap = renderTable(result.table, paginationInfo);
    if (tableWrap) {
      const panel = buildResultPanel(result.table);
      div.appendChild(panel.panelEl);
      tableWrap.style.display = "";
      panel.tableView.appendChild(tableWrap);
      panel.setTableRef(tableWrap.querySelector("table"));
      requestAnimationFrame(() => { try { panel.renderGraph(); } catch(e) { console.error("Graph render error:", e); } });
    }
  }
  // Chart recommendations
  if (result && result.chart_recommendations && result.chart_recommendations.length) {
    const chartDiv = document.createElement("div");
    chartDiv.className = "chart-recommendations";
    chartDiv.innerHTML = '<div class="section-label">Suggested Charts</div>' +
      result.chart_recommendations.map(c => {
        const icon = c.type === 'pie' ? '🥧' : c.type === 'bar' ? '📊' : c.type === 'line' ? '📈' : c.type === 'scatter' ? '⚬' : '📋';
        return `<button class="chart-rec-btn" data-config='${escapeHtml(JSON.stringify(c))}' title="Confidence: ${c.confidence}">${icon} ${escapeHtml(c.title)}</button>`;
      }).join("");
    div.appendChild(chartDiv);
  }
  // Drill-down links
  if (result && result.drill_down_links && result.drill_down_links.length) {
    const drillDiv = document.createElement("div");
    drillDiv.className = "drill-down-container";
    drillDiv.innerHTML = '<div class="section-label">Related Data</div>' +
      result.drill_down_links.map(l =>
        `<button class="drill-chip" data-query="${escapeHtml(l.query)}" title="${escapeHtml(l.description)}">${escapeHtml(l.entity)} → ${escapeHtml(l.via_column)}=${escapeHtml(l.via_value)}</button>`
      ).join("");
      div.appendChild(drillDiv);
  }
  // Auto-train result
  if (result && result.auto_train_result) {
    const atr = result.auto_train_result;
    const trainDiv = document.createElement("div");
    trainDiv.className = "auto-train-badge";
    const taskIcon = atr.task_type === "classification" ? "🏷️" : "📈";
    const metricsText = atr.task_type === "classification"
      ? `Accuracy: ${((atr.metrics?.accuracy || 0) * 100).toFixed(1)}%`
      : `R²: ${(atr.metrics?.r2 || 0).toFixed(4)}`;
    trainDiv.innerHTML = `${taskIcon} <strong>Auto-trained:</strong> ${escapeHtml(atr.algorithm)} → predict <strong>${escapeHtml(atr.target_column)}</strong> (${atr.task_type}) | ${metricsText} | ${atr.sample_count} samples`;
    div.appendChild(trainDiv);
  }
  // Analyze button (on-demand insights)
  if (result && result.tool_calls && result.tool_calls.some(t => t.type === "odata.query")) {
    const analyzeBtn = document.createElement("button");
    analyzeBtn.className = "analyze-btn";
    analyzeBtn.textContent = "🔍 Analyze";
    analyzeBtn.title = "Get AI insights about this data";
    analyzeBtn.addEventListener("click", async () => {
      analyzeBtn.disabled = true;
      analyzeBtn.textContent = "Analyzing...";
      try {
        const data = await api("/chat/analyze", {
          method: "POST",
          body: { query: lastQuery, session_id: currentSessionId, user_role: roleSelect.value },
        });
        renderInsightsPanel(div, data);
      } catch (e) {
        analyzeBtn.textContent = "Error: " + e.message;
      }
    });
    div.appendChild(analyzeBtn);
  }
  if (result && result.tool_calls && result.tool_calls.length) {
    const hasQuery = result.tool_calls.some(t => t.type === "odata.query");
    if (hasQuery) {
      const metaToggle = document.createElement("div");
      metaToggle.className = "meta-toggle";
      metaToggle.textContent = "Details";
      metaToggle.title = "Show/hide query details";
      const meta = document.createElement("div");
      meta.className = "meta meta-hidden";
      meta.innerHTML = result.tool_calls.map((t) => {
        if (t.type === "odata.query") {
          const correctedTag = t.corrected ? ` <span class="tool-pill corrected">corrected</span>` : "";
          return `<span class="tool-pill">${escapeHtml(t.service_id)}/${escapeHtml(t.entity_set)}</span> ${t.row_count} rows${correctedTag}<div class="url-line">${escapeHtml(t.url || "")}</div>`;
        }
        if (t.type === "prediction") {
          let displayVal = String(t.prediction);
          let confText = t.confidence || "";
          if (t.task_type === "classification") {
            const prob = Number(t.prediction);
            const label = prob >= 0.5 ? "Yes" : "No";
            const confPct = prob >= 0.5 ? (prob * 100).toFixed(0) : ((1 - prob) * 100).toFixed(0);
            displayVal = label;
            confText = `(confidence: ${confPct}%)`;
          }
          return `<span class="tool-pill" style="background:#3b82f6;color:white">prediction</span> <strong>${escapeHtml(t.target)}</strong> = <strong>${escapeHtml(displayVal)}</strong> <span style="opacity:0.6">${escapeHtml(confText)}</span>`;
        }
        return `<span class="tool-pill">error</span> ${escapeHtml(t.error || "")}`;
      }).join("");
      metaToggle.addEventListener("click", () => {
        meta.classList.toggle("meta-hidden");
        metaToggle.textContent = meta.classList.contains("meta-hidden") ? "Details" : "Hide";
      });
      div.appendChild(metaToggle);
      div.appendChild(meta);
    }
  }
  } catch(e) { console.error("Bubble render error:", e); }
  messagesEl.appendChild(div);
  if (scroll) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderInsightsPanel(bubbleDiv, data) {
  const existing = bubbleDiv.querySelector(".insights-panel");
  if (existing) existing.remove();

  const panel = document.createElement("div");
  panel.className = "insights-panel";

  let html = "";

  if (data.summary) {
    html += `<div class="insights-summary">${escapeHtml(data.summary)}</div>`;
  }

  if (data.profile) {
    const p = data.profile;
    html += `<div class="insights-profile">
      <span class="insight-stat">${p.row_count} rows</span>
      <span class="insight-stat">${p.column_count} columns</span>
      <span class="insight-stat">${p.numeric_columns?.length || 0} numeric</span>
      <span class="insight-stat">${p.categorical_columns?.length || 0} categorical</span>
      <span class="insight-stat">Quality: ${p.quality_score}/100</span>
    </div>`;
  }

  if (data.insights && data.insights.length) {
    html += `<div class="insights-section"><div class="insights-label">Data Insights</div><ul>`;
    data.insights.forEach(i => { html += `<li>${escapeHtml(i)}</li>`; });
    html += `</ul></div>`;
  }

  if (data.ml_recommendation && data.ml_recommendation.algorithm) {
    const ml = data.ml_recommendation;
    html += `<div class="insights-section insights-ml">
      <div class="insights-label">ML Recommendation</div>
      <div class="insights-ml-detail">
        <strong>${escapeHtml(ml.algorithm)}</strong> for <strong>${escapeHtml(ml.target_column || "N/A")}</strong>
        (${ml.task_type}) - ${escapeHtml(ml.reason || "")}
      </div>
    </div>`;
  }

  if (data.chart_insights && data.chart_insights.length) {
    html += `<div class="insights-section"><div class="insights-label">Chart Suggestions</div><ul>`;
    data.chart_insights.forEach(c => { html += `<li>${escapeHtml(c)}</li>`; });
    html += `</ul></div>`;
  }

  if (data.suggestions && data.suggestions.length) {
    html += `<div class="insights-section"><div class="insights-label">Try Next</div><div class="insights-suggestions">`;
    data.suggestions.forEach(s => {
      html += `<button class="insight-suggestion-chip" onclick="document.getElementById('queryInput').value='${escapeHtml(s)}';document.getElementById('queryInput').focus()">${escapeHtml(s)}</button>`;
    });
    html += `</div></div>`;
  }

  panel.innerHTML = html;
  bubbleDiv.appendChild(panel);
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

function updateTableVisibility(tableEl, allColumns, hiddenCols) {
  const ths = tableEl.querySelectorAll("thead th");
  ths.forEach((th, i) => {
    th.style.display = hiddenCols.has(allColumns[i]) ? "none" : "";
  });
  tableEl.querySelectorAll("tbody tr").forEach((tr) => {
    const tds = tr.querySelectorAll("td");
    tds.forEach((td, i) => {
      td.style.display = hiddenCols.has(allColumns[i]) ? "none" : "";
    });
  });
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

function formatCellValue(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") {
    if (Array.isArray(v)) return v.length ? `[${v.length} items]` : "[]";
    return JSON.stringify(v);
  }
  const s = String(v);
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
    const d = new Date(s);
    if (!isNaN(d.getTime())) return d.toLocaleDateString();
  }
  if (s.length > 60) return s.slice(0, 57) + "...";
  return s;
}

function renderTable(table, paginationInfo = null) {
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
      td.textContent = formatCellValue(row[c]);
      td.title = typeof row[c] === "object" ? JSON.stringify(row[c]) : String(row[c] ?? "");
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tableEl.appendChild(tbody);
  wrap.appendChild(tableEl);
  
  // Pagination controls
  if (paginationInfo && paginationInfo.total_count > paginationInfo.page_size) {
    const paginationDiv = document.createElement("div");
    paginationDiv.className = "pagination-controls";
    
    const info = document.createElement("span");
    info.className = "pagination-info";
    info.textContent = `Page ${paginationInfo.current_page} of ${paginationInfo.total_pages} (${paginationInfo.total_count} total rows)`;
    paginationDiv.appendChild(info);
    
    const buttons = document.createElement("div");
    buttons.className = "pagination-buttons";
    
    const prevBtn = document.createElement("button");
    prevBtn.className = "pagination-btn";
    prevBtn.textContent = "← Previous";
    prevBtn.disabled = !paginationInfo.has_prev;
    prevBtn.addEventListener("click", () => loadPage("prev"));
    buttons.appendChild(prevBtn);
    
    const nextBtn = document.createElement("button");
    nextBtn.className = "pagination-btn";
    nextBtn.textContent = "Next →";
    nextBtn.disabled = !paginationInfo.has_next;
    nextBtn.addEventListener("click", () => loadPage("next"));
    buttons.appendChild(nextBtn);
    
    paginationDiv.appendChild(buttons);
    wrap.appendChild(paginationDiv);
    
    // Store pagination state for this table
    wrap.dataset.paginationSession = currentPaginationSession;
  } else if (table.truncated || table.row_count > table.rows.length) {
    const note = document.createElement("div");
    note.className = "url-line";
    note.textContent = `Showing ${table.rows.length} of ${table.row_count} rows${table.total_count ? ` (total: ${table.total_count})` : ""}.`;
    wrap.appendChild(note);
  }
  return wrap;
}

async function loadPage(action, page = 1) {
  if (!currentPaginationSession) return;
  
  try {
    const result = await api("/odata/page", {
      method: "POST",
      body: { session_id: currentPaginationSession, action, page }
    });
    
    currentTableData = result.table;
    
    // Update the current table in the UI
    const lastBubble = messagesEl.querySelector(".bubble.assistant:last-child");
    if (lastBubble) {
      const tableWrapper = lastBubble.querySelector(".table-wrapper");
      if (tableWrapper) {
        const newTableWrap = renderTable(result.table, result.pagination);
        if (newTableWrap) {
          tableWrapper.parentNode.replaceChild(newTableWrap, tableWrapper);
        }
      }
    }
  } catch (err) {
    console.error("Pagination error:", err);
  }
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

const CLUSTER_COLORS = ["#8b5cf6", "#06b6d4", "#eab308", "#ef4444", "#22c55e", "#f97316", "#ec4899"];

function renderScatterPlot(canvasId, scatterData) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !scatterData || !scatterData.points || scatterData.points.length === 0) return;
  
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const padding = 50;
  
  // Clear canvas
  ctx.fillStyle = "#1e1e2e";
  ctx.fillRect(0, 0, width, height);
  
  const points = scatterData.points;
  const centroids = scatterData.centroids || [];
  
  // Find bounds
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  
  // Add padding to bounds
  const xRange = maxX - minX || 1;
  const yRange = maxY - minY || 1;
  minX -= xRange * 0.1;
  maxX += xRange * 0.1;
  minY -= yRange * 0.1;
  maxY += yRange * 0.1;
  
  // Scale functions
  const scaleX = (v) => padding + ((v - minX) / (maxX - minX)) * (width - 2 * padding);
  const scaleY = (v) => height - padding - ((v - minY) / (maxY - minY)) * (height - 2 * padding);
  
  // Draw grid lines
  ctx.strokeStyle = "#333355";
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const x = padding + (i / 4) * (width - 2 * padding);
    const y = padding + (i / 4) * (height - 2 * padding);
    ctx.beginPath();
    ctx.moveTo(x, padding);
    ctx.lineTo(x, height - padding);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(padding, y);
    ctx.lineTo(width - padding, y);
    ctx.stroke();
  }
  
  // Draw axes border
  ctx.strokeStyle = "#555577";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padding, padding);
  ctx.lineTo(padding, height - padding);
  ctx.lineTo(width - padding, height - padding);
  ctx.stroke();
  
  // Draw axis labels
  ctx.fillStyle = "#aaaacc";
  ctx.font = "11px 'Inter', sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(scatterData.x_label || "PC1", width / 2, height - 10);
  ctx.save();
  ctx.translate(15, height / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(scatterData.y_label || "PC2", 0, 0);
  ctx.restore();
  
  // Draw tick labels
  ctx.fillStyle = "#888899";
  ctx.font = "9px 'JetBrains Mono', monospace";
  ctx.textAlign = "center";
  for (let i = 0; i <= 4; i++) {
    const val = minX + (i / 4) * (maxX - minX);
    const x = padding + (i / 4) * (width - 2 * padding);
    ctx.fillText(val.toFixed(1), x, height - padding + 15);
  }
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const val = minY + (i / 4) * (maxY - minY);
    const y = height - padding - (i / 4) * (height - 2 * padding);
    ctx.fillText(val.toFixed(1), padding - 5, y + 3);
  }
  
  // Draw points
  for (const p of points) {
    const x = scaleX(p.x);
    const y = scaleY(p.y);
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fillStyle = CLUSTER_COLORS[p.cluster % CLUSTER_COLORS.length] + "cc";
    ctx.fill();
    ctx.strokeStyle = CLUSTER_COLORS[p.cluster % CLUSTER_COLORS.length];
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  
  // Draw centroids (larger diamonds)
  for (const c of centroids) {
    const x = scaleX(c.x);
    const y = scaleY(c.y);
    const size = 8;
    ctx.beginPath();
    ctx.moveTo(x, y - size);
    ctx.lineTo(x + size, y);
    ctx.lineTo(x, y + size);
    ctx.lineTo(x - size, y);
    ctx.closePath();
    ctx.fillStyle = "#0055ff";
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  
  // Draw legend
  const legendX = width - padding - 100;
  let legendY = padding + 10;
  ctx.fillStyle = "#1e1e2e";
  ctx.fillRect(legendX - 5, legendY - 5, 110, (scatterData.centroids.length + 1) * 20 + 10);
  ctx.strokeStyle = "#555577";
  ctx.strokeRect(legendX - 5, legendY - 5, 110, (scatterData.centroids.length + 1) * 20 + 10);
  
  ctx.font = "10px 'Inter', sans-serif";
  ctx.textAlign = "left";
  for (let i = 0; i < scatterData.centroids.length; i++) {
    // Cluster dot
    ctx.beginPath();
    ctx.arc(legendX + 5, legendY + 5, 4, 0, Math.PI * 2);
    ctx.fillStyle = CLUSTER_COLORS[i % CLUSTER_COLORS.length];
    ctx.fill();
    ctx.fillStyle = "#ccccee";
    ctx.fillText(`Cluster ${i}`, legendX + 15, legendY + 9);
    legendY += 20;
  }
  // Centroid legend
  ctx.beginPath();
  ctx.moveTo(legendX + 5, legendY - 3);
  ctx.lineTo(legendX + 10, legendY + 2);
  ctx.lineTo(legendX + 5, legendY + 7);
  ctx.lineTo(legendX, legendY + 2);
  ctx.closePath();
  ctx.fillStyle = "#0055ff";
  ctx.fill();
  ctx.fillStyle = "#ccccee";
  ctx.fillText("Centroid", legendX + 15, legendY + 5);
}

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
    // Scatter plot for K-Means
    if (cl.scatter_data && cl.scatter_data.points && cl.scatter_data.points.length > 0) {
      const sd = cl.scatter_data;
      const W = 600, H = 400, P = 50;
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (const pt of sd.points) {
        if (pt.x < minX) minX = pt.x; if (pt.x > maxX) maxX = pt.x;
        if (pt.y < minY) minY = pt.y; if (pt.y > maxY) maxY = pt.y;
      }
      const xr = (maxX - minX) || 1, yr = (maxY - minY) || 1;
      minX -= xr * 0.15; maxX += xr * 0.15; minY -= yr * 0.15; maxY += yr * 0.15;
      const sx = (v) => P + ((v - minX) / (maxX - minX)) * (W - 2 * P);
      const sy = (v) => H - P - ((v - minY) / (maxY - minY)) * (H - 2 * P);
      const colors = ["#8b5cf6","#06b6d4","#eab308","#ef4444","#22c55e"];
      let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;background:#1e1e2e;border-radius:8px;border:1px solid var(--border)">`;
      // grid
      for (let i = 0; i <= 5; i++) {
        const x = P + (i / 5) * (W - 2 * P), y = P + (i / 5) * (H - 2 * P);
        svg += `<line x1="${x}" y1="${P}" x2="${x}" y2="${H-P}" stroke="#333355" stroke-width="0.5"/>`;
        svg += `<line x1="${P}" y1="${y}" x2="${W-P}" y2="${y}" stroke="#333355" stroke-width="0.5"/>`;
      }
      // axes
      svg += `<line x1="${P}" y1="${P}" x2="${P}" y2="${H-P}" stroke="#555577" stroke-width="1"/>`;
      svg += `<line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}" stroke="#555577" stroke-width="1"/>`;
      // axis labels
      svg += `<text x="${W/2}" y="${H-8}" text-anchor="middle" fill="#aaaacc" font-size="11" font-family="sans-serif">${escapeHtml(sd.x_label||"PC1")}</text>`;
      svg += `<text x="14" y="${H/2}" text-anchor="middle" fill="#aaaacc" font-size="11" font-family="sans-serif" transform="rotate(-90,14,${H/2})">${escapeHtml(sd.y_label||"PC2")}</text>`;
      // tick labels
      for (let i = 0; i <= 5; i++) {
        const val = minX + (i / 5) * (maxX - minX);
        svg += `<text x="${P + (i/5)*(W-2*P)}" y="${H-P+16}" text-anchor="middle" fill="#888899" font-size="9" font-family="monospace">${val.toFixed(1)}</text>`;
      }
      for (let i = 0; i <= 5; i++) {
        const val = minY + (i / 5) * (maxY - minY);
        svg += `<text x="${P-6}" y="${H-P-(i/5)*(H-2*P)+4}" text-anchor="end" fill="#888899" font-size="9" font-family="monospace">${val.toFixed(1)}</text>`;
      }
      // points
      for (const pt of sd.points) {
        svg += `<circle cx="${sx(pt.x)}" cy="${sy(pt.y)}" r="5" fill="${colors[pt.cluster % colors.length]}" fill-opacity="0.85" stroke="white" stroke-width="0.5"/>`;
      }
      // centroids
      for (const c of sd.centroids) {
        const cx = sx(c.x), cy = sy(c.y);
        svg += `<polygon points="${cx},${cy-9} ${cx+9},${cy} ${cx},${cy+9} ${cx-9},${cy}" fill="#0066ff" stroke="white" stroke-width="2"/>`;
      }
      // legend
      let ly = P + 10;
      svg += `<rect x="${W-P-120}" y="${ly-8}" width="115" height="${sd.centroids.length*20+32}" rx="4" fill="#1e1e2e" stroke="#555577"/>`;
      for (let i = 0; i < sd.centroids.length; i++) {
        svg += `<circle cx="${W-P-105}" cy="${ly+5}" r="5" fill="${colors[i]}"/>`;
        svg += `<text x="${W-P-92}" y="${ly+9}" fill="#ccccee" font-size="10" font-family="sans-serif">Cluster ${i}</text>`;
        ly += 20;
      }
      svg += `<polygon points="${W-P-105},${ly-3} ${W-P-100},${ly+2} ${W-P-105},${ly+7} ${W-P-110},${ly+2}" fill="#0066ff"/>`;
      svg += `<text x="${W-P-92}" y="${ly+6}" fill="#ccccee" font-size="10" font-family="sans-serif">Centroid</text>`;
      svg += `</svg>`;
      html += `<div class="scatter-plot-container"><div class="scatter-title">Cluster Visualization (PCA Projection)</div>`;
      html += `<div class="scatter-subtitle">Variance explained: ${(sd.variance_explained * 100).toFixed(1)}%</div>`;
      html += svg;
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

function generateSupervisedInsights(res) {
  const insights = [];
  const target = res.target_column;
  const fi = res.feature_importance || [];
  const m = res.metrics;
  const task = res.task_type;
  const samples = res.sample_count;

  if (fi.length > 0) {
    const top = fi[0];
    const topPct = (top.importance * 100).toFixed(1);
    const topCol = top.column;

    if (top.importance > 0.5) {
      insights.push(`<strong>${topCol}</strong> is the dominant predictor of <strong>${target}</strong> (${topPct}% importance), meaning it has the strongest influence on the outcome.`);
    } else if (top.importance > 0.2) {
      insights.push(`<strong>${topCol}</strong> is the most important factor for predicting <strong>${target}</strong> (${topPct}% importance).`);
    } else {
      insights.push(`No single feature strongly predicts <strong>${target}</strong>. The model relies on a combination of factors.`);
    }

    if (fi.length >= 2) {
      const second = fi[1];
      const secondPct = (second.importance * 100).toFixed(1);
      if (second.importance > 0.15) {
        insights.push(`<strong>${second.column}</strong> is also significant (${secondPct}% importance).`);
      }
    }

    const lowFeatures = fi.filter(f => f.importance < 0.05);
    if (lowFeatures.length > 0) {
      const names = lowFeatures.map(f => `<strong>${f.column}</strong>`).join(", ");
      insights.push(`${names} have minimal impact on the prediction and could be removed without losing accuracy.`);
    }
  }

  if (task === "regression") {
    const r2 = parseFloat(m.r2);
    if (r2 > 0.85) {
      insights.push(`The model explains <strong>${(r2 * 100).toFixed(0)}%</strong> of the variance — excellent fit.`);
    } else if (r2 > 0.6) {
      insights.push(`The model explains <strong>${(r2 * 100).toFixed(0)}%</strong> of the variance — good fit but could be improved.`);
    } else if (r2 > 0.3) {
      insights.push(`The model explains only <strong>${(r2 * 100).toFixed(0)}%</strong> of the variance — moderate fit. Consider adding more features.`);
    } else {
      insights.push(`The model explains only <strong>${(r2 * 100).toFixed(0)}%</strong> of the variance — weak fit. The data may need more features or a different approach.`);
    }

    const mae = parseFloat(m.mae);
    if (mae < 0.05) {
      insights.push(`Average prediction error is very low (${mae}) — predictions are highly accurate.`);
    } else if (mae < 0.15) {
      insights.push(`Average prediction error is ${mae} — predictions are reasonably accurate.`);
    } else {
      insights.push(`Average prediction error is ${mae} — predictions have notable uncertainty.`);
    }

    if (fi.length > 0) {
      const topCol = fi[0].column;
      insights.push(`Higher values of <strong>${topCol}</strong> tend to correlate with <strong>${target}</strong>.`);
    }
  }

  if (task === "classification") {
    const acc = parseFloat(m.accuracy);
    if (acc > 0.9) {
      insights.push(`The model achieves <strong>${(acc * 100).toFixed(0)}%</strong> accuracy — excellent classification performance.`);
    } else if (acc > 0.75) {
      insights.push(`The model achieves <strong>${(acc * 100).toFixed(0)}%</strong> accuracy — good but room for improvement.`);
    } else if (acc > 0.6) {
      insights.push(`The model achieves <strong>${(acc * 100).toFixed(0)}%</strong> accuracy — moderate. Consider more data or better features.`);
    } else {
      insights.push(`The model achieves only <strong>${(acc * 100).toFixed(0)}%</strong> accuracy — weak performance. The features may not be sufficient for this classification.`);
    }

    const f1 = parseFloat(m.f1);
    if (f1 > 0.85) {
      insights.push(`F1 score of <strong>${f1}</strong> indicates strong precision-recall balance.`);
    } else if (f1 < 0.5) {
      insights.push(`F1 score of <strong>${f1}</strong> is low — the model struggles to balance false positives and false negatives.`);
    }

    if (fi.length > 0) {
      const topCol = fi[0].column;
      insights.push(`Products with higher <strong>${topCol}</strong> values are more likely to be classified as <strong>${target}</strong>.`);
    }
  }

  if (samples < 30) {
    insights.push(`⚠️ Only <strong>${samples}</strong> training samples — results may not generalize well. More data would improve reliability.`);
  } else if (samples < 100) {
    insights.push(`Training on <strong>${samples}</strong> samples — reasonable for initial analysis but more data would help.`);
  }

  return insights;
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
  const insights = generateSupervisedInsights(res);
  if (insights.length > 0) {
    html += `<div class="analyze-subtitle">Business Insights</div><div class="insights-list">`;
    for (const ins of insights) {
      html += `<div class="insight-item">${ins}</div>`;
    }
    html += `</div>`;
  }
  if (res.confusion_matrix) {    html += `<div class="analyze-subtitle">Confusion Matrix</div>`;
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

  // Column picker in header
  // Column toggle button — shows all columns or only filtered
  const allColumns = [...table.columns];
  const tableRef = { el: null };
  let showingAll = false;

  const colBtn = document.createElement("button");
  colBtn.className = "col-toggle-btn";
  colBtn.textContent = `Show All Columns (${allColumns.length})`;
  colBtn.title = "Toggle all columns on/off";
  colBtn.addEventListener("click", () => {
    if (!tableRef.el) return;
    showingAll = !showingAll;
    if (showingAll) {
      // Show all columns — unhide everything
      updateTableVisibility(tableRef.el, allColumns, new Set());
      colBtn.textContent = `Show Filtered (${allColumns.length})`;
      colBtn.classList.add("active");
    } else {
      // Re-apply filter — hide useless columns
      const useless = new Set();
      const rows = [];
      const trs = tableRef.el.querySelectorAll("tbody tr");
      trs.forEach(tr => {
        const row = {};
        const tds = tr.querySelectorAll("td");
        tds.forEach((td, i) => { row[allColumns[i]] = td.textContent; });
        rows.push(row);
      });
      allColumns.forEach(col => {
        if (isUselessCol(col, rows)) useless.add(col);
      });
      updateTableVisibility(tableRef.el, allColumns, useless);
      colBtn.textContent = `Show All Columns (${allColumns.length})`;
      colBtn.classList.remove("active");
    }
  });
  tabsEl.appendChild(colBtn);

  function isUselessCol(col, rows) {
    if (!rows.length) return false;
    const vals = rows.map(r => (r[col] || "").trim());
    const unique = new Set(vals);
    if (unique.size <= 1) return true;
    const empty = vals.filter(v => v === "" || v === "0" || v === "0.0" || v === "0.00" || v.startsWith("000000000")).length;
    if (empty / vals.length >= 0.95) return true;
    if (/InternalNumber|CharcInternal|ConfigurableProd|ProdCharc/i.test(col)) return true;
    return false;
  }

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
  return { panelEl, tableView, renderGraph, setTableRef: (el) => { tableRef.el = el; } };
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

function addProcessingBubble(text) {
  const div = document.createElement("div");
  div.className = "bubble assistant processing-bubble";
  div.innerHTML = `<span class="processing-text">${escapeHtml(text)}</span><span class="typing-dots"><span></span><span></span><span></span></span>`;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function removeProcessingBubble() {
  const el = messagesEl.querySelector(".processing-bubble");
  if (el) el.remove();
}

async function send() {
  if (isLoading) return;
  const q = queryInput.value.trim();
  if (!q) return;
  isLoading = true;
  sendBtn.disabled = true;
  sendBtn.textContent = "Sending...";
  lastQuery = q;
  lastSummary = "";
  lastTable = null;
  if (messagesEl.querySelector(".empty-state")) messagesEl.innerHTML = "";
  addUserBubble(q);
  queryInput.value = "";
  const processingEl = addProcessingBubble("Processing your query...");
  try {
    const resp = await api("/chat", {
      method: "POST",
      body: {
        query: q,
        session_id: currentSessionId,
        user_role: roleSelect.value,
        selected_entities: selectedEntities.length > 0 ? selectedEntities : undefined,
      },
    });
    removeProcessingBubble();
    currentSessionId = resp.session_id;
    lastSummary = resp.summary;
    lastTable = resp.table;
    addAssistantBubble(resp.summary, {
      table: resp.table,
      tool_calls: resp.tool_calls,
      llm: {
        provider: resp.llm_provider,
        latency_ms: resp.llm_latency_ms,
        tokens: resp.llm_tokens,
        corrected: (resp.tool_calls || []).some((t) => t.corrected),
        intent: resp.intent,
      },
    });
    loadSessions();
  } catch (e) {
    removeProcessingBubble();
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

// Event delegation for chart recommendations and drill-down chips
messagesEl.addEventListener("click", (e) => {
  const chartBtn = e.target.closest(".chart-rec-btn");
  if (chartBtn) {
    try {
      const config = JSON.parse(chartBtn.dataset.config);
      showChartRecommendation(config);
    } catch (err) {}
    return;
  }
  const drillBtn = e.target.closest(".drill-chip");
  if (drillBtn) {
    queryInput.value = drillBtn.dataset.query;
    send();
    return;
  }
});

function showChartRecommendation(config) {
  const lastBubble = messagesEl.querySelector(".bubble.assistant:last-child");
  if (!lastBubble) return;
  const graphCanvas = lastBubble.querySelector("#graph-canvas");
  if (!graphCanvas) return;
  const graphTab = lastBubble.querySelector('[data-tab="graph"]');
  if (graphTab) graphTab.click();
}

// ─── Query Suggestions / Autocomplete ─────────────────────────────────────────
const suggestionsDropdown = document.getElementById("suggestionsDropdown");
let allSuggestions = [];

async function loadSuggestions() {
  try {
    const resp = await api("/suggestions");
    allSuggestions = resp.suggestions || [];
  } catch (e) {}
}

function showSuggestions(query) {
  if (!suggestionsDropdown || !query || query.length < 2) {
    if (suggestionsDropdown) suggestionsDropdown.classList.add("hidden");
    return;
  }
  const q = query.toLowerCase();
  const matches = allSuggestions.filter(s =>
    s.query.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)
  ).slice(0, 8);
  if (matches.length === 0) {
    suggestionsDropdown.classList.add("hidden");
    return;
  }
  suggestionsDropdown.innerHTML = matches.map(s =>
    `<div class="suggestion-item" data-query="${escapeHtml(s.query)}">
      <span class="suggestion-query">${escapeHtml(s.query)}</span>
      <span class="suggestion-desc">${escapeHtml(s.description)}</span>
    </div>`
  ).join("");
  suggestionsDropdown.classList.remove("hidden");
}

if (suggestionsDropdown) {
  suggestionsDropdown.addEventListener("click", (e) => {
    const item = e.target.closest(".suggestion-item");
    if (item) {
      queryInput.value = item.dataset.query;
      suggestionsDropdown.classList.add("hidden");
      queryInput.focus();
    }
  });
}

queryInput.addEventListener("input", () => showSuggestions(queryInput.value));
queryInput.addEventListener("blur", () => {
  setTimeout(() => suggestionsDropdown && suggestionsDropdown.classList.add("hidden"), 200);
});

loadSuggestions();

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
  const authType = $("svcAuthType").value;
  const payload = {
    id: $("svcId").value.trim(),
    name: $("svcName").value.trim(),
    base_url: $("svcUrl").value.trim(),
    description: $("svcDesc").value.trim(),
    auth_type: authType || null,
  };
  if (authType === "basic") {
    payload.auth_username = $("svcAuthUser").value.trim();
    payload.auth_password = $("svcAuthPass").value;
  } else if (authType === "bearer") {
    payload.auth_token = $("svcAuthToken").value.trim();
  } else if (authType === "api_key") {
    payload.auth_api_key = $("svcAuthApiKey").value.trim();
  }
  if (!payload.id || !payload.name || !payload.base_url) return;
  try {
    await api("/services", { method: "POST", body: payload });
    addServiceForm.reset();
    $("authFields").classList.add("hidden");
    loadServices();
  } catch (e) {
    alert("Failed to register: " + e.message);
  }
});

// Auth type toggle
const svcAuthType = $("svcAuthType");
if (svcAuthType) {
  svcAuthType.addEventListener("change", () => {
    const t = svcAuthType.value;
    const authFields = $("authFields");
    const user = $("svcAuthUser");
    const pass = $("svcAuthPass");
    const token = $("svcAuthToken");
    const apiKey = $("svcAuthApiKey");
    authFields.classList.toggle("hidden", !t);
    user.classList.toggle("hidden", t !== "basic");
    pass.classList.toggle("hidden", t !== "basic");
    token.classList.toggle("hidden", t !== "bearer");
    apiKey.classList.toggle("hidden", t !== "api_key");
  });
}

checkHealth();
loadSessions();

const shareFab = $("shareFab");
const shareModal = $("shareModal");
const closeShareModal = $("closeShareModal");
const sharePreview = $("sharePreview");
const shareStatus = $("shareStatus");
let lastQuery = "";
let lastSummary = "";
let lastTable = null;

function trackLastMessage(query, summary, table) {
  lastQuery = query || "";
  lastSummary = summary || "";
  lastTable = table || null;
}

if (shareFab) {
  shareFab.addEventListener("click", () => {
    if (!lastQuery && !lastSummary) {
      shareStatus.textContent = "No chat content to share yet.";
      shareStatus.className = "share-status error";
      shareModal.classList.remove("hidden");
      return;
    }
    let preview = `Query: ${lastQuery}\n\n${lastSummary}`;
    if (lastTable && lastTable.rows && lastTable.rows.length) {
      preview += `\n\nData: ${lastTable.rows.length} rows (${lastTable.columns.join(", ")})`;
    }
    sharePreview.textContent = preview;
    shareStatus.textContent = "";
    shareStatus.className = "share-status";
    shareModal.classList.remove("hidden");
  });
}

if (closeShareModal) {
  closeShareModal.addEventListener("click", () => shareModal.classList.add("hidden"));
  shareModal.addEventListener("click", (e) => {
    if (e.target === shareModal) shareModal.classList.add("hidden");
  });
}

document.querySelectorAll(".share-channel-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const channel = btn.dataset.channel;
    if (channel === "clipboard" || channel === "copy") {
      let text = `Query: ${lastQuery}\n\n${lastSummary}`;
      if (lastTable && lastTable.rows) {
        const cols = lastTable.columns;
        text += "\n\n" + cols.join(" | ") + "\n";
        text += lastTable.rows.slice(0, 20).map((r) => cols.map((c) => r[c] ?? "").join(" | ")).join("\n");
        if (lastTable.rows.length > 20) text += `\n... and ${lastTable.rows.length - 20} more rows`;
      }
      try {
        await navigator.clipboard.writeText(text);
        shareStatus.textContent = "Copied to clipboard!";
        shareStatus.className = "share-status success";
      } catch {
        try {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
          shareStatus.textContent = "Copied to clipboard!";
          shareStatus.className = "share-status success";
        } catch {
          shareStatus.textContent = "Failed to copy. Please select and copy manually.";
          shareStatus.className = "share-status error";
        }
      }
      return;
    }
    if (channel === "email" || channel === "whatsapp" || channel === "slack") {
      let text = `Query: ${lastQuery}\n\n${lastSummary}`;
      if (lastTable && lastTable.rows) {
        const cols = lastTable.columns;
        text += "\n\n" + cols.join(" | ") + "\n";
        text += lastTable.rows.slice(0, 20).map((r) => cols.map((c) => r[c] ?? "").join(" | ")).join("\n");
        if (lastTable.rows.length > 20) text += `\n... and ${lastTable.rows.length - 20} more rows`;
      }
      try {
        const resp = await api("/share", {
          method: "POST",
          body: { channel, query: lastQuery, summary: lastSummary, table: lastTable, session_id: currentSessionId || "" },
        });
        if (resp.success) {
          const labels = { email: "Email sent!", whatsapp: "WhatsApp sent!", slack: "Shared to Slack!" };
          shareStatus.textContent = labels[channel] || "Shared!";
          shareStatus.className = "share-status success";
        } else {
          shareStatus.textContent = `${channel} failed: ${resp.detail || "n8n workflow not configured"}`;
          shareStatus.className = "share-status error";
        }
      } catch (e) {
        shareStatus.textContent = `${channel} requires n8n webhook. Check n8n is running.`;
        shareStatus.className = "share-status error";
      }
      return;
    }
  });
});

const origAddAssistant = addAssistantBubble;
function addAssistantBubbleTracked(summary, result, scroll, paginationInfo) {
  origAddAssistant(summary, result, scroll, paginationInfo);
  if (result) {
    trackLastMessage(lastQuery, summary, result.table);
  }
}

// ==================== ENTITY SELECTOR ====================

let entityPanelOpen = false;
let allServices = [];
let selectedEntities = [];
let detectedJoins = [];
let currentEntityService = null;

const entityPanel = $("entityPanel");
const entitySelectorBtn = $("entitySelectorBtn");
const closeEntityPanel = $("closeEntityPanel");
const entityServiceTabs = $("entityServiceTabs");
const entitySearch = $("entitySearch");
const entityList = $("entityList");
const detectedJoinsEl = $("detectedJoins");
const entitySelectedCount = $("entitySelectedCount");
const executeEntityJoinBtn = $("executeEntityJoin");

entitySelectorBtn.addEventListener("click", toggleEntityPanel);
closeEntityPanel.addEventListener("click", () => {
  entityPanel.classList.add("hidden");
  entityPanelOpen = false;
});

function toggleEntityPanel() {
  entityPanelOpen = !entityPanelOpen;
  entityPanel.classList.toggle("hidden", !entityPanelOpen);
  if (entityPanelOpen && allServices.length === 0) {
    loadEntityServices();
  }
}

async function loadEntityServices() {
  try {
    allServices = await api("/services");
    renderServiceTabs();
    if (allServices.length > 0) {
      selectEntityService(allServices[0].id);
    }
  } catch (e) {
    console.error("Failed to load services for entity selector", e);
  }
}

function renderServiceTabs() {
  entityServiceTabs.innerHTML = "";
  allServices.forEach((svc) => {
    const tab = document.createElement("button");
    tab.className = "entity-service-tab" + (svc.id === currentEntityService ? " active" : "");
    tab.textContent = svc.name;
    tab.dataset.serviceId = svc.id;
    tab.addEventListener("click", () => selectEntityService(svc.id));
    entityServiceTabs.appendChild(tab);
  });
}

async function selectEntityService(serviceId) {
  currentEntityService = serviceId;
  renderServiceTabs();
  entitySearch.value = "";
  try {
    const data = await api(`/entities/${serviceId}`);
    renderEntityList(data.entities || []);
  } catch (e) {
    entityList.innerHTML = `<div style="padding:10px;color:var(--text-muted)">Failed to load entities</div>`;
  }
}

function renderEntityList(entities) {
  entityList.innerHTML = "";
  const search = entitySearch.value.toLowerCase();
  const filtered = search
    ? entities.filter((e) => e.name.toLowerCase().includes(search))
    : entities;
  filtered.forEach((ent) => {
    const isSelected = selectedEntities.some(
      (s) => s.service_id === currentEntityService && s.entity_name === ent.name
    );
    // Count property labels
    const props = ent.properties || [];
    const labelCounts = {};
    props.forEach((p) => {
      const lbl = typeof p === "object" ? (p.label || "Attribute") : "Attribute";
      labelCounts[lbl] = (labelCounts[lbl] || 0) + 1;
    });
    const labelTags = Object.entries(labelCounts)
      .map(([lbl, cnt]) => `<span class="prop-label-tag prop-label-${lbl.toLowerCase()}">${lbl}: ${cnt}</span>`)
      .join("");
    const item = document.createElement("label");
    item.className = "entity-checkbox-item";
    item.innerHTML = `
      <input type="checkbox" ${isSelected ? "checked" : ""} data-entity="${ent.name}" />
      <span>${escapeHtml(ent.name)}</span>
      <span class="entity-props">${ent.property_count} props</span>
      <div class="prop-labels">${labelTags}</div>
    `;
    const checkbox = item.querySelector("input");
    checkbox.addEventListener("change", () => {
      toggleEntity(currentEntityService, ent.name, ent.properties, checkbox.checked);
    });
    entityList.appendChild(item);
  });
  if (filtered.length === 0) {
    entityList.innerHTML = `<div style="padding:10px;color:var(--text-muted)">No entities found</div>`;
  }
}

entitySearch.addEventListener("input", async () => {
  if (!currentEntityService) return;
  try {
    const data = await api(`/entities/${currentEntityService}`);
    renderEntityList(data.entities || []);
  } catch (e) {}
});

function toggleEntity(serviceId, entityName, properties, checked) {
  if (checked) {
    if (!selectedEntities.some((s) => s.service_id === serviceId && s.entity_name === entityName)) {
      selectedEntities.push({ service_id: serviceId, entity_name: entityName, properties: properties || [] });
    }
  } else {
    selectedEntities = selectedEntities.filter(
      (s) => !(s.service_id === serviceId && s.entity_name === entityName)
    );
  }
  updateEntityCount();
  detectJoinsFromSelection();
}

function updateEntityCount() {
  const count = selectedEntities.length;
  if (count === 0) {
    entitySelectedCount.textContent = "No entities selected";
    entitySelectedCount.className = "";
  } else if (count === 1) {
    entitySelectedCount.textContent = "1 entity selected — will query this entity in chat";
    entitySelectedCount.className = "entity-status-active";
  } else {
    entitySelectedCount.textContent = `${count} entities selected — will auto-join in chat`;
    entitySelectedCount.className = "entity-status-active";
  }
  executeEntityJoinBtn.disabled = count < 2;
  executeEntityJoinBtn.textContent = count < 2 ? "Select 2+ to join" : "Execute Join (optional)";
}

async function detectJoinsFromSelection() {
  if (selectedEntities.length < 2) {
    detectedJoins = [];
    renderDetectedJoins();
    return;
  }
  try {
    const resp = await api("/entities/auto-join", {
      method: "POST",
      body: { entities: selectedEntities },
    });
    detectedJoins = resp.joins || [];
    renderDetectedJoins();
  } catch (e) {
    detectedJoins = [];
    renderDetectedJoins();
  }
}

function renderDetectedJoins() {
  if (detectedJoins.length === 0) {
    detectedJoinsEl.innerHTML = "";
    return;
  }
  const labelNames = { primary_key: "Primary Key", foreign_key: "Foreign Key", attribute: "Attribute", fuzzy: "Fuzzy", neo4j: "Confirmed" };
  let html = `<div class="detected-joins-title">Detected Joins (${detectedJoins.length})</div>`;
  detectedJoins.forEach((j) => {
    const label = j.label || "attribute";
    const labelName = labelNames[label] || label;
    html += `
      <div class="join-item join-item-${label}">
        <span class="join-label join-label-${label}">${escapeHtml(labelName)}</span>
        <span class="join-key">${escapeHtml(j.left_entity)}.${escapeHtml(j.left_key)}</span>
        <span>=</span>
        <span class="join-key">${escapeHtml(j.right_entity)}.${escapeHtml(j.right_key)}</span>
        <span class="join-confidence">${Math.round(j.confidence * 100)}%</span>
      </div>
    `;
  });
  detectedJoinsEl.innerHTML = html;
}

executeEntityJoinBtn.addEventListener("click", async () => {
  if (selectedEntities.length === 0) return;
  const query = $("queryInput").value.trim() || `Show data from ${selectedEntities.map((e) => e.entity_name).join(" and ")}`;
  executeEntityJoinBtn.disabled = true;
  executeEntityJoinBtn.textContent = "Executing...";
  try {
    addProcessingBubble("Executing entity join...");
    const resp = await api("/entities/execute-join", {
      method: "POST",
      body: {
        entities: selectedEntities,
        joins: detectedJoins,
        query: query,
        top: 10,
      },
    });
    removeProcessingBubble();
    if (resp.error) {
      addAssistantBubble(resp.error, null, true);
    } else {
      const rowCount = resp.table?.row_count || 0;
      let summary = `Joined ${resp.entity_count} entities with ${resp.join_count} join(s). Found ${rowCount} rows.`;
      if (rowCount === 0) {
        summary += " No matching rows found between the selected entities. Try selecting different entities or adjusting the join key.";
      }
      addAssistantBubble(summary, resp, true);
    }
  } catch (e) {
    removeProcessingBubble();
    addAssistantBubble(`Entity join failed: ${e.message}`, null, true);
  }
  executeEntityJoinBtn.disabled = false;
  executeEntityJoinBtn.textContent = "Execute Join";
  updateEntityCount();
});
