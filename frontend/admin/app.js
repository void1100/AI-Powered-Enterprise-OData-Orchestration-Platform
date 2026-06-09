const API = window.location.port === "3000"
  ? `${window.location.protocol}//${window.location.hostname}:8000`
  : `${window.location.protocol}//${window.location.hostname}:8000`;

let currentUser = null;
let currentPage = "dashboard";

// --- API Helper ---
async function api(path, opts = {}) {
  const token = localStorage.getItem("admin_token");
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API}${path}`, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { logout(); throw new Error("Session expired"); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// --- Auth ---
async function login(username, password) {
  const data = await api("/auth/login", { method: "POST", body: { username, password } });
  localStorage.setItem("admin_token", data.access_token);
  localStorage.setItem("admin_refresh", data.refresh_token);
  currentUser = data.user;
  showAdmin();
}

function logout() {
  localStorage.removeItem("admin_token");
  localStorage.removeItem("admin_refresh");
  currentUser = null;
  document.getElementById("loginScreen").classList.remove("hidden");
  document.getElementById("adminShell").classList.add("hidden");
}

async function checkAuth() {
  const token = localStorage.getItem("admin_token");
  if (!token) return false;
  try {
    currentUser = await api("/auth/me");
    return true;
  } catch {
    return false;
  }
}

// --- Toast ---
function toast(msg, type = "success") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast toast-${type}`;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3000);
}

// --- Modal ---
function showModal(html) {
  document.getElementById("modalCard").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

// --- Theme ---
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
}

// --- Navigation ---
function navigateTo(page) {
  currentPage = page;
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  document.querySelector(`[data-page="${page}"]`)?.classList.add("active");
  document.getElementById("pageTitle").textContent = {
    dashboard: "Dashboard",
    users: "User Management",
    roles: "Role Management",
    services: "Service Management",
    analytics: "Analytics",
    audit: "Audit Log",
    settings: "System Settings",
  }[page] || page;
  loadPage(page);
}

