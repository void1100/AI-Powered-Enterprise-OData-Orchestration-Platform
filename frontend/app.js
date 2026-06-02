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
  if (result && result.table) {
    const wrap = renderTable(result.table);
    if (wrap) div.appendChild(wrap);
  }
  if (result && result.tool_calls && result.tool_calls.length) {
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = result.tool_calls.map((t) => {
      if (t.type === "odata.query") {
        return `<span class="tool-pill">${escapeHtml(t.service_id)}/${escapeHtml(t.entity_set)}</span> ${t.row_count} rows<div class="url-line">${escapeHtml(t.url || "")}</div>`;
      }
      return `<span class="tool-pill">error</span> ${escapeHtml(t.error || "")}`;
    }).join("");
    div.appendChild(meta);
  }
  messagesEl.appendChild(div);
  if (scroll) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderTable(table) {
  if (!table || !table.columns || !table.rows || !table.rows.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "table-wrapper";
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
    addAssistantBubble(resp.summary, { table: resp.table, tool_calls: resp.tool_calls });
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
    services.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<div><strong>${escapeHtml(s.name)}</strong><div class="url-line">${escapeHtml(s.base_url)}</div><div>${s.entity_sets.map((e) => `<span class="tool-pill">${escapeHtml(e)}</span>`).join("")}</div></div>`;
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
