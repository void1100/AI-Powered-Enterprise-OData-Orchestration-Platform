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
  const allowed = ROLE_PERMISSIONS[currentUser?.role] || ["dashboard"];
  if (!allowed.includes(page)) {
    page = allowed[0] || "dashboard";
  }
  currentPage = page;
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  document.querySelector(`[data-page="${page}"]`)?.classList.add("active");
  document.getElementById("pageTitle").textContent = {
    dashboard: "Dashboard",
    users: "User Management",
    roles: "Role Management",
    services: "Service Management",
    custom_entities: "Custom Entities",
    joins: "Join Services",
    analytics: "Analytics",
    usage: "Usage",
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
    else if (page === "custom_entities") await loadCustomEntities(content);
    else if (page === "joins") await loadJoins(content);
    else if (page === "analytics") await loadAnalytics(content);
    else if (page === "usage") await loadUsage(content);
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
      <div class="form-group"><label>Authentication</label>
        <select id="newServiceAuthType" onchange="toggleAuthFields()">
          <option value="">No Authentication</option>
          <option value="basic">Basic Auth (Username/Password)</option>
          <option value="bearer">Bearer Token</option>
          <option value="api_key">API Key</option>
        </select>
      </div>
      <div id="authFieldsContainer" style="display:none">
        <div class="form-group" id="authUserGroup" style="display:none"><label>Username</label><input type="text" id="newServiceAuthUser"></div>
        <div class="form-group" id="authPassGroup" style="display:none"><label>Password</label><input type="password" id="newServiceAuthPass"></div>
        <div class="form-group" id="authTokenGroup" style="display:none"><label>Bearer Token</label><input type="text" id="newServiceAuthToken"></div>
        <div class="form-group" id="authApiKeyGroup" style="display:none"><label>API Key</label><input type="text" id="newServiceAuthApiKey"></div>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Register</button>
      </div>
    </form>
  `);
}

function toggleAuthFields() {
  const t = document.getElementById("newServiceAuthType").value;
  const container = document.getElementById("authFieldsContainer");
  container.style.display = t ? "block" : "none";
  document.getElementById("authUserGroup").style.display = t === "basic" ? "block" : "none";
  document.getElementById("authPassGroup").style.display = t === "basic" ? "block" : "none";
  document.getElementById("authTokenGroup").style.display = t === "bearer" ? "block" : "none";
  document.getElementById("authApiKeyGroup").style.display = t === "api_key" ? "block" : "none";
}

async function addService(e) {
  e.preventDefault();
  const authType = document.getElementById("newServiceAuthType").value;
  const body = {
    id: document.getElementById("newServiceId").value,
    name: document.getElementById("newServiceName").value,
    base_url: document.getElementById("newServiceUrl").value,
    description: document.getElementById("newServiceDesc").value,
    auth_type: authType || null,
  };
  if (authType === "basic") {
    body.auth_username = document.getElementById("newServiceAuthUser").value;
    body.auth_password = document.getElementById("newServiceAuthPass").value;
  } else if (authType === "bearer") {
    body.auth_token = document.getElementById("newServiceAuthToken").value;
  } else if (authType === "api_key") {
    body.auth_api_key = document.getElementById("newServiceAuthApiKey").value;
  }
  try {
    await api("/admin/services", { method: "POST", body });
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

// --- Custom Entities ---
async function loadCustomEntities(el) {
  const [entities, services] = await Promise.all([
    api("/custom_entities"),
    api("/services"),
  ]);
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header">
        <h2>Custom Entities (${entities.length})</h2>
        <div class="table-actions">
          <button class="btn btn-primary btn-sm" onclick="showCreateCustomEntity()">+ Create Entity</button>
        </div>
      </div>
      ${entities.length === 0 ? '<div class="empty-state"><p>No custom entities yet. Click "+ Create Entity" to start.</p></div>' : `
      <table>
        <thead><tr><th>Name</th><th>Service</th><th>Base Entity</th><th>Description</th><th>Filter</th><th>Columns</th><th>Created By</th><th>Actions</th></tr></thead>
        <tbody>
          ${entities.map(e => `
            <tr>
              <td><strong>${escapeHtml(e.name)}</strong></td>
              <td>${escapeHtml(e.service_id)}</td>
              <td>${escapeHtml(e.base_entity_set)}</td>
              <td>${escapeHtml(e.description || "—")}</td>
              <td><code>${escapeHtml(e.default_filter || "none")}</code></td>
              <td>${(e.allowed_columns || []).length > 0 ? e.allowed_columns.map(c => `<span class="badge badge-active">${escapeHtml(c)}</span>`).join(" ") : "<em>all</em>"}</td>
              <td>${escapeHtml(e.created_by || "—")}</td>
              <td>
                <button class="btn btn-ghost btn-sm" onclick="testCustomEntity('${escapeHtml(e.service_id)}','${escapeHtml(e.name)}')">Test</button>
                <button class="btn btn-ghost btn-sm" onclick="editCustomEntity('${escapeHtml(e.service_id)}','${escapeHtml(e.name)}')">Edit</button>
                <button class="btn btn-danger btn-sm" onclick="deleteCustomEntity('${escapeHtml(e.service_id)}','${escapeHtml(e.name)}')">Delete</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>`}
    </div>
    <div class="table-wrap" style="margin-top:24px">
      <div class="table-header"><h2>Quick Create (Chatbox)</h2></div>
      <div style="padding:16px">
        <div style="display:flex;gap:8px">
          <input type="text" id="customEntityChat" placeholder='Describe your custom entity, e.g. "Create VIP_Customers from Customers where Country is USA"' style="flex:1;padding:8px 12px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text)">
          <button class="btn btn-primary" onclick="chatCreateCustomEntity()">Create</button>
        </div>
        <div id="customEntityChatResult" style="margin-top:8px"></div>
      </div>
    </div>
  `;
  window._customEntityServices = services;
}

function showCreateCustomEntity() {
  const services = window._customEntityServices || [];
  showModal(`
    <h2 style="margin:0 0 16px">Create Custom Entity</h2>
    <div class="form-group">
      <label>Service</label>
      <select id="ceService" class="form-input" onchange="loadBaseEntities(this.value)">
        ${services.map(s => `<option value="${s.id}">${escapeHtml(s.name)} (${s.id})</option>`).join("")}
      </select>
    </div>
    <div class="form-group">
      <label>Base Entity Set</label>
      <select id="ceBaseEntity" class="form-input"><option>Loading...</option></select>
    </div>
    <div class="form-group">
      <label>Custom Name</label>
      <input type="text" id="ceName" class="form-input" placeholder="e.g. VIP_Customers">
    </div>
    <div class="form-group">
      <label>Description</label>
      <input type="text" id="ceDesc" class="form-input" placeholder="e.g. VIP Customers from USA only">
    </div>
    <div class="form-group">
      <label>Default Filter (OData syntax)</label>
      <input type="text" id="ceFilter" class="form-input" placeholder="e.g. Country eq 'USA'">
    </div>
    <div class="form-group">
      <label>Allowed Columns (comma-separated, blank = all)</label>
      <input type="text" id="ceColumns" class="form-input" placeholder="e.g. CustomerID, CompanyName, City">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitCreateCustomEntity()">Create</button>
    </div>
  `);
  if (services.length > 0) loadBaseEntities(services[0].id);
}

async function loadBaseEntities(serviceId) {
  const sel = document.getElementById("ceBaseEntity");
  const services = window._customEntityServices || [];
  const svc = services.find(s => s.id === serviceId);
  sel.innerHTML = (svc?.entity_sets || []).map(es => `<option value="${es}">${es}</option>`).join("");
}

async function submitCreateCustomEntity() {
  const serviceId = document.getElementById("ceService").value;
  const name = document.getElementById("ceName").value.trim();
  const baseEntity = document.getElementById("ceBaseEntity").value;
  const desc = document.getElementById("ceDesc").value.trim();
  const filter = document.getElementById("ceFilter").value.trim();
  const cols = document.getElementById("ceColumns").value.split(",").map(s => s.trim()).filter(Boolean);
  if (!name) return toast("Name is required", "error");
  if (!baseEntity) return toast("Base entity is required", "error");
  try {
    await api(`/custom_entities/${serviceId}`, {
      method: "POST",
      body: { name, base_entity_set: baseEntity, description: desc, default_filter: filter, allowed_columns: cols },
    });
    closeModal(); toast("Custom entity created");
    loadCustomEntities(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

async function chatCreateCustomEntity() {
  const input = document.getElementById("customEntityChat");
  const resultDiv = document.getElementById("customEntityChatResult");
  const text = input.value.trim();
  if (!text) return;
  resultDiv.innerHTML = '<span style="color:var(--text-muted)">Processing...</span>';
  try {
    const services = window._customEntityServices || [];
    const firstService = services[0];
    if (!firstService) return resultDiv.innerHTML = '<span style="color:var(--danger)">No services registered</span>';
    const nameMatch = text.match(/(?:called?|named?|name\s+is?)\s+(\w+)/i);
    const filterMatch = text.match(/where\s+(.+?)(?:\s+and\s+|\s*$)/i);
    const baseMatch = text.match(/from\s+(\w+)/i);
    const colsMatch = text.match(/columns?\s+(.+?)(?:\s+and\s+|\s*$)/i);
    const name = nameMatch ? nameMatch[1] : "Custom_" + (baseMatch ? baseMatch[1] : "Entity");
    const baseEntity = baseMatch ? baseMatch[1] : firstService.entity_sets[0] || "Entities";
    const filter = filterMatch ? filterMatch[1] : "";
    const cols = colsMatch ? colsMatch[1].split(/[,\s]+and\s+/).map(s => s.trim()).filter(Boolean) : [];
    await api(`/custom_entities/${firstService.id}`, {
      method: "POST",
      body: { name, base_entity_set: baseEntity, description: text, default_filter: filter, allowed_columns: cols },
    });
    resultDiv.innerHTML = `<span style="color:var(--success)">Created "<strong>${escapeHtml(name)}</strong>" from ${escapeHtml(baseEntity)}</span>`;
    input.value = "";
    loadCustomEntities(document.getElementById("pageContent"));
  } catch (e) {
    resultDiv.innerHTML = `<span style="color:var(--danger)">Error: ${escapeHtml(e.message)}</span>`;
  }
}

async function editCustomEntity(serviceId, name) {
  const entity = (await api("/custom_entities")).find(e => e.service_id === serviceId && e.name === name);
  if (!entity) return toast("Entity not found", "error");
  showModal(`
    <h2 style="margin:0 0 16px">Edit: ${escapeHtml(name)}</h2>
    <div class="form-group">
      <label>Description</label>
      <input type="text" id="editCeDesc" class="form-input" value="${escapeHtml(entity.description || "")}">
    </div>
    <div class="form-group">
      <label>Default Filter</label>
      <input type="text" id="editCeFilter" class="form-input" value="${escapeHtml(entity.default_filter || "")}">
    </div>
    <div class="form-group">
      <label>Allowed Columns (comma-separated)</label>
      <input type="text" id="editCeColumns" class="form-input" value="${(entity.allowed_columns || []).join(", ")}">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitEditCustomEntity('${escapeHtml(serviceId)}','${escapeHtml(name)}')">Save</button>
    </div>
  `);
}

async function submitEditCustomEntity(serviceId, name) {
  const desc = document.getElementById("editCeDesc").value.trim();
  const filter = document.getElementById("editCeFilter").value.trim();
  const cols = document.getElementById("editCeColumns").value.split(",").map(s => s.trim()).filter(Boolean);
  try {
    await api(`/custom_entities/${serviceId}/${name}`, {
      method: "PATCH",
      body: { description: desc, default_filter: filter, allowed_columns: cols },
    });
    closeModal(); toast("Updated");
    loadCustomEntities(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

async function deleteCustomEntity(serviceId, name) {
  if (!confirm(`Delete custom entity "${name}"?`)) return;
  try {
    await api(`/custom_entities/${serviceId}/${name}`, { method: "DELETE" });
    toast("Deleted");
    loadCustomEntities(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

async function testCustomEntity(serviceId, name) {
  showModal(`
    <h2 style="margin:0 0 16px">Test: ${escapeHtml(name)}</h2>
    <div id="ceTestResult"><span style="color:var(--text-muted)">Running test query...</span></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
    </div>
  `);
  try {
    const result = await api("/chat", { method: "POST", body: { query: `Show top 5 from ${name}`, role: "admin" } });
    const resultDiv = document.getElementById("ceTestResult");
    if (result.table && result.table.rows && result.table.rows.length > 0) {
      resultDiv.innerHTML = `
        <p style="color:var(--success);margin:0 0 8px">Test passed! ${result.table.rows.length} rows returned.</p>
        <div style="max-height:300px;overflow:auto">
          <table style="width:100%;font-size:12px">
            <thead><tr>${(result.table.columns || []).map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
            <tbody>${result.table.rows.slice(0, 5).map(r => `<tr>${(Array.isArray(r) ? r : Object.values(r)).map(v => `<td>${escapeHtml(String(v ?? ""))}</td>`).join("")}</tr>`).join("")}</tbody>
          </table>
        </div>`;
    } else {
      resultDiv.innerHTML = `<p style="color:var(--danger)">No data returned. ${result.error ? escapeHtml(result.error) : ""}</p>`;
    }
  } catch (e) {
    document.getElementById("ceTestResult").innerHTML = `<p style="color:var(--danger)">Error: ${escapeHtml(e.message)}</p>`;
  }
}

// --- Join Services ---
async function loadJoins(el) {
  const [joins, services] = await Promise.all([
    api("/joins"),
    api("/services"),
  ]);
  window._joinServices = services;
  window._joinList = joins;
  el.innerHTML = `
    <div class="table-wrap">
      <div class="table-header">
        <h2>Cross-Service Joins (${joins.length})</h2>
        <div class="table-actions">
          <button class="btn btn-primary btn-sm" onclick="showCreateJoin()">+ Create Join</button>
        </div>
      </div>
      ${joins.length === 0 ? '<div class="empty-state"><p>No joins defined yet. Click "+ Create Join" to combine entities from different services.</p></div>' : `
      <table>
        <thead><tr><th>Name</th><th>Strategy</th><th>Left</th><th>Right</th><th>Description</th><th>Created</th><th>Actions</th></tr></thead>
        <tbody>
          ${joins.map(j => `
            <tr>
              <td><strong>${escapeHtml(j.name)}</strong></td>
              <td><span class="badge badge-active">${escapeHtml(j.strategy)}</span></td>
              <td>${escapeHtml(j.left_service)}.${escapeHtml(j.left_entity)}</td>
              <td>${escapeHtml(j.right_service)}.${escapeHtml(j.right_entity)}</td>
              <td>${escapeHtml(j.description || "—")}</td>
              <td>${j.created_at ? new Date(j.created_at).toLocaleDateString() : "—"}</td>
              <td>
                <button class="btn btn-primary btn-sm" onclick="executeJoin('${j.id}')">Execute</button>
                <button class="btn btn-ghost btn-sm" onclick="showEditJoin('${j.id}')">Edit</button>
                <button class="btn btn-danger btn-sm" onclick="deleteJoin('${j.id}')">Delete</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>`}
    </div>
    <div id="joinResult" style="margin-top:24px"></div>
  `;
}

function showCreateJoin() {
  const services = window._joinServices || [];
  const svcOpts = services.map(s => `<option value="${s.id}">${escapeHtml(s.name)} (${s.id}) — ${s.entity_sets.length} entities</option>`).join("");
  showModal(`
    <h2 style="margin:0 0 16px">Create Cross-Service Join</h2>
    <div class="form-group">
      <label>Join Name</label>
      <input type="text" id="joinName" class="form-input" placeholder="e.g. Products_Comparison">
    </div>
    <div class="form-group">
      <label>Strategy</label>
      <select id="joinStrategy" class="form-input" onchange="updateJoinForm()">
        <option value="union">Union (Stack rows from both services)</option>
        <option value="match">Match (Join by common key)</option>
        <option value="enrichment">Enrichment (Primary + Secondary lookup)</option>
      </select>
    </div>
    <div class="form-group">
      <label>Left Service</label>
      <select id="joinLeftSvc" class="form-input" onchange="updateJoinEntities('left')">${svcOpts}</select>
    </div>
    <div class="form-group">
      <label>Left Entity</label>
      <select id="joinLeftEntity" class="form-input"><option>Loading...</option></select>
    </div>
    <div class="form-group" id="joinLeftKeyGroup" style="display:none">
      <label>Left Join Key</label>
      <input type="text" id="joinLeftKey" class="form-input" placeholder="e.g. ProductID">
    </div>
    <div class="form-group">
      <label>Right Service</label>
      <select id="joinRightSvc" class="form-input" onchange="updateJoinEntities('right')">${svcOpts}</select>
    </div>
    <div class="form-group">
      <label>Right Entity</label>
      <select id="joinRightEntity" class="form-input"><option>Loading...</option></select>
    </div>
    <div class="form-group" id="joinRightKeyGroup" style="display:none">
      <label>Right Join Key</label>
      <input type="text" id="joinRightKey" class="form-input" placeholder="e.g. ProductID">
    </div>
    <div class="form-group">
      <label>Description</label>
      <input type="text" id="joinDesc" class="form-input" placeholder="e.g. Compare products across services">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitCreateJoin()">Create</button>
    </div>
  `);
  updateJoinEntities("left");
  updateJoinEntities("right");
}

function updateJoinForm() {
  const strategy = document.getElementById("joinStrategy").value;
  document.getElementById("joinLeftKeyGroup").style.display = (strategy === "match" || strategy === "enrichment") ? "" : "none";
  document.getElementById("joinRightKeyGroup").style.display = (strategy === "match" || strategy === "enrichment") ? "" : "none";
}

function updateJoinEntities(side) {
  const svcSel = document.getElementById(`join${side === "left" ? "Left" : "Right"}Svc`);
  const entSel = document.getElementById(`join${side === "left" ? "Left" : "Right"}Entity`);
  const services = window._joinServices || [];
  const svc = services.find(s => s.id === svcSel.value);
  entSel.innerHTML = (svc?.entity_sets || []).map(es => `<option value="${es}">${es}</option>`).join("");
}

async function submitCreateJoin() {
  const name = document.getElementById("joinName").value.trim();
  const strategy = document.getElementById("joinStrategy").value;
  const leftSvc = document.getElementById("joinLeftSvc").value;
  const leftEntity = document.getElementById("joinLeftEntity").value;
  const leftKey = document.getElementById("joinLeftKey").value.trim();
  const rightSvc = document.getElementById("joinRightSvc").value;
  const rightEntity = document.getElementById("joinRightEntity").value;
  const rightKey = document.getElementById("joinRightKey").value.trim();
  const desc = document.getElementById("joinDesc").value.trim();
  if (!name) return toast("Name is required", "error");
  if (!leftEntity || !rightEntity) return toast("Both entities required", "error");
  if ((strategy === "match" || strategy === "enrichment") && (!leftKey || !rightKey)) return toast("Join keys required for this strategy", "error");
  try {
    await api("/joins", {
      method: "POST",
      body: { name, strategy, left_service: leftSvc, left_entity: leftEntity, left_key: leftKey, right_service: rightSvc, right_entity: rightEntity, right_key: rightKey, description: desc },
    });
    closeModal(); toast("Join created");
    loadJoins(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

function showEditJoin(joinId) {
  const joins = window._joinList || [];
  const j = joins.find(x => x.id === joinId);
  if (!j) return toast("Join not found", "error");
  const services = window._joinServices || [];
  const svcOpts = services.map(s => `<option value="${s.id}" ${s.id === j.left_service || s.id === j.right_service ? "" : ""}>${escapeHtml(s.name)} (${s.id}) — ${s.entity_sets.length} entities</option>`).join("");
  const leftSvcOpts = services.map(s => `<option value="${s.id}" ${s.id === j.left_service ? "selected" : ""}>${escapeHtml(s.name)} (${s.id})</option>`).join("");
  const rightSvcOpts = services.map(s => `<option value="${s.id}" ${s.id === j.right_service ? "selected" : ""}>${escapeHtml(s.name)} (${s.id})</option>`).join("");
  const leftSvc = services.find(s => s.id === j.left_service);
  const rightSvc = services.find(s => s.id === j.right_service);
  const leftEntOpts = (leftSvc?.entity_sets || []).map(es => `<option value="${es}" ${es === j.left_entity ? "selected" : ""}>${es}</option>`).join("");
  const rightEntOpts = (rightSvc?.entity_sets || []).map(es => `<option value="${es}" ${es === j.right_entity ? "selected" : ""}>${es}</option>`).join("");
  const showKey = j.strategy === "match" || j.strategy === "enrichment";
  showModal(`
    <h2 style="margin:0 0 16px">Edit Join: ${escapeHtml(j.name)}</h2>
    <div class="form-group">
      <label>Join Name</label>
      <input type="text" id="editJoinName" class="form-input" value="${escapeHtml(j.name)}">
    </div>
    <div class="form-group">
      <label>Strategy</label>
      <select id="editJoinStrategy" class="form-input" onchange="updateEditJoinForm()">
        <option value="union" ${j.strategy === "union" ? "selected" : ""}>Union (Stack rows from both services)</option>
        <option value="match" ${j.strategy === "match" ? "selected" : ""}>Match (Join by common key)</option>
        <option value="enrichment" ${j.strategy === "enrichment" ? "selected" : ""}>Enrichment (Primary + Secondary lookup)</option>
      </select>
    </div>
    <div class="form-group">
      <label>Left Service</label>
      <select id="editJoinLeftSvc" class="form-input" onchange="updateEditJoinEntities('left')">${leftSvcOpts}</select>
    </div>
    <div class="form-group">
      <label>Left Entity</label>
      <select id="editJoinLeftEntity" class="form-input">${leftEntOpts}</select>
    </div>
    <div class="form-group" id="editJoinLeftKeyGroup" style="${showKey ? "" : "display:none"}">
      <label>Left Join Key</label>
      <input type="text" id="editJoinLeftKey" class="form-input" value="${escapeHtml(j.left_key || "")}">
    </div>
    <div class="form-group">
      <label>Right Service</label>
      <select id="editJoinRightSvc" class="form-input" onchange="updateEditJoinEntities('right')">${rightSvcOpts}</select>
    </div>
    <div class="form-group">
      <label>Right Entity</label>
      <select id="editJoinRightEntity" class="form-input">${rightEntOpts}</select>
    </div>
    <div class="form-group" id="editJoinRightKeyGroup" style="${showKey ? "" : "display:none"}">
      <label>Right Join Key</label>
      <input type="text" id="editJoinRightKey" class="form-input" value="${escapeHtml(j.right_key || "")}">
    </div>
    <div class="form-group">
      <label>Description</label>
      <input type="text" id="editJoinDesc" class="form-input" value="${escapeHtml(j.description || "")}">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitEditJoin('${j.id}')">Save Changes</button>
    </div>
  `);
  window._editingJoinId = j.id;
}

function updateEditJoinForm() {
  const strategy = document.getElementById("editJoinStrategy").value;
  document.getElementById("editJoinLeftKeyGroup").style.display = (strategy === "match" || strategy === "enrichment") ? "" : "none";
  document.getElementById("editJoinRightKeyGroup").style.display = (strategy === "match" || strategy === "enrichment") ? "" : "none";
}

function updateEditJoinEntities(side) {
  const svcSel = document.getElementById(`editJoin${side === "left" ? "Left" : "Right"}Svc`);
  const entSel = document.getElementById(`editJoin${side === "left" ? "Left" : "Right"}Entity`);
  const services = window._joinServices || [];
  const svc = services.find(s => s.id === svcSel.value);
  const currentVal = entSel.value;
  entSel.innerHTML = (svc?.entity_sets || []).map(es => `<option value="${es}" ${es === currentVal ? "selected" : ""}>${es}</option>`).join("");
}

async function submitEditJoin(joinId) {
  const name = document.getElementById("editJoinName").value.trim();
  const strategy = document.getElementById("editJoinStrategy").value;
  const leftSvc = document.getElementById("editJoinLeftSvc").value;
  const leftEntity = document.getElementById("editJoinLeftEntity").value;
  const leftKey = document.getElementById("editJoinLeftKey").value.trim();
  const rightSvc = document.getElementById("editJoinRightSvc").value;
  const rightEntity = document.getElementById("editJoinRightEntity").value;
  const rightKey = document.getElementById("editJoinRightKey").value.trim();
  const desc = document.getElementById("editJoinDesc").value.trim();
  if (!name) return toast("Name is required", "error");
  if (!leftEntity || !rightEntity) return toast("Both entities required", "error");
  try {
    await api(`/joins/${joinId}`, {
      method: "PATCH",
      body: { name, strategy, left_service: leftSvc, left_entity: leftEntity, left_key: leftKey, right_service: rightSvc, right_entity: rightEntity, right_key: rightKey, description: desc },
    });
    closeModal(); toast("Join updated");
    loadJoins(document.getElementById("pageContent"));
  } catch (e) { toast(e.message, "error"); }
}

async function executeJoin(joinId) {
  const resultDiv = document.getElementById("joinResult");
  resultDiv.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-muted)">Loading...</div>';
  try {
    const data = await api(`/joins/${joinId}/execute`, { method: "POST" });
    const r = data.result;
    window._currentJoinId = joinId;
    window._currentJoinData = data;
    const hideCols = ["@odata.id", "@odata.etag", "@odata.editLink", "Emails", "AddressInfo", "Concurrency", "Photo", "Notes", "PhotoPath"];
    const displayCols = r.columns.filter(c => !hideCols.includes(c));
    let html = '';
    html += '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;margin-bottom:16px">';
    html += '<div style="padding:12px 16px;border-bottom:1px solid var(--border)"><strong>' + escapeHtml(data.join.name) + ' &mdash; ' + escapeHtml(r.strategy) + ' &mdash; ' + r.row_count + ' rows</strong></div>';
    html += '<div style="overflow-x:auto">';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;line-height:1">';
    html += '<thead><tr>';
    displayCols.forEach(function(c) {
      html += '<th style="padding:4px 6px;text-align:left;background:var(--bg-elev);border-bottom:1px solid var(--border);font-size:11px;font-weight:600;color:var(--text-muted);white-space:nowrap">' + escapeHtml(c) + '</th>';
    });
    html += '</tr></thead><tbody>';
    r.rows.slice(0, 50).forEach(function(row) {
      html += '<tr>';
      displayCols.forEach(function(c) {
        var val = String(row[c] ?? '');
        if (val.length > 30) val = val.substring(0, 30) + '...';
        html += '<td style="padding:3px 6px;border-bottom:1px solid var(--border);white-space:nowrap;font-size:12px">' + escapeHtml(val) + '</td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    if (r.row_count > 50) html += '<div style="padding:4px 8px;font-size:11px;color:var(--text-muted)">Showing 50 of ' + r.row_count + ' rows</div>';
    html += '</div>';
    html += '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px">';
    html += '<div style="padding:8px 16px;border-bottom:1px solid var(--border);background:var(--bg-elev)"><strong style="font-size:12px">Ask about this data</strong></div>';
    html += '<div id="joinChatMessages" style="height:180px;overflow-y:auto;padding:10px"></div>';
    html += '<div style="padding:6px 10px;border-top:1px solid var(--border);display:flex;gap:6px;background:var(--bg-elev)">';
    html += '<input type="text" id="joinChatInput" placeholder="Ask about this join..." style="flex:1;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;background:var(--bg-input);color:var(--text)" />';
    html += '<button class="btn btn-primary btn-sm" onclick="sendJoinChat()" style="padding:6px 12px;font-size:11px">Ask</button>';
    html += '</div></div>';
    resultDiv.innerHTML = html;
    document.getElementById("joinChatInput").addEventListener("keydown", function(e) { if (e.key === "Enter") sendJoinChat(); });
    document.getElementById("joinChatInput").focus();
  } catch (e) {
    resultDiv.innerHTML = '<div style="padding:16px;color:var(--danger)">Error: ' + escapeHtml(e.message) + '</div>';
  }
}

async function sendJoinChat() {
  const input = document.getElementById("joinChatInput");
  const messagesEl = document.getElementById("joinChatMessages");
  const query = input.value.trim();
  if (!query || !window._currentJoinId) return;
  input.value = "";
  const userDiv = document.createElement("div");
  userDiv.style.cssText = "align-self:flex-end;background:var(--accent);color:white;padding:8px 12px;border-radius:12px 12px 2px 12px;font-size:12px;max-width:75%;word-wrap:break-word";
  userDiv.textContent = query;
  messagesEl.appendChild(userDiv);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  const loadingDiv = document.createElement("div");
  loadingDiv.style.cssText = "align-self:flex-start;padding:8px 12px;font-size:12px;color:var(--text-muted);font-style:italic";
  loadingDiv.textContent = "Thinking...";
  messagesEl.appendChild(loadingDiv);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  try {
    const resp = await api(`/joins/${window._currentJoinId}/chat`, {
      method: "POST",
      body: { query },
    });
    loadingDiv.remove();
    const ansDiv = document.createElement("div");
    ansDiv.style.cssText = "align-self:flex-start;background:var(--bg-elev-2);padding:10px 14px;border-radius:12px 12px 12px 2px;font-size:12px;max-width:80%;border:1px solid var(--border);line-height:1.5";
    const formatted = escapeHtml(resp.answer).replace(/\n/g, "<br>").replace(/\|/g, "<span style='color:var(--text-muted)'>|</span>");
    ansDiv.innerHTML = `<div style="margin-bottom:6px;font-size:10px;color:var(--accent);font-weight:600;text-transform:uppercase">${escapeHtml(resp.provider)}</div><div>${formatted}</div>`;
    if (resp.table && resp.table.rows && resp.table.rows.length > 0) {
      const tWrap = document.createElement("div");
      tWrap.style.cssText = "overflow-x:auto;margin-top:10px;max-height:400px;overflow-y:auto";
      let thtml = '<table style="width:100%;border-collapse:collapse;font-size:11px"><thead><tr>';
      for (const c of resp.table.columns) {
        thtml += `<th style="padding:4px 8px;border-bottom:2px solid var(--border);text-align:left;position:sticky;top:0;background:var(--bg-elev-2)">${escapeHtml(c)}</th>`;
      }
      thtml += '</tr></thead><tbody>';
      for (const r of resp.table.rows) {
        thtml += '<tr>';
        for (const c of resp.table.columns) {
          const v = r[c] != null ? String(r[c]) : "";
          thtml += `<td style="padding:3px 8px;border-bottom:1px solid var(--border);white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(v)}">${escapeHtml(v)}</td>`;
        }
        thtml += '</tr>';
      }
      thtml += '</tbody></table>';
      tWrap.innerHTML = thtml;
      ansDiv.appendChild(tWrap);
      const note = document.createElement("div");
      note.style.cssText = "margin-top:6px;font-size:10px;color:var(--text-muted)";
      note.textContent = `Showing ${resp.table.rows.length} of ${resp.table.total_count || resp.table.row_count} rows`;
      ansDiv.appendChild(note);
    }
    messagesEl.appendChild(ansDiv);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } catch (e) {
    loadingDiv.remove();
    const errDiv = document.createElement("div");
    errDiv.style.cssText = "align-self:flex-start;padding:8px 12px;font-size:12px;color:var(--danger);background:#fef2f2;border-radius:8px;border:1px solid #fecaca";
    errDiv.textContent = "Error: " + e.message;
    messagesEl.appendChild(errDiv);
  }
}

async function deleteJoin(joinId) {
  if (!confirm("Delete this join?")) return;
  try {
    await api(`/joins/${joinId}`, { method: "DELETE" });
    toast("Join deleted");
    loadJoins(document.getElementById("pageContent"));
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

// --- Usage ---
async function loadUsage(el) {
  try {
    const data = await api("/admin/usage");
    const today = data.today || {};
    const byProvider = data.by_provider || [];
    const recentQueries = data.recent_queries || [];
    const dailyAvg = data.daily_average || {};
    const thisWeek = data.this_week || [];
    const rateLimits = data.rate_limits || [];

    const totalTokensToday = today.total_tokens || 0;
    const totalQueriesToday = today.total_queries || 0;
    const avgDaily = dailyAvg.avg_daily_tokens || 0;

    // Build rate limit cards
    const rateLimitCards = rateLimits.map(r => {
      const statusColor = r.status === "ok" ? "green" : r.status === "warning" ? "yellow" : "red";
      return `
        <div class="usage-card usage-card-rate">
          <div class="usage-card-label">${escapeHtml(r.label)}</div>
          <div class="usage-card-value">${r.usage_percent}%</div>
          <div class="usage-card-bar">
            <div class="usage-card-bar-fill" style="width:${r.usage_percent}%;background:var(--${statusColor})"></div>
          </div>
          <div class="usage-card-sub">${formatNum(r.used_today)} / ${formatNum(r.daily_limit)} tokens</div>
          <div class="usage-card-sub">${r.rpm_limit} RPM limit</div>
        </div>`;
    }).join("");

    // Build bar chart for providers
    const maxTokens = Math.max(...byProvider.map(p => p.total_tokens), 1);
    const providerBars = byProvider.map(p => {
      const pct = Math.round((p.total_tokens / maxTokens) * 100);
      const cost = p.estimated_cost !== undefined ? `$${p.estimated_cost.toFixed(4)}` : "";
      return `
        <div class="usage-bar-row">
          <span class="usage-bar-label">${escapeHtml(p.label || p.provider)}</span>
          <div class="usage-bar-track">
            <div class="usage-bar-fill" style="width:${pct}%"></div>
          </div>
          <span class="usage-bar-value">${formatNum(p.total_tokens)} tokens</span>
          <span class="usage-bar-cost">${cost}</span>
        </div>`;
    }).join("");

    // Build daily chart (last 7 days)
    const weekMax = Math.max(...thisWeek.map(d => d.tokens), 1);
    const dailyBars = thisWeek.map(d => {
      const pct = Math.round((d.tokens / weekMax) * 100);
      const date = d.date.slice(5);
      return `
        <div class="usage-daily-col">
          <div class="usage-daily-bar-wrap">
            <div class="usage-daily-bar" style="height:${Math.max(pct, 2)}%"></div>
          </div>
          <span class="usage-daily-date">${date}</span>
          <span class="usage-daily-tokens">${formatNum(d.tokens)}</span>
        </div>`;
    }).join("");

    // Recent queries table
    const rows = recentQueries.map(q => {
      const ts = q.timestamp ? new Date(q.timestamp).toLocaleString() : "";
      const truncQuery = (q.user_query || "").length > 60 ? (q.user_query || "").slice(0, 60) + "..." : (q.user_query || "");
      const cached = q.cached ? '<span class="badge badge-green">Cached</span>' : "";
      return `
        <tr>
          <td class="mono">${ts}</td>
          <td>${escapeHtml(truncQuery)}</td>
          <td><span class="badge badge-blue">${escapeHtml(q.provider || "")}</span></td>
          <td class="mono">${formatNum(q.tokens)}</td>
          <td class="mono">${q.latency_ms || 0}ms</td>
          <td>${cached}</td>
        </tr>`;
    }).join("");

    el.innerHTML = `
      <div class="usage-page">
        <div class="usage-summary-cards">
          <div class="usage-card">
            <div class="usage-card-label">Today</div>
            <div class="usage-card-value">${formatNum(totalTokensToday)}</div>
            <div class="usage-card-sub">${totalQueriesToday} queries</div>
          </div>
          <div class="usage-card">
            <div class="usage-card-label">7-Day Average</div>
            <div class="usage-card-value">${formatNum(avgDaily)}</div>
            <div class="usage-card-sub">tokens/day</div>
          </div>
          <div class="usage-card">
            <div class="usage-card-label">This Week</div>
            <div class="usage-card-value">${formatNum(thisWeek.reduce((s, d) => s + d.tokens, 0))}</div>
            <div class="usage-card-sub">${thisWeek.reduce((s, d) => s + d.queries, 0)} queries</div>
          </div>
        </div>

        <div class="usage-section">
          <h3>Provider Rate Limits (Today)</h3>
          <div class="usage-summary-cards">${rateLimitCards || '<div class="empty-state">No data yet</div>'}</div>
        </div>

        <div class="usage-section">
          <h3>Usage by Provider</h3>
          <div class="usage-bars">${providerBars || '<div class="empty-state">No data yet</div>'}</div>
        </div>

        <div class="usage-section">
          <h3>Daily Usage (Last 7 Days)</h3>
          <div class="usage-daily-chart">${dailyBars || '<div class="empty-state">No data yet</div>'}</div>
        </div>

        <div class="usage-section">
          <h3>Recent Queries</h3>
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Query</th>
                  <th>Provider</th>
                  <th>Tokens</th>
                  <th>Latency</th>
                  <th>Cached</th>
                </tr>
              </thead>
              <tbody>${rows || '<tr><td colspan="6" class="empty-state">No queries yet</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><p>Error loading usage: ${escapeHtml(e.message)}</p></div>`;
  }
}

function formatNum(n) {
  if (n === 0) return "0";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return String(n);
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

const ROLE_PERMISSIONS = {
  super_admin: ["dashboard", "users", "roles", "services", "custom_entities", "joins", "analytics", "usage", "audit", "settings"],
  admin: ["dashboard", "users", "roles", "services", "custom_entities", "joins", "analytics", "usage", "audit", "settings"],
  analyst: ["dashboard", "analytics", "usage", "services"],
  user: ["dashboard"],
  viewer: ["dashboard"],
};

function showAdmin() {
  document.getElementById("loginScreen").classList.add("hidden");
  document.getElementById("adminShell").classList.remove("hidden");
  document.getElementById("userInfo").textContent = `${currentUser.username} (${currentUser.role})`;
  const allowed = ROLE_PERMISSIONS[currentUser.role] || ["dashboard"];
  document.querySelectorAll(".nav-item[data-page]").forEach(el => {
    const page = el.dataset.page;
    if (allowed.includes(page)) {
      el.style.display = "";
    } else {
      el.style.display = "none";
    }
  });
  navigateTo(allowed.includes("dashboard") ? "dashboard" : allowed[0]);
}

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("open");
}

init();