async function loadPage(page) {
  const content = document.getElementById("pageContent");
  content.innerHTML = `<div class="loading">Loading...</div>`;
  try {
    if (page === "dashboard") await loadDashboard(content);
    else if (page === "users") await loadUsers(content);
    else if (page === "roles") await loadRoles(content);
    else if (page === "services") await loadServices(content);
    else if (page === "analytics") await loadAnalytics(content);
    else if (page === "audit") await loadAudit(content);
    else if (page === "settings") await loadSettings(content);
  } catch (e) {
    content.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(e.message)}</p></div>`;
  }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

// --- Dashboard ---
async function loadDashboard(el) {
  const data = await api("/admin/dashboard");
  el.innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-value">${data.total_users}</div><div class="stat-label">Total Users</div></div>
      ${Object.entries(data.user_counts).map(([role, cnt]) => `
        <div class="stat-card"><div class="stat-value">${cnt}</div><div class="stat-label">${escapeHtml(role)}</div></div>
      `).join("")}
    </div>
    <div class="table-wrap">
      <div class="table-header"><h2>Recent Activity</h2></div>
      <table>
        <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Resource</th><th>Status</th></tr></thead>
        <tbody>
          ${data.recent_activity.length === 0 ? '<tr><td colspan="5" class="empty-state"><p>No activity yet</p></td></tr>' : ""}
          ${data.recent_activity.map(a => `
            <tr>
              <td>${new Date(a.created_at).toLocaleString()}</td>
              <td>${escapeHtml(a.username || "—")}</td>
              <td><span class="badge badge-${a.status === "success" ? "active" : "locked"}">${escapeHtml(a.action)}</span></td>
              <td>${escapeHtml(a.resource)}</td>
              <td>${escapeHtml(a.status)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

// --- Users ---
async function loadUsers(el) {
  const users = await api("/admin/users");
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header">
        <h2>Users (${users.length})</h2>
        <div class="table-actions">
          <input type="text" class="table-search" placeholder="Search users..." oninput="filterUsers(this.value)">
          <button class="btn btn-primary btn-sm" onclick="showCreateUser()">+ Add User</button>
        </div>
      </div>
      <table id="usersTable">
        <thead><tr><th>Username</th><th>Email</th><th>Role</th><th>Status</th><th>Last Login</th><th>Actions</th></tr></thead>
        <tbody>
          ${users.map(u => `
            <tr data-search="${(u.username + u.email + u.display_name).toLowerCase()}">
              <td><strong>${escapeHtml(u.username)}</strong></td>
              <td>${escapeHtml(u.email)}</td>
              <td><span class="badge badge-${u.role}">${escapeHtml(u.role)}</span></td>
              <td><span class="badge badge-${u.status}">${escapeHtml(u.status)}</span></td>
              <td>${u.last_login ? new Date(u.last_login).toLocaleString() : "Never"}</td>
              <td>
                <button class="btn btn-ghost btn-sm" onclick="showEditUser(${u.id})">Edit</button>
                ${u.username !== "admin" ? `<button class="btn btn-danger btn-sm" onclick="confirmDeleteUser(${u.id}, '${escapeHtml(u.username)}')">Delete</button>` : ""}
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function filterUsers(q) {
  const rows = document.querySelectorAll("#usersTable tbody tr");
  rows.forEach(r => { r.style.display = r.dataset.search.includes(q.toLowerCase()) ? "" : "none"; });
}

function showCreateUser() {
  showModal(`
    <div class="modal-title">Create User</div>
    <form id="createUserForm" onsubmit="createUser(event)">
      <div class="form-group"><label>Username</label><input type="text" id="newUsername" required></div>
      <div class="form-group"><label>Email</label><input type="email" id="newEmail" required></div>
      <div class="form-group"><label>Password</label><input type="password" id="newPassword" required minlength="8"></div>
      <div class="form-group"><label>Role</label><select id="newRole">
        <option value="user">User</option><option value="analyst">Analyst</option><option value="admin">Admin</option><option value="viewer">Viewer</option>
      </select></div>
      <div class="form-group"><label>Display Name</label><input type="text" id="newDisplayName"></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Create</button>
      </div>
    </form>
  `);
}

async function createUser(e) {
  e.preventDefault();
  try {
    await api("/admin/users", { method: "POST", body: {
      username: document.getElementById("newUsername").value,
      email: document.getElementById("newEmail").value,
      password: document.getElementById("newPassword").value,
      role: document.getElementById("newRole").value,
      display_name: document.getElementById("newDisplayName").value,
    }});
    closeModal(); toast("User created"); loadUsers(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

async function showEditUser(id) {
  const users = await api("/admin/users");
  const u = users.find(x => x.id === id);
  if (!u) return;
  showModal(`
    <div class="modal-title">Edit User: ${escapeHtml(u.username)}</div>
    <form id="editUserForm" onsubmit="updateUser(event, ${id})">
      <div class="form-group"><label>Email</label><input type="email" id="editEmail" value="${escapeHtml(u.email)}"></div>
      <div class="form-group"><label>Role</label><select id="editRole">
        ${["super_admin","admin","analyst","user","viewer"].map(r => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
      </select></div>
      <div class="form-group"><label>Status</label><select id="editStatus">
        ${["active","disabled","locked"].map(s => `<option value="${s}" ${u.status === s ? "selected" : ""}>${s}</option>`).join("")}
      </select></div>
      <div class="form-group"><label>Display Name</label><input type="text" id="editDisplayName" value="${escapeHtml(u.display_name || "")}"></div>
      <div class="form-group"><label>New Password (leave blank to keep)</label><input type="password" id="editPassword" minlength="8"></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>
  `);
}

async function updateUser(e, id) {
  e.preventDefault();
  try {
    const body = {
      email: document.getElementById("editEmail").value,
      role: document.getElementById("editRole").value,
      status: document.getElementById("editStatus").value,
      display_name: document.getElementById("editDisplayName").value,
    };
    const pw = document.getElementById("editPassword").value;
    if (pw) body.password = pw;
    await api(`/admin/users/${id}`, { method: "PATCH", body });
    closeModal(); toast("User updated"); loadUsers(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

function confirmDeleteUser(id, username) {
  showModal(`
    <div class="modal-title">Delete User</div>
    <p>Are you sure you want to delete <strong>${escapeHtml(username)}</strong>?</p>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" onclick="deleteUser(${id})">Delete</button>
    </div>
  `);
}

async function deleteUser(id) {
  try {
    await api(`/admin/users/${id}`, { method: "DELETE" });
    closeModal(); toast("User deleted"); loadUsers(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

// --- Roles ---
async function loadRoles(el) {
  const roles = await api("/admin/roles");
  const isSuperAdmin = currentUser.role === "super_admin";
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header">
        <h2>Roles (${roles.length})</h2>
        ${isSuperAdmin ? '<div class="table-actions"><button class="btn btn-primary btn-sm" onclick="showCreateRole()">+ Add Role</button></div>' : ""}
      </div>
      <table>
        <thead><tr><th>Name</th><th>Display Name</th><th>Description</th><th>Permissions</th><th>System</th>${isSuperAdmin ? "<th>Actions</th>" : ""}</tr></thead>
        <tbody>
          ${roles.map(r => `
            <tr>
              <td><span class="badge badge-${r.name}">${escapeHtml(r.name)}</span></td>
              <td>${escapeHtml(r.display_name)}</td>
              <td>${escapeHtml(r.description)}</td>
              <td><code style="font-size:11px">${escapeHtml(r.permissions)}</code></td>
              <td>${r.is_system ? "✓" : ""}</td>
              ${isSuperAdmin ? `<td>${!r.is_system ? `<button class="btn btn-ghost btn-sm" onclick="showEditRole('${r.name}')">Edit</button> <button class="btn btn-danger btn-sm" onclick="confirmDeleteRole('${r.name}')">Delete</button>` : ""}</td>` : ""}
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function showCreateRole() {
  showModal(`
    <div class="modal-title">Create Role</div>
    <form onsubmit="createRole(event)">
      <div class="form-group"><label>Name</label><input type="text" id="newRoleName" required pattern="[a-z_]+"></div>
      <div class="form-group"><label>Display Name</label><input type="text" id="newRoleDisplay" required></div>
      <div class="form-group"><label>Description</label><input type="text" id="newRoleDesc"></div>
      <div class="form-group"><label>Permissions (JSON)</label><textarea id="newRolePerms" rows="4" style="width:100%;padding:8px;background:var(--bg-elev);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:var(--mono);font-size:12px">{"queries":"rw","history":"r"}</textarea></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Create</button>
      </div>
    </form>
  `);
}

async function createRole(e) {
  e.preventDefault();
  try {
    await api("/admin/roles", { method: "POST", body: {
      name: document.getElementById("newRoleName").value,
      display_name: document.getElementById("newRoleDisplay").value,
      description: document.getElementById("newRoleDesc").value,
      permissions: document.getElementById("newRolePerms").value,
    }});
    closeModal(); toast("Role created"); loadRoles(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

async function showEditRole(name) {
  const roles = await api("/admin/roles");
  const r = roles.find(x => x.name === name);
  if (!r) return;
  showModal(`
    <div class="modal-title">Edit Role: ${escapeHtml(r.name)}</div>
    <form onsubmit="updateRole(event, '${r.name}')">
      <div class="form-group"><label>Display Name</label><input type="text" id="editRoleDisplay" value="${escapeHtml(r.display_name)}"></div>
      <div class="form-group"><label>Description</label><input type="text" id="editRoleDesc" value="${escapeHtml(r.description)}"></div>
      <div class="form-group"><label>Permissions (JSON)</label><textarea id="editRolePerms" rows="4" style="width:100%;padding:8px;background:var(--bg-elev);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:var(--mono);font-size:12px">${escapeHtml(r.permissions)}</textarea></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>
  `);
}

async function updateRole(e, name) {
  e.preventDefault();
  try {
    await api(`/admin/roles/${name}`, { method: "PATCH", body: {
      display_name: document.getElementById("editRoleDisplay").value,
      description: document.getElementById("editRoleDesc").value,
      permissions: document.getElementById("editRolePerms").value,
    }});
    closeModal(); toast("Role updated"); loadRoles(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

function confirmDeleteRole(name) {
  showModal(`
    <div class="modal-title">Delete Role</div>
    <p>Are you sure you want to delete <strong>${escapeHtml(name)}</strong>?</p>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" onclick="deleteRole('${name}')">Delete</button>
    </div>
  `);
}

async function deleteRole(name) {
  try {
    await api(`/admin/roles/${name}`, { method: "DELETE" });
    closeModal(); toast("Role deleted"); loadRoles(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

// --- Services ---
async function loadServices(el) {
  const services = await api("/admin/services");
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header">
        <h2>Services (${services.length})</h2>
        <div class="table-actions">
          <button class="btn btn-primary btn-sm" onclick="showAddService()">+ Add Service</button>
        </div>
      </div>
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Base URL</th><th>Description</th><th>Entities</th><th>Actions</th></tr></thead>
        <tbody>
          ${services.length === 0 ? '<tr><td colspan="6" class="empty-state"><p>No services registered</p></td></tr>' : ""}
          ${services.map(s => `
            <tr>
              <td><strong>${escapeHtml(s.id)}</strong></td>
              <td>${escapeHtml(s.name)}</td>
              <td style="font-family:var(--mono);font-size:11px">${escapeHtml(s.base_url)}</td>
              <td>${escapeHtml(s.description || "")}</td>
              <td>${(s.entity_sets || []).length}</td>
              <td><button class="btn btn-danger btn-sm" onclick="confirmDeleteService('${s.id}')">Delete</button></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function showAddService() {
  showModal(`
    <div class="modal-title">Add OData Service</div>
    <form onsubmit="addService(event)">
      <div class="form-group"><label>Service ID</label><input type="text" id="newServiceId" required pattern="[a-z0-9_-]+"></div>
      <div class="form-group"><label>Name</label><input type="text" id="newServiceName" required></div>
      <div class="form-group"><label>Base URL</label><input type="url" id="newServiceUrl" required placeholder="https://services.odata.org/V4/Northwind/Northwind.svc"></div>
      <div class="form-group"><label>Description</label><input type="text" id="newServiceDesc"></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Register</button>
      </div>
    </form>
  `);
}

async function addService(e) {
  e.preventDefault();
  try {
    await api("/admin/services", { method: "POST", body: {
      id: document.getElementById("newServiceId").value,
      name: document.getElementById("newServiceName").value,
      base_url: document.getElementById("newServiceUrl").value,
      description: document.getElementById("newServiceDesc").value,
    }});
    closeModal(); toast("Service registered"); loadServices(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

function confirmDeleteService(id) {
  showModal(`
    <div class="modal-title">Delete Service</div>
    <p>Are you sure you want to delete <strong>${escapeHtml(id)}</strong>?</p>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" onclick="deleteService('${id}')">Delete</button>
    </div>
  `);
}

async function deleteService(id) {
  try {
    await api(`/admin/services/${id}`, { method: "DELETE" });
    closeModal(); toast("Service deleted"); loadServices(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

// --- Analytics ---
async function loadAnalytics(el) {
  const data = await api("/admin/analytics");
  el.innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-value">${data.total_users}</div><div class="stat-label">Total Users</div></div>
      <div class="stat-card"><div class="stat-value">${data.total_services}</div><div class="stat-label">Services</div></div>
      <div class="stat-card"><div class="stat-value">${data.total_audit_entries}</div><div class="stat-label">Audit Entries</div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div class="table-wrap">
        <div class="table-header"><h2>Actions</h2></div>
        <table>
          <thead><tr><th>Action</th><th>Count</th></tr></thead>
          <tbody>${data.action_breakdown.map(a => `<tr><td>${escapeHtml(a.action)}</td><td>${a.count}</td></tr>`).join("")}</tbody>
        </table>
      </div>
      <div class="table-wrap">
        <div class="table-header"><h2>Resources</h2></div>
        <table>
          <thead><tr><th>Resource</th><th>Count</th></tr></thead>
          <tbody>${data.resource_breakdown.map(r => `<tr><td>${escapeHtml(r.resource)}</td><td>${r.count}</td></tr>`).join("")}</tbody>
        </table>
      </div>
    </div>
    <div class="table-wrap" style="margin-top:16px">
      <div class="table-header"><h2>Status Breakdown</h2></div>
      <table>
        <thead><tr><th>Status</th><th>Count</th></tr></thead>
        <tbody>${data.status_breakdown.map(s => `<tr><td><span class="badge badge-${s.status === "success" ? "active" : "locked"}">${escapeHtml(s.status)}</span></td><td>${s.count}</td></tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

// --- Settings ---
async function loadSettings(el) {
  const settings = await api("/admin/settings");
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header"><h2>System Settings</h2></div>
      <div style="padding:20px">
        <form onsubmit="saveSettings(event)">
          <div class="form-group">
            <label>LLM Provider</label>
            <select id="settingProvider">
              ${["mock","openai","groq","gemini"].map(p => `<option value="${p}" ${settings.llm_provider === p ? "selected" : ""}>${p}</option>`).join("")}
            </select>
          </div>
          <div class="form-group">
            <label>LLM Model</label>
            <input type="text" id="settingModel" value="${escapeHtml(settings.llm_model || "")}">
          </div>
          <div class="form-group">
            <label>CORS Origins</label>
            <input type="text" id="settingCors" value="${escapeHtml(settings.cors_origins || "")}">
          </div>
          <div class="form-group">
            <label>Neo4j URI</label>
            <input type="text" id="settingNeo4j" value="${escapeHtml(settings.neo4j_uri || "")}" disabled>
          </div>
          <button type="submit" class="btn btn-primary">Save Settings</button>
        </form>
      </div>
    </div>
  `;
}

async function saveSettings(e) {
  e.preventDefault();
  try {
    await api("/admin/settings", { method: "PATCH", body: {
      llm_provider: document.getElementById("settingProvider").value,
      llm_model: document.getElementById("settingModel").value,
      cors_origins: document.getElementById("settingCors").value,
    }});
    toast("Settings saved (restart backend to apply)");
  } catch (e) { toast(e.message, "error"); }
}

// --- Audit ---
async function loadAudit(el) {
  const logs = await api("/admin/audit?limit=200");
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header"><h2>Audit Log (${logs.length})</h2></div>
      <table>
        <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Resource</th><th>Details</th><th>IP</th><th>Status</th></tr></thead>
        <tbody>
          ${logs.length === 0 ? '<tr><td colspan="7" class="empty-state"><p>No audit entries</p></td></tr>' : ""}
          ${logs.map(a => `
            <tr>
              <td>${new Date(a.created_at).toLocaleString()}</td>
              <td>${escapeHtml(a.username || "—")}</td>
              <td>${escapeHtml(a.action)}</td>
              <td>${escapeHtml(a.resource)}</td>
              <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(a.details)}</td>
              <td>${escapeHtml(a.ip_address)}</td>
              <td><span class="badge badge-${a.status === "success" ? "active" : "locked"}">${escapeHtml(a.status)}</span></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

// --- Init ---
async function init() {
  const stored = localStorage.getItem("theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);

  document.getElementById("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("loginBtn");
    const errEl = document.getElementById("loginError");
    btn.disabled = true; btn.textContent = "Signing in...";
    errEl.classList.add("hidden");
    try {
      await login(
        document.getElementById("loginUsername").value,
        document.getElementById("loginPassword").value
      );
    } catch (e) {
      errEl.textContent = e.message;
      errEl.classList.remove("hidden");
    } finally {
      btn.disabled = false; btn.textContent = "Sign In";
    }
  });

  if (await checkAuth()) showAdmin();
}

function showAdmin() {
  document.getElementById("loginScreen").classList.add("hidden");
  document.getElementById("adminShell").classList.remove("hidden");
  document.getElementById("userInfo").textContent = `${currentUser.username} (${currentUser.role})`;
  navigateTo("dashboard");
}

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("open");
}

init();
