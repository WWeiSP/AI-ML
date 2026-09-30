/* Agent Studio single-page app (no build step). */
"use strict";

const S = { token: null, user: null, meta: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
  } catch (_) { /* storage unavailable */ }
  return null;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function md(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
}

function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), bad ? 5000 : 2500);
}

async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers.Authorization = `Bearer ${S.token}`;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401 && path !== "/api/auth/login") { logout(false); }
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

async function attempt(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    toast(e.message, true);
    return undefined;
  }
}

function modal(html, onMount) {
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `<div class="modal">${html}</div>`;
  const close = () => back.remove();
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  $$("[data-close]", back).forEach((b) => b.addEventListener("click", close));
  document.body.appendChild(back);
  const first = $("input, textarea, select", back);
  if (first) first.focus();
  if (onMount) onMount(back, close);
  return close;
}

const statusPill = (s) => {
  const cls = { published: "ok", succeeded: "ok", active: "ok", failed: "bad", running: "warn", draft: "" }[s] ?? "";
  return `<span class="pill ${cls}">${esc(s)}</span>`;
};

/* ------------------------------------------------------------------ shell */
function shell(active, content) {
  const nav = [
    ["home", "#/", "🏠", "Home"],
    ["agents", "#/agents", "🤖", "Agents"],
    ["workflows", "#/workflows", "🔀", "Workflows"],
    ["catalog", "#/catalog", "🛍️", "Catalog"],
  ];
  $("#app").innerHTML = `
    <div class="shell">
      <aside class="sidebar">
        <div class="brand"><span class="logo">✦</span><span>Agent Studio</span></div>
        <nav class="nav">${nav.map(([k, h, i, l]) => `<a href="${h}" class="${k === active ? "active" : ""}">${i} <span>${l}</span></a>`).join("")}</nav>
        <div class="spacer"></div>
        <div class="userbox">
          <div class="avatar">${esc(S.user.display_name[0].toUpperCase())}</div>
          <div class="who grow"><div>${esc(S.user.display_name)}</div><div class="muted small">@${esc(S.user.username)}</div></div>
          <button class="ghost sm" id="logout" title="Sign out">⎋</button>
        </div>
      </aside>
      <main id="main">${content}</main>
    </div>`;
  $("#logout").addEventListener("click", () => logout(true));
}

function logout(callApi) {
  if (callApi && S.token) fetch("/api/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${S.token}` } });
  S.token = null; S.user = null;
  store("studio.token", null);
  renderAuth();
}

/* ------------------------------------------------------------------ auth */
function renderAuth(mode = "login") {
  $("#app").innerHTML = `
    <div class="auth"><div class="card">
      <div class="brand"><span class="logo">✦</span><span>Agent Studio</span></div>
      <h2>${mode === "login" ? "Sign in" : "Create your account"}</h2>
      <p class="muted">Build agents, publish and share them, and automate work with workflows.</p>
      <form id="authform">
        <label>Username</label><input name="username" autocomplete="username" required minlength="3">
        ${mode === "register" ? `<label>Display name</label><input name="display_name">` : ""}
        <label>Password</label><input name="password" type="password" autocomplete="${mode === "login" ? "current-password" : "new-password"}" required minlength="6">
        <div class="row" style="margin-top:16px"><button class="primary grow" type="submit">${mode === "login" ? "Sign in" : "Create account"}</button></div>
      </form>
      <p class="muted small" style="margin-top:12px">${mode === "login"
        ? `New here? <a href="#" id="switch">Create an account</a>`
        : `Already have an account? <a href="#" id="switch">Sign in</a>`}</p>
    </div></div>`;
  $("#switch").addEventListener("click", (e) => { e.preventDefault(); renderAuth(mode === "login" ? "register" : "login"); });
  $("#authform").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target));
    const res = await attempt(() => api("POST", `/api/auth/${mode}`, body));
    if (!res) return;
    S.token = res.token; S.user = res.user;
    store("studio.token", res.token);
    route();
  });
}

/* ------------------------------------------------------------------ home */
async function renderHome() {
  shell("home", `<div class="page"><div class="muted">Loading…</div></div>`);
  const st = await attempt(() => api("GET", "/api/stats"));
  if (!st) return;
  $("#main").innerHTML = `<div class="page">
    <div class="page-head"><h1 class="grow">Welcome back, ${esc(S.user.display_name)}</h1>
      <button id="newAgent" class="primary">＋ New agent</button><button id="newWf">＋ New workflow</button></div>
    ${S.meta.llm_enabled ? "" : `<div class="banner">Generative answers are off: the server has no <code>ANTHROPIC_API_KEY</code>. Agents still answer from topics and knowledge.</div>`}
    <div class="stats">
      ${[["Agents", st.agents], ["Published", st.published], ["Workflows", st.workflows], ["Active flows", st.active], ["Runs", st.runs], ["Conversations", st.conversations]]
        .map(([l, n]) => `<div class="card stat"><div class="muted small">${l}</div><div class="n">${n}</div></div>`).join("")}
    </div>
    <div class="card"><h2>Recent workflow runs</h2>
      ${st.recent_runs.length ? `<table><tr><th>Workflow</th><th>Status</th><th>Trigger</th><th>When</th></tr>
        ${st.recent_runs.map((r) => `<tr><td><a href="#/workflows/${r.workflow_id}">${esc(r.workflow)}</a></td><td>${statusPill(r.status)}</td><td>${esc(r.trigger_type)}</td><td class="muted">${ago(r.started_at)}</td></tr>`).join("")}
      </table>` : `<div class="empty">No runs yet. Build a workflow and give it a test run.</div>`}
    </div></div>`;
  $("#newAgent").addEventListener("click", newAgentDialog);
  $("#newWf").addEventListener("click", newWorkflowDialog);
}

/* ------------------------------------------------------------------ agents list */
async function renderAgents() {
  shell("agents", `<div class="page"><div class="muted">Loading…</div></div>`);
  const agents = await attempt(() => api("GET", "/api/agents"));
  if (!agents) return;
  $("#main").innerHTML = `<div class="page">
    <div class="page-head"><h1 class="grow">Agents</h1><button id="newAgent" class="primary">＋ New agent</button></div>
    ${agents.length ? `<div class="grid">${agents.map((a) => `
      <div class="card tile" data-id="${a.id}">
        <div class="row"><span class="icon">${esc(a.icon)}</span><span class="grow"></span>${statusPill(a.status)}${a.role !== "owner" ? `<span class="pill accent">${esc(a.role)}</span>` : ""}</div>
        <div class="title">${esc(a.name)}</div>
        <div class="desc">${esc(a.description || "No description")}</div>
        <div class="muted small">${a.role === "owner" ? "" : `by @${esc(a.owner.username)} · `}updated ${ago(a.updated_at)}${a.version ? ` · v${a.version}` : ""}</div>
      </div>`).join("")}</div>`
    : `<div class="card empty"><div style="font-size:36px">🤖</div><p>You haven't built any agents yet.</p><button class="primary" id="newAgent2">Create your first agent</button></div>`}
  </div>`;
  $$(".tile").forEach((t) => t.addEventListener("click", () => { location.hash = `#/agents/${t.dataset.id}`; }));
  $("#newAgent").addEventListener("click", newAgentDialog);
  if ($("#newAgent2")) $("#newAgent2").addEventListener("click", newAgentDialog);
}

function newAgentDialog() {
  modal(`<h2>New agent</h2>
    <form id="f">
      <div class="row"><div style="width:70px"><label>Icon</label><input name="icon" value="🤖" maxlength="8"></div>
        <div class="grow"><label>Name</label><input name="name" required placeholder="e.g. IT Helpdesk"></div></div>
      <label>Description</label><input name="description" placeholder="What does this agent help with?">
      <label>Instructions</label><textarea name="instructions" rows="4" placeholder="You are a friendly assistant that…"></textarea>
      <div class="actions"><button type="button" data-close>Cancel</button><button class="primary" type="submit">Create</button></div>
    </form>`, (el, close) => {
    $("#f", el).addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target));
      const a = await attempt(() => api("POST", "/api/agents", body));
      if (a) { close(); location.hash = `#/agents/${a.id}`; }
    });
  });
}

/* ------------------------------------------------------------------ agent editor */
async function renderAgentEditor(id) {
  shell("agents", `<div class="page"><div class="muted">Loading…</div></div>`);
  const [agent, workflows] = await Promise.all([
    attempt(() => api("GET", `/api/agents/${id}`)),
    attempt(() => api("GET", "/api/workflows")),
  ]);
  if (!agent) return;
  if (agent.role === "user") { location.hash = `#/chat/${id}`; return; }
  const E = { agent, workflows: workflows || [], tab: "overview", dirty: false, convo: null, messages: [] };
  const canEdit = ["owner", "editor"].includes(agent.role);
  const isOwner = agent.role === "owner";

  $("#main").innerHTML = `<div class="page" style="max-width:1400px">
    <div class="page-head">
      <a href="#/agents" class="muted">← Agents</a>
      <span style="font-size:26px" id="hIcon"></span><h1 id="hName"></h1><span id="hStatus"></span>
      <span class="grow"></span>
      <span class="muted small" id="dirty"></span>
      ${canEdit ? `<button id="save">Save</button><button id="publish" class="primary">Publish</button>` : `<span class="pill accent">read-only (${esc(agent.role)})</span>`}
      ${isOwner ? `<button id="del" class="danger ghost" title="Delete agent">🗑</button>` : ""}
    </div>
    <div class="editor">
      <div>
        <div class="tabs" id="tabs">
          ${[["overview", "Overview"], ["knowledge", "Knowledge"], ["topics", "Topics"], ["actions", "Actions"], ["publish", "Publish & share"], ["activity", "Activity"]]
            .map(([k, l]) => `<button data-tab="${k}">${l}</button>`).join("")}
        </div>
        <div id="tab"></div>
      </div>
      <div class="card chat">
        <div class="chat-head"><strong class="grow">Test your agent</strong><span class="pill">draft</span><button class="sm" id="reset">↺ New chat</button></div>
        <div class="chat-body" id="chatBody"></div>
        <form class="chat-input" id="chatForm"><input id="chatInput" placeholder="${canEdit ? "Send a test message…" : "Only editors can test drafts"}" autocomplete="off" ${canEdit ? "" : "disabled"}><button class="primary" ${canEdit ? "" : "disabled"}>Send</button></form>
      </div>
    </div></div>`;

  const header = () => {
    $("#hIcon").textContent = E.agent.icon;
    $("#hName").textContent = E.agent.name;
    $("#hStatus").innerHTML = statusPill(E.agent.status) + (E.agent.version ? ` <span class="pill">v${E.agent.version}</span>` : "");
    $("#dirty").textContent = E.dirty ? "Unsaved changes" : "";
  };
  const markDirty = () => { E.dirty = true; header(); };

  async function save(quiet) {
    if (!canEdit) return true;
    const a = E.agent;
    const body = {
      name: a.name, description: a.description, icon: a.icon, instructions: a.instructions, model: a.model,
      greeting: a.greeting, starters: a.starters, knowledge: a.knowledge, topics: a.topics, tools: a.tools,
    };
    const res = await attempt(() => api("PUT", `/api/agents/${id}`, body), quiet ? null : "Saved");
    if (!res) return false;
    E.agent = res; E.dirty = false; header();
    return true;
  }

  const tabs = {
    overview() {
      const a = E.agent;
      return `<div class="card"><fieldset ${canEdit ? "" : "disabled"} style="border:0;padding:0;margin:0">
        <div class="row"><div style="width:80px"><label>Icon</label><input data-f="icon" value="${esc(a.icon)}" maxlength="8"></div>
          <div class="grow"><label>Name</label><input data-f="name" value="${esc(a.name)}"></div></div>
        <label>Description</label><input data-f="description" value="${esc(a.description)}">
        <label>Model</label><select data-f="model">${S.meta.models.map((m) => `<option value="${m.id}" ${m.id === a.model ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</select>
        <label>Instructions</label>
        <textarea data-f="instructions" rows="9" placeholder="Describe how the agent should behave, its tone, and what it should or shouldn't do.">${esc(a.instructions)}</textarea>
        <label>Greeting message</label><input data-f="greeting" value="${esc(a.greeting)}" placeholder="Hi! How can I help today?">
        <label>Suggested prompts <span class="muted">(one per line)</span></label>
        <textarea data-f="starters" rows="3">${esc((a.starters || []).join("\n"))}</textarea>
      </fieldset></div>`;
    },
    knowledge() {
      const docs = E.agent.knowledge;
      return `<div class="card"><fieldset ${canEdit ? "" : "disabled"} style="border:0;padding:0;margin:0">
        <div class="row"><h2 class="grow" style="margin:0">Knowledge sources</h2>
          <label class="btn" style="margin:0;font-weight:400">⬆ Upload .txt/.md<input type="file" id="upload" accept=".txt,.md,.csv,.json,text/*" multiple hidden></label>
          <button id="addDoc">＋ Add text</button></div>
        <p class="muted" style="margin-top:8px">The agent grounds its answers on these documents.</p>
        ${docs.length ? docs.map((d, i) => `<div class="list-item">
          <div class="row"><input data-k="${i}" data-kf="title" value="${esc(d.title)}" placeholder="Title"><button class="ghost danger sm" data-delk="${i}">Remove</button></div>
          <textarea data-k="${i}" data-kf="content" rows="5" style="margin-top:8px" placeholder="Paste content…">${esc(d.content)}</textarea>
          <div class="muted small">${d.content.length.toLocaleString()} characters</div></div>`).join("")
          : `<div class="empty">No knowledge yet. Add FAQs, policies or product docs.</div>`}
      </fieldset></div>`;
    },
    topics() {
      const ts = E.agent.topics;
      const wfOpts = (sel) => `<option value="">— none —</option>` + E.workflows.map((w) => `<option value="${w.id}" ${w.id === sel ? "selected" : ""}>${esc(w.name)}${w.active ? "" : " (inactive)"}</option>`).join("");
      return `<div class="card"><fieldset ${canEdit ? "" : "disabled"} style="border:0;padding:0;margin:0">
        <div class="row"><h2 class="grow" style="margin:0">Topics</h2><button id="addTopic">＋ Add topic</button></div>
        <p class="muted" style="margin-top:8px">Topics are authored conversation paths. When a message contains a trigger phrase, the topic answers instead of the model. Use <code>{{message}}</code> and, when a workflow runs, <code>{{result}}</code>.</p>
        ${ts.length ? ts.map((t, i) => `<div class="list-item">
          <div class="row"><input data-t="${i}" data-tf="name" value="${esc(t.name)}" placeholder="Topic name">
            <label class="row" style="margin:0;font-weight:400;width:auto"><input type="checkbox" data-t="${i}" data-tf="enabled" ${t.enabled !== false ? "checked" : ""} style="width:auto"> On</label>
            <button class="ghost danger sm" data-delt="${i}">Remove</button></div>
          <label>Trigger phrases <span class="muted">(comma separated)</span></label>
          <input data-t="${i}" data-tf="triggers" value="${esc((t.triggers || []).join(", "))}" placeholder="reset password, forgot password">
          <label>Response</label><textarea data-t="${i}" data-tf="response" rows="3">${esc(t.response)}</textarea>
          <label>Run workflow first <span class="muted">(optional)</span></label><select data-t="${i}" data-tf="workflow_id">${wfOpts(t.workflow_id)}</select>
        </div>`).join("") : `<div class="empty">No topics. The agent will rely on instructions and knowledge.</div>`}
      </fieldset></div>`;
    },
    actions() {
      const tools = new Set(E.agent.tools);
      return `<div class="card"><fieldset ${canEdit ? "" : "disabled"} style="border:0;padding:0;margin:0">
        <h2>Actions</h2>
        <p class="muted">Attach workflows the agent can run as tools during generative answers. Only <strong>activated</strong> workflows are callable.</p>
        ${E.workflows.length ? E.workflows.map((w) => `<label class="list-item row" style="font-weight:400;margin:0 0 8px;cursor:pointer">
          <input type="checkbox" data-tool="${w.id}" ${tools.has(w.id) ? "checked" : ""} style="width:auto">
          <span class="grow"><strong>${esc(w.name)}</strong><br><span class="muted small">${esc(w.description || "No description")}</span></span>
          ${w.active ? statusPill("active") : `<span class="pill warn">inactive</span>`}</label>`).join("")
          : `<div class="empty">No workflows yet. <a href="#/workflows">Create one</a> to give this agent actions.</div>`}
      </fieldset></div>`;
    },
    publish() {
      const a = E.agent;
      const base = location.origin;
      const pub = `${base}/a/${a.slug}`;
      return `<div class="stack">
        <div class="card"><h2>Publish</h2>
          <p class="muted">Publishing snapshots the current saved configuration as a new version. Drafts keep changing without affecting users until you publish again.</p>
          <div class="row" style="flex-wrap:wrap">
            <div style="min-width:220px"><label>Who can use it</label><select id="vis" ${canEdit ? "" : "disabled"}>
              ${[["private", "Only people I share with"], ["org", "Everyone in the studio"], ["public", "Anyone with the link"]]
                .map(([v, l]) => `<option value="${v}" ${a.visibility === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
            <div class="grow"><label>Release notes</label><input id="notes" placeholder="What changed?" ${canEdit ? "" : "disabled"}></div>
          </div>
          <div class="row" style="margin-top:14px">${canEdit ? `<button class="primary" id="pub2">Publish v${a.version + 1}</button>${a.status === "published" ? `<button id="unpub">Unpublish</button>` : ""}` : ""}</div>
        </div>
        ${a.status === "published" ? `<div class="card"><h2>Channels</h2>
          <label>In-studio chat</label><div class="row"><input readonly value="${esc(base)}/#/chat/${a.id}"><a class="btn" href="#/chat/${a.id}">Open</a></div>
          ${a.visibility === "public" ? `<label>Public link</label><div class="row"><input readonly value="${esc(pub)}"><button data-copy="${esc(pub)}">Copy</button><a class="btn" href="${esc(pub)}" target="_blank">Open</a></div>
          <label>Embed on a website</label><textarea readonly rows="2" class="mono">&lt;iframe src="${esc(pub)}?embed=1" style="width:400px;height:600px;border:0"&gt;&lt;/iframe&gt;</textarea>`
          : `<p class="muted small" style="margin-top:10px">Set visibility to “Anyone with the link” to get a public link and embed code.</p>`}
        </div>` : ""}
        <div class="card"><h2>Sharing</h2>
          <p class="muted">Editors can change and publish the agent; viewers can see its configuration and use the published version.</p>
          ${isOwner ? `<form class="row" id="shareForm"><input name="username" placeholder="username" list="userList" required class="grow"><datalist id="userList"></datalist>
            <select name="role" style="width:120px"><option value="viewer">Viewer</option><option value="editor">Editor</option></select><button class="primary">Share</button></form>` : ""}
          <div id="shares" style="margin-top:10px"></div>
        </div>
        <div class="card"><h2>Version history</h2><div id="versions" class="muted">Loading…</div></div>
      </div>`;
    },
    activity() {
      return `<div class="card"><h2>Conversations</h2><div id="convos" class="muted">Loading…</div></div>`;
    },
  };

  function renderTab() {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === E.tab));
    $("#tab").innerHTML = tabs[E.tab]();
    bindTab();
  }

  function bindTab() {
    const a = E.agent;
    $$("[data-f]").forEach((el) => el.addEventListener("input", () => {
      const f = el.dataset.f;
      a[f] = f === "starters" ? el.value.split("\n").map((s) => s.trim()).filter(Boolean) : el.value;
      markDirty();
      if (f === "icon" || f === "name") header();
    }));
    // knowledge
    $$("[data-k]").forEach((el) => el.addEventListener("input", () => { a.knowledge[+el.dataset.k][el.dataset.kf] = el.value; markDirty(); }));
    $$("[data-delk]").forEach((el) => el.addEventListener("click", () => { a.knowledge.splice(+el.dataset.delk, 1); markDirty(); renderTab(); }));
    if ($("#addDoc")) $("#addDoc").addEventListener("click", () => { a.knowledge.push({ title: "New document", content: "" }); markDirty(); renderTab(); });
    if ($("#upload")) $("#upload").addEventListener("change", async (e) => {
      for (const file of e.target.files) {
        if (file.size > 100000) { toast(`${file.name} is larger than 100 KB`, true); continue; }
        a.knowledge.push({ title: file.name, content: await file.text() });
      }
      markDirty(); renderTab();
    });
    // topics
    $$("[data-t]").forEach((el) => el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => {
      const t = a.topics[+el.dataset.t];
      const f = el.dataset.tf;
      if (f === "triggers") t.triggers = el.value.split(",").map((s) => s.trim()).filter(Boolean);
      else if (f === "enabled") t.enabled = el.checked;
      else if (f === "workflow_id") t.workflow_id = el.value || null;
      else t[f] = el.value;
      markDirty();
    }));
    $$("[data-delt]").forEach((el) => el.addEventListener("click", () => { a.topics.splice(+el.dataset.delt, 1); markDirty(); renderTab(); }));
    if ($("#addTopic")) $("#addTopic").addEventListener("click", () => {
      a.topics.push({ name: "New topic", triggers: [], response: "", workflow_id: null, enabled: true }); markDirty(); renderTab();
    });
    // actions
    $$("[data-tool]").forEach((el) => el.addEventListener("change", () => {
      const s = new Set(a.tools);
      el.checked ? s.add(el.dataset.tool) : s.delete(el.dataset.tool);
      a.tools = [...s]; markDirty();
    }));
    // publish tab
    $$("[data-copy]").forEach((b) => b.addEventListener("click", () => { navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied")); }));
    if ($("#pub2")) $("#pub2").addEventListener("click", () => publish($("#vis").value, $("#notes").value));
    if ($("#unpub")) $("#unpub").addEventListener("click", async () => {
      const r = await attempt(() => api("POST", `/api/agents/${id}/unpublish`), "Unpublished");
      if (r) { E.agent = r; header(); renderTab(); }
    });
    if ($("#shares")) loadShares();
    if ($("#versions")) loadVersions();
    if ($("#convos")) loadConvos();
  }

  async function publish(visibility, notes) {
    if (E.dirty && !(await save(true))) return;
    const r = await attempt(() => api("POST", `/api/agents/${id}/publish`, { visibility, notes }), "Published 🎉");
    if (r) { E.agent = r; header(); if (E.tab === "publish") renderTab(); }
  }

  async function loadShares() {
    const list = await attempt(() => api("GET", `/api/agents/${id}/shares`));
    if (!list) return;
    renderShareList($("#shares"), list, isOwner, async (username) => {
      const r = await attempt(() => api("DELETE", `/api/agents/${id}/shares/${encodeURIComponent(username)}`), "Access removed");
      if (r) loadShares();
    });
    const form = $("#shareForm");
    if (form && !form.dataset.bound) {
      form.dataset.bound = "1";
      bindUserSuggest($("input[name=username]", form), $("#userList"));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const r = await attempt(() => api("POST", `/api/agents/${id}/shares`, Object.fromEntries(new FormData(form))), "Shared");
        if (r) { form.reset(); loadShares(); }
      });
    }
  }

  async function loadVersions() {
    const vs = await attempt(() => api("GET", `/api/agents/${id}/versions`));
    if (!vs) return;
    $("#versions").innerHTML = vs.length ? `<table><tr><th>Version</th><th>Notes</th><th>By</th><th>When</th><th></th></tr>
      ${vs.map((v) => `<tr><td>v${v.version}</td><td>${esc(v.notes || "—")}</td><td>@${esc(v.published_by)}</td><td class="muted">${ago(v.created_at)}</td>
      <td>${canEdit ? `<button class="sm" data-restore="${v.version}">Restore to draft</button>` : ""}</td></tr>`).join("")}</table>`
      : "Not published yet.";
    $$("[data-restore]").forEach((b) => b.addEventListener("click", async () => {
      const r = await attempt(() => api("POST", `/api/agents/${id}/versions/${b.dataset.restore}/restore`), `Draft restored from v${b.dataset.restore}`);
      if (r) { E.agent = r; E.dirty = false; header(); }
    }));
  }

  async function loadConvos() {
    if (!canEdit) { $("#convos").textContent = "Only editors can see conversations."; return; }
    const cs = await attempt(() => api("GET", `/api/agents/${id}/conversations`));
    if (!cs) return;
    $("#convos").innerHTML = cs.length ? cs.map((c) => `<details class="list-item"><summary>
        <strong>${esc(c.username ? "@" + c.username : "anonymous")}</strong> · <span class="pill">${esc(c.channel)}</span>
        · ${c.messages.length / 2} turns · <span class="muted">${ago(c.updated_at)}</span></summary>
        <div style="margin-top:8px">${c.messages.map((m) => `<p><strong>${m.role === "user" ? "User" : "Agent"}:</strong> ${md(m.content)}</p>`).join("")}</div>
      </details>`).join("") : "No conversations yet.";
  }

  /* chat */
  function renderChat() {
    const body = $("#chatBody");
    const greet = E.agent.greeting ? `<div class="msg bot">${md(E.agent.greeting)}</div>` : "";
    const starters = !E.messages.length && E.agent.starters.length
      ? `<div class="chips">${E.agent.starters.map((s) => `<span class="chip" data-starter="${esc(s)}">${esc(s)}</span>`).join("")}</div>` : "";
    body.innerHTML = greet + starters + E.messages.map(msgHtml).join("");
    $$("[data-starter]", body).forEach((c) => c.addEventListener("click", () => send(c.dataset.starter)));
    body.scrollTop = body.scrollHeight;
  }

  async function send(text) {
    if (!text.trim() || !canEdit) return;
    if (E.dirty && !(await save(true))) return;
    E.messages.push({ role: "user", content: text });
    E.messages.push({ role: "pending" });
    renderChat();
    try {
      const r = await api("POST", `/api/agents/${id}/chat`, { message: text, conversation_id: E.convo, mode: "draft" });
      E.convo = r.conversation_id;
      E.messages.pop();
      E.messages.push({ role: "assistant", content: r.reply.text, reply: r.reply });
    } catch (e) {
      E.messages.pop();
      E.messages.push({ role: "assistant", content: `⚠️ ${e.message}` });
    }
    renderChat();
  }

  $("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#chatInput").value; $("#chatInput").value = ""; send(v); });
  $("#reset").addEventListener("click", () => { E.convo = null; E.messages = []; renderChat(); });
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => { E.tab = b.dataset.tab; renderTab(); }));
  if ($("#save")) $("#save").addEventListener("click", () => save(false));
  if ($("#publish")) $("#publish").addEventListener("click", () => { E.tab = "publish"; renderTab(); });
  if ($("#del")) $("#del").addEventListener("click", () => confirmDialog(`Delete “${E.agent.name}”? This removes all versions and conversations.`, async () => {
    if (await attempt(() => api("DELETE", `/api/agents/${id}`), "Agent deleted")) location.hash = "#/agents";
  }));
  guardUnsaved(() => E.dirty);
  header(); renderTab(); renderChat();
}

function msgHtml(m) {
  if (m.role === "pending") return `<div class="msg bot typing">Thinking…</div>`;
  if (m.role === "user") return `<div class="msg user">${esc(m.content)}</div>`;
  const r = m.reply;
  let meta = "";
  if (r) {
    const src = { topic: `topic: ${r.topic}`, llm: "generative answer", knowledge: "from knowledge", fallback: "no match", error: "error" }[r.source] || r.source;
    const tools = (r.tool_calls || []).map((c) => `⚙ ${esc(c.workflow || "workflow")} ${c.status === "succeeded" ? "✓" : "✗"}`).join(" · ");
    meta = `<span class="meta">${esc(src)}${tools ? " · " + tools : ""}</span>`;
  }
  return `<div class="msg bot">${md(m.content)}${meta}</div>`;
}

function renderShareList(el, list, canManage, onRemove) {
  el.innerHTML = list.length ? `<table>${list.map((s) => `<tr><td><strong>${esc(s.display_name)}</strong> <span class="muted">@${esc(s.username)}</span></td>
    <td><span class="pill accent">${esc(s.role)}</span></td><td style="text-align:right">${canManage ? `<button class="sm ghost danger" data-unshare="${esc(s.username)}">Remove</button>` : ""}</td></tr>`).join("")}</table>`
    : `<p class="muted">Not shared with anyone yet.</p>`;
  $$("[data-unshare]", el).forEach((b) => b.addEventListener("click", () => onRemove(b.dataset.unshare)));
}

function bindUserSuggest(input, datalist) {
  let t;
  input.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const us = await api("GET", `/api/users?q=${encodeURIComponent(input.value)}`).catch(() => []);
      datalist.innerHTML = us.map((u) => `<option value="${esc(u.username)}">${esc(u.display_name)}</option>`).join("");
    }, 200);
  });
}

function confirmDialog(text, onYes) {
  modal(`<h2>Are you sure?</h2><p>${esc(text)}</p><div class="actions"><button data-close>Cancel</button><button class="primary" id="yes">Delete</button></div>`,
    (el, close) => $("#yes", el).addEventListener("click", () => { close(); onYes(); }));
}

let unsavedGuard = null;
function guardUnsaved(isDirty) { unsavedGuard = isDirty; }
window.addEventListener("beforeunload", (e) => { if (unsavedGuard && unsavedGuard()) { e.preventDefault(); e.returnValue = ""; } });

/* ------------------------------------------------------------------ catalog & consumer chat */
async function renderCatalog() {
  shell("catalog", `<div class="page"><div class="muted">Loading…</div></div>`);
  const agents = await attempt(() => api("GET", "/api/catalog"));
  if (!agents) return;
  $("#main").innerHTML = `<div class="page">
    <div class="page-head"><h1 class="grow">Agent catalog</h1><input id="q" placeholder="Search agents…" style="max-width:260px"></div>
    <p class="muted">Published agents shared with everyone in the studio.</p>
    <div class="grid" id="cat"></div></div>`;
  const draw = (q) => {
    const list = agents.filter((a) => (a.name + " " + a.description).toLowerCase().includes(q.toLowerCase()));
    $("#cat").innerHTML = list.length ? list.map((a) => `<div class="card tile" data-id="${a.id}">
      <div class="row"><span class="icon">${esc(a.icon)}</span><span class="grow"></span><span class="pill">v${a.version}</span>${a.visibility === "public" ? `<span class="pill accent">public</span>` : ""}</div>
      <div class="title">${esc(a.name)}</div><div class="desc">${esc(a.description || "")}</div>
      <div class="muted small">by @${esc(a.owner.username)} · ${ago(a.published_at)}</div></div>`).join("")
      : `<div class="card empty" style="grid-column:1/-1">No published agents${q ? " match your search" : " yet"}.</div>`;
    $$(".tile").forEach((t) => t.addEventListener("click", () => { location.hash = `#/chat/${t.dataset.id}`; }));
  };
  $("#q").addEventListener("input", (e) => draw(e.target.value));
  draw("");
}

async function renderConsumerChat(id) {
  shell("catalog", `<div class="page"><div class="muted">Loading…</div></div>`);
  const a = await attempt(() => api("GET", `/api/agents/${id}`));
  if (!a) return;
  if (a.status !== "published") { $("#main").innerHTML = `<div class="page"><div class="card empty">This agent is not published.</div></div>`; return; }
  const C = { convo: null, messages: [] };
  $("#main").innerHTML = `<div class="page" style="max-width:820px">
    <div class="page-head"><a href="#/catalog" class="muted">← Catalog</a><span style="font-size:26px">${esc(a.icon)}</span>
      <div class="grow"><h1>${esc(a.name)}</h1><div class="muted small">${esc(a.description)}</div></div>
      ${["owner", "editor", "viewer"].includes(a.role) ? `<a class="btn" href="#/agents/${a.id}">Open in editor</a>` : ""}</div>
    <div class="card chat" style="position:static;height:calc(100vh - 150px)">
      <div class="chat-body" id="chatBody"></div>
      <form class="chat-input" id="chatForm"><input id="chatInput" placeholder="Message ${esc(a.name)}…" autocomplete="off"><button class="primary">Send</button></form>
    </div></div>`;
  const draw = () => {
    const starters = !C.messages.length && a.starters.length ? `<div class="chips">${a.starters.map((s) => `<span class="chip" data-starter="${esc(s)}">${esc(s)}</span>`).join("")}</div>` : "";
    $("#chatBody").innerHTML = (a.greeting ? `<div class="msg bot">${md(a.greeting)}</div>` : "") + starters + C.messages.map(msgHtml).join("");
    $$("[data-starter]").forEach((c) => c.addEventListener("click", () => send(c.dataset.starter)));
    $("#chatBody").scrollTop = 1e9;
  };
  const send = async (text) => {
    if (!text.trim()) return;
    C.messages.push({ role: "user", content: text }, { role: "pending" }); draw();
    try {
      const r = await api("POST", `/api/agents/${id}/chat`, { message: text, conversation_id: C.convo, mode: "published" });
      C.convo = r.conversation_id; C.messages.pop();
      C.messages.push({ role: "assistant", content: r.reply.text, reply: r.reply });
    } catch (e) { C.messages.pop(); C.messages.push({ role: "assistant", content: `⚠️ ${e.message}` }); }
    draw();
  };
  $("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#chatInput").value; $("#chatInput").value = ""; send(v); });
  draw();
}

/* ------------------------------------------------------------------ workflows list */
async function renderWorkflows() {
  shell("workflows", `<div class="page"><div class="muted">Loading…</div></div>`);
  const wfs = await attempt(() => api("GET", "/api/workflows"));
  if (!wfs) return;
  const trig = { manual: "▶ Manual", webhook: "🔗 Webhook", schedule: "⏰ Schedule", agent: "🤖 Agent action" };
  $("#main").innerHTML = `<div class="page">
    <div class="page-head"><h1 class="grow">Workflows</h1><button id="newWf" class="primary">＋ New workflow</button></div>
    ${wfs.length ? `<div class="card" style="padding:0"><table>
      <tr><th>Name</th><th>Trigger</th><th>Status</th><th>Steps</th><th>Last run</th><th>Updated</th></tr>
      ${wfs.map((w) => `<tr class="wfrow" data-id="${w.id}" style="cursor:pointer">
        <td><strong>${esc(w.name)}</strong>${w.role !== "owner" ? ` <span class="pill accent">${esc(w.role)}</span>` : ""}<br><span class="muted small">${esc(w.description)}</span></td>
        <td>${trig[w.trigger.type] || esc(w.trigger.type)}${w.trigger.type === "schedule" ? `<span class="muted small"> every ${w.trigger.interval_seconds}s</span>` : ""}</td>
        <td>${w.active ? statusPill("active") : `<span class="pill">off</span>`}</td>
        <td>${w.nodes.length}</td><td class="muted">${ago(w.last_run_at)}</td><td class="muted">${ago(w.updated_at)}</td></tr>`).join("")}
    </table></div>` : `<div class="card empty"><div style="font-size:36px">🔀</div><p>Workflows automate work: call APIs, ask agents, branch on results.</p><button class="primary" id="newWf2">Design your first workflow</button></div>`}
  </div>`;
  $$(".wfrow").forEach((r) => r.addEventListener("click", () => { location.hash = `#/workflows/${r.dataset.id}`; }));
  $("#newWf").addEventListener("click", newWorkflowDialog);
  if ($("#newWf2")) $("#newWf2").addEventListener("click", newWorkflowDialog);
}

function newWorkflowDialog() {
  modal(`<h2>New workflow</h2><form id="f">
    <label>Name</label><input name="name" required placeholder="e.g. Triage support tickets">
    <label>Description</label><input name="description" placeholder="Shown to agents when used as an action">
    <label>Start from</label><select name="template">
      <option value="">Blank canvas</option>
      <option value="ask_agent">Ask an agent and return its answer</option>
      <option value="health_check">Scheduled API health check</option></select>
    <label>Trigger</label><select name="trigger">
      <option value="manual">Manual</option><option value="webhook">Webhook (HTTP POST)</option>
      <option value="schedule">Schedule</option><option value="agent">Agent action</option></select>
    <div class="actions"><button type="button" data-close>Cancel</button><button class="primary">Create</button></div></form>`, (el, close) => {
    $("#f", el).addEventListener("submit", async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target));
      const body = { name: d.name, description: d.description, template: d.template || null,
        trigger: { type: d.trigger, interval_seconds: d.trigger === "schedule" ? 300 : null } };
      const w = await attempt(() => api("POST", "/api/workflows", body));
      if (w) { close(); location.hash = `#/workflows/${w.id}`; }
    });
  });
}

/* ------------------------------------------------------------------ workflow designer */
const NODE_W = 190;
const typeInfo = (t) => S.meta.node_types.find((n) => n.type === t) || { icon: "?", label: t, fields: [] };

function portPos(node, kind, branch) {
  if (kind === "in") return { x: node.x, y: node.y + 23 };
  const y = branch === "true" ? 13 : branch === "false" ? 35 : 23;
  return { x: node.x + NODE_W, y: node.y + y };
}

function curve(a, b) {
  const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

async function renderDesigner(id) {
  shell("workflows", `<div class="page"><div class="muted">Loading…</div></div>`);
  const [wf, agents] = await Promise.all([attempt(() => api("GET", `/api/workflows/${id}`)), attempt(() => api("GET", "/api/agents"))]);
  if (!wf) return;
  const catalog = await api("GET", "/api/catalog").catch(() => []);
  const agentOpts = [...(agents || []), ...catalog.filter((c) => !(agents || []).some((a) => a.id === c.id))];
  const D = { wf, sel: null, selEdge: null, dirty: false, runs: [], run: null, runsOpen: true, sampleInput: store(`studio.sample.${id}`) || '{\n  "message": "Hello"\n}' };
  const canEdit = ["owner", "editor"].includes(wf.role);

  $("#main").innerHTML = `<div class="designer">
    <div class="designer-bar">
      <a href="#/workflows" class="muted">←</a>
      <input class="name" id="wfName" value="${esc(wf.name)}" ${canEdit ? "" : "disabled"}>
      <span id="wfStatus"></span><span class="muted small" id="dirty"></span>
      <span class="grow" style="flex:1"></span>
      ${canEdit ? `<button id="validate">✓ Validate</button><button id="save">Save</button><button id="runBtn">▶ Test run</button><button id="activate" class="primary"></button>` : `<span class="pill accent">read-only (${esc(wf.role)})</span>`}
      <button id="shareBtn">Share</button>
      ${wf.role === "owner" ? `<button id="del" class="ghost danger" title="Delete workflow">🗑</button>` : ""}
    </div>
    <div class="designer-main">
      <div class="palette" id="palette"></div>
      <div class="canvas-wrap" id="wrap"><div class="canvas" id="canvas"><svg id="edges"></svg></div></div>
      <div class="props" id="props"></div>
    </div>
    <div class="runs-panel" id="runs"></div>
  </div>`;

  const header = () => {
    $("#wfStatus").innerHTML = D.wf.active ? statusPill("active") : `<span class="pill">inactive</span>`;
    $("#dirty").textContent = D.dirty ? "Unsaved changes" : "";
    if ($("#activate")) $("#activate").textContent = D.wf.active ? "⏸ Deactivate" : "⚡ Activate";
  };
  const markDirty = () => { D.dirty = true; header(); };

  /* palette */
  const cats = [...new Set(S.meta.node_types.map((n) => n.category))].filter((c) => c !== "Start");
  $("#palette").innerHTML = canEdit ? cats.map((c) => `<h4>${c}</h4>` + S.meta.node_types.filter((n) => n.category === c)
    .map((n) => `<div class="palette-item" draggable="true" data-type="${n.type}" title="${esc(n.description)}"><span>${n.icon}</span><span>${esc(n.label)}</span></div>`).join("")).join("")
    : `<p class="muted small">You have view access to this workflow.</p>`;
  $$(".palette-item").forEach((p) => {
    p.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/node-type", p.dataset.type));
    p.addEventListener("click", () => {
      const wrap = $("#wrap");
      addNode(p.dataset.type, wrap.scrollLeft + 120 + Math.random() * 60, wrap.scrollTop + 80 + Math.random() * 60);
    });
  });
  $("#wrap").addEventListener("dragover", (e) => { if (canEdit) e.preventDefault(); });
  $("#wrap").addEventListener("drop", (e) => {
    const type = e.dataTransfer.getData("text/node-type");
    if (!type) return;
    e.preventDefault();
    const r = $("#canvas").getBoundingClientRect();
    addNode(type, e.clientX - r.left - NODE_W / 2, e.clientY - r.top - 20);
  });

  function addNode(type, x, y) {
    const info = typeInfo(type);
    let n = 1;
    while (D.wf.nodes.some((nd) => nd.id === `${type}_${n}`)) n++;
    const config = {};
    info.fields.forEach((f) => { if (f.default !== undefined) config[f.key] = f.default; });
    const node = { id: `${type}_${n}`, type, name: info.label, config, x: Math.max(10, Math.round(x)), y: Math.max(10, Math.round(y)) };
    D.wf.nodes.push(node);
    D.sel = node.id; D.selEdge = null;
    markDirty(); draw();
  }

  /* canvas */
  function nodeSub(n) {
    const c = n.config || {};
    switch (n.type) {
      case "trigger": return { manual: "Manual", webhook: "Webhook", schedule: `Every ${D.wf.trigger.interval_seconds}s`, agent: "Agent action" }[D.wf.trigger.type];
      case "http_request": return `${c.method || "GET"} ${(c.url || "").replace(/^https?:\/\//, "")}`;
      case "agent": { const a = agentOpts.find((x) => x.id === c.agent_id); return a ? a.name : "Choose an agent"; }
      case "condition": return `${c.left || ""} ${(c.operator || "").replace("_", " ")} ${c.right || ""}`;
      case "set_variable": return `vars.${c.name || ""}`;
      case "delay": return `${c.seconds || 0}s`;
      default: return typeInfo(n.type).label;
    }
  }

  function draw() {
    const canvas = $("#canvas");
    const status = {};
    if (D.run) D.run.steps.forEach((s) => { status[s.node_id] = s.status; });
    $$(".node", canvas).forEach((n) => n.remove());
    for (const n of D.wf.nodes) {
      const el = document.createElement("div");
      el.className = `node${n.id === D.sel ? " selected" : ""}${status[n.id] ? " ran-" + status[n.id] : ""}`;
      el.style.left = `${n.x}px`; el.style.top = `${n.y}px`;
      el.dataset.id = n.id;
      const outs = n.type === "condition" ? `<span class="port out true" data-branch="true" title="true"></span><span class="port out false" data-branch="false" title="false"></span>`
        : n.type === "respond" ? "" : `<span class="port out" title="Drag to connect"></span>`;
      el.innerHTML = `${n.type === "trigger" ? "" : `<span class="port in"></span>`}${outs}
        <div class="node-head"><span class="ico">${typeInfo(n.type).icon}</span><div style="min-width:0"><div class="t">${esc(n.name || n.type)}</div><div class="s t" style="font-weight:400">${esc(nodeSub(n) || "")}</div></div></div>`;
      canvas.appendChild(el);
    }
    drawEdges();
    drawProps();
  }

  function drawEdges(temp) {
    const byId = Object.fromEntries(D.wf.nodes.map((n) => [n.id, n]));
    let html = "";
    D.wf.edges.forEach((e, i) => {
      const a = byId[e.source], b = byId[e.target];
      if (!a || !b) return;
      const p1 = portPos(a, "out", e.branch), p2 = portPos(b, "in");
      const d = curve(p1, p2);
      html += `<path class="edge-hit" data-edge="${i}" d="${d}"></path><path class="edge ${e.branch || ""}${D.selEdge === i ? " selected" : ""}" d="${d}"></path>`;
      if (e.branch) html += `<text class="edge-label" x="${p1.x + 10}" y="${p1.y - 4}">${e.branch}</text>`;
    });
    if (temp) html += `<path class="edge" style="stroke-dasharray:5 4" d="${curve(temp.a, temp.b)}"></path>`;
    $("#edges").innerHTML = html;
  }

  /* pointer interactions: move nodes, connect ports, select */
  let drag = null;
  $("#canvas").addEventListener("pointerdown", (e) => {
    const edgeEl = e.target.closest("[data-edge]");
    if (edgeEl) { D.selEdge = +edgeEl.dataset.edge; D.sel = null; draw(); return; }
    const nodeEl = e.target.closest(".node");
    if (!nodeEl) { if (D.sel || D.selEdge !== null) { D.sel = null; D.selEdge = null; draw(); } return; }
    const node = D.wf.nodes.find((n) => n.id === nodeEl.dataset.id);
    const r = $("#canvas").getBoundingClientRect();
    if (e.target.classList.contains("port") && e.target.classList.contains("out")) {
      if (!canEdit) return;
      drag = { kind: "connect", node, branch: e.target.dataset.branch || null, r };
    } else {
      drag = { kind: "move", node, el: nodeEl, dx: e.clientX - r.left - node.x, dy: e.clientY - r.top - node.y, r, moved: false };
      if (D.sel !== node.id) { D.sel = node.id; D.selEdge = null; draw(); drag.el = $(`.node[data-id="${CSS.escape(node.id)}"]`); }
    }
    e.preventDefault();
  });
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  function onMove(e) {
    if (!drag || !document.body.contains($("#canvas"))) return;
    const r = $("#canvas").getBoundingClientRect();
    if (drag.kind === "move" && canEdit) {
      drag.node.x = Math.max(0, Math.round(e.clientX - r.left - drag.dx));
      drag.node.y = Math.max(0, Math.round(e.clientY - r.top - drag.dy));
      drag.el.style.left = `${drag.node.x}px`; drag.el.style.top = `${drag.node.y}px`;
      drag.moved = true;
      drawEdges();
    } else if (drag.kind === "connect") {
      drawEdges({ a: portPos(drag.node, "out", drag.branch), b: { x: e.clientX - r.left, y: e.clientY - r.top } });
    }
  }
  function onUp(e) {
    if (!drag) return;
    const d = drag; drag = null;
    if (!document.body.contains($("#canvas"))) {
      window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); return;
    }
    if (d.kind === "move" && d.moved) markDirty();
    if (d.kind === "connect") {
      const target = document.elementFromPoint(e.clientX, e.clientY)?.closest(".node");
      const tid = target && target.dataset.id;
      const tnode = tid && D.wf.nodes.find((n) => n.id === tid);
      if (tnode && tid !== d.node.id && tnode.type !== "trigger") {
        const exists = D.wf.edges.some((x) => x.source === d.node.id && x.target === tid && (x.branch || null) === d.branch);
        if (d.branch) D.wf.edges = D.wf.edges.filter((x) => !(x.source === d.node.id && x.branch === d.branch));
        if (!exists) { D.wf.edges.push(d.branch ? { source: d.node.id, target: tid, branch: d.branch } : { source: d.node.id, target: tid }); markDirty(); }
      }
      drawEdges();
    }
  }
  const onKey = (e) => {
    if (!document.body.contains($("#canvas"))) { window.removeEventListener("keydown", onKey); return; }
    if (!canEdit || !["Delete", "Backspace"].includes(e.key) || e.target.matches("input, textarea, select")) return;
    if (D.selEdge !== null) { D.wf.edges.splice(D.selEdge, 1); D.selEdge = null; markDirty(); draw(); }
    else if (D.sel) deleteNode(D.sel);
  };
  window.addEventListener("keydown", onKey);

  function deleteNode(nid) {
    const n = D.wf.nodes.find((x) => x.id === nid);
    if (!n || n.type === "trigger") return;
    D.wf.nodes = D.wf.nodes.filter((x) => x.id !== nid);
    D.wf.edges = D.wf.edges.filter((e) => e.source !== nid && e.target !== nid);
    D.sel = null; markDirty(); draw();
  }

  /* properties panel */
  function drawProps() {
    const p = $("#props");
    if (D.selEdge !== null) {
      const e = D.wf.edges[D.selEdge];
      p.innerHTML = `<h3>Connection</h3><p class="muted">${esc(e.source)} → ${esc(e.target)}${e.branch ? ` (when ${e.branch})` : ""}</p>
        ${canEdit ? `<button class="danger" id="delEdge">Remove connection</button>` : ""}`;
      if ($("#delEdge")) $("#delEdge").addEventListener("click", () => { D.wf.edges.splice(D.selEdge, 1); D.selEdge = null; markDirty(); draw(); });
      return;
    }
    const n = D.wf.nodes.find((x) => x.id === D.sel);
    if (!n) {
      p.innerHTML = `<h3>Workflow</h3><fieldset ${canEdit ? "" : "disabled"} style="border:0;padding:0;margin:0">
        <label>Description</label><textarea id="wfDesc" rows="3" placeholder="What it does (agents see this)">${esc(D.wf.description)}</textarea></fieldset>
        <p class="muted small" style="margin-top:14px">Drag blocks from the left onto the canvas. Drag from a block's right-hand port to another block to connect them. Select a block or connection and press Delete to remove it.</p>
        <p class="muted small">Reference data in any field: <code>{{input.x}}</code>, <code>{{last}}</code>, <code>{{vars.name}}</code>, <code>{{steps.&lt;id&gt;.field}}</code>.</p>`;
      if ($("#wfDesc")) $("#wfDesc").addEventListener("input", (e) => { D.wf.description = e.target.value; markDirty(); });
      return;
    }
    const info = typeInfo(n.type);
    let html = `<div class="row"><span style="font-size:20px">${info.icon}</span><h3 class="grow" style="margin:0">${esc(info.label)}</h3>
      ${canEdit && n.type !== "trigger" ? `<button class="sm ghost danger" id="delNode">Delete</button>` : ""}</div>
      <p class="muted small">${esc(info.description)}</p>
      <fieldset ${canEdit ? "" : "disabled"} style="border:0;padding:0;margin:0">
      <label>Name</label><input data-nf="name" value="${esc(n.name)}">
      <div class="muted small" style="margin-top:4px">id: <code>${esc(n.id)}</code> → <code>{{steps.${esc(n.id)}}}</code></div>`;
    if (n.type === "trigger") {
      const t = D.wf.trigger;
      html += `<label>Trigger type</label><select id="trigType">${S.meta.trigger_types.map((x) => `<option value="${x}" ${x === t.type ? "selected" : ""}>${x}</option>`).join("")}</select>`;
      if (t.type === "schedule") html += `<label>Run every (seconds)</label><input id="trigInterval" type="number" min="10" value="${esc(t.interval_seconds || 300)}">`;
      if (t.type === "webhook" && D.wf.webhook_key) {
        const url = `${location.origin}/api/hooks/${D.wf.id}?key=${D.wf.webhook_key}`;
        html += `<label>Webhook URL</label><textarea readonly rows="3" class="mono">${esc(url)}</textarea>
          <p class="muted small">POST JSON to this URL while the workflow is active. The body becomes <code>{{input}}</code>.</p>
          <div class="row"><button class="sm" id="copyHook">Copy</button>${D.wf.role === "owner" ? `<button class="sm" id="rotate">Rotate key</button>` : ""}</div>
          <label>curl example</label><pre class="out">curl -X POST '${esc(url)}' \\\n  -H 'Content-Type: application/json' \\\n  -d '{"message":"hi"}'</pre>`;
      }
      if (t.type === "agent") html += `<p class="muted small" style="margin-top:10px">Attach this workflow to an agent under <strong>Actions</strong> or a topic. The agent's tool input becomes <code>{{input}}</code>.</p>`;
      if (t.type === "manual") html += `<p class="muted small" style="margin-top:10px">Run it with <strong>▶ Test run</strong>.</p>`;
    }
    for (const f of info.fields) {
      const v = n.config[f.key] ?? "";
      html += `<label>${esc(f.label)}</label>`;
      if (f.kind === "select") html += `<select data-cf="${f.key}">${f.options.map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`;
      else if (f.kind === "textarea") html += `<textarea data-cf="${f.key}" rows="4" class="mono">${esc(typeof v === "string" ? v : JSON.stringify(v))}</textarea>`;
      else if (f.kind === "number") html += `<input data-cf="${f.key}" type="number" value="${esc(v)}">`;
      else if (f.kind === "agent") html += `<select data-cf="${f.key}"><option value="">— choose —</option>${agentOpts.map((a) => `<option value="${a.id}" ${a.id === v ? "selected" : ""}>${esc(a.icon)} ${esc(a.name)}${a.status === "published" ? "" : " (draft)"}</option>`).join("")}</select>`;
      else html += `<input data-cf="${f.key}" value="${esc(v)}">`;
    }
    html += `</fieldset>`;
    const step = D.run && D.run.steps.find((s) => s.node_id === n.id);
    if (step) html += `<label>Last run output</label>${statusPill(step.status)}<pre class="out">${esc(step.error || JSON.stringify(step.output, null, 2))}</pre>`;
    p.innerHTML = html;

    const refreshNode = () => {
      const el = $(`.node[data-id="${CSS.escape(n.id)}"]`);
      if (el) { $(".t", el).textContent = n.name; $(".s", el).textContent = nodeSub(n) || ""; }
    };
    $$("[data-nf]", p).forEach((el) => el.addEventListener("input", () => { n.name = el.value; markDirty(); refreshNode(); }));
    $$("[data-cf]", p).forEach((el) => el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
      n.config[el.dataset.cf] = el.type === "number" ? Number(el.value) : el.value; markDirty(); refreshNode();
    }));
    if ($("#delNode")) $("#delNode").addEventListener("click", () => deleteNode(n.id));
    if ($("#trigType")) $("#trigType").addEventListener("change", (e) => {
      D.wf.trigger = { type: e.target.value, interval_seconds: e.target.value === "schedule" ? (D.wf.trigger.interval_seconds || 300) : null };
      markDirty(); draw();
    });
    if ($("#trigInterval")) $("#trigInterval").addEventListener("input", (e) => { D.wf.trigger.interval_seconds = Number(e.target.value); markDirty(); refreshNode(); });
    if ($("#copyHook")) $("#copyHook").addEventListener("click", () => navigator.clipboard.writeText(`${location.origin}/api/hooks/${D.wf.id}?key=${D.wf.webhook_key}`).then(() => toast("Copied")));
    if ($("#rotate")) $("#rotate").addEventListener("click", async () => {
      const r = await attempt(() => api("POST", `/api/workflows/${id}/rotate-key`), "New webhook key issued");
      if (r) { D.wf.webhook_key = r.webhook_key; drawProps(); }
    });
  }

  /* runs panel */
  async function loadRuns(selectLatest) {
    const runs = await api("GET", `/api/workflows/${id}/runs`).catch(() => []);
    D.runs = runs;
    if (selectLatest && runs[0]) D.run = runs[0];
    drawRuns();
  }
  function drawRuns() {
    const r = D.run;
    $("#runs").innerHTML = `<div class="head"><strong>Runs</strong><span class="muted small">${D.runs.length} recent</span><span style="flex:1"></span>
        <button class="sm ghost" id="refreshRuns">↻</button><button class="sm ghost" id="toggleRuns">${D.runsOpen ? "▾ Hide" : "▸ Show"}</button></div>
      ${D.runsOpen ? `<div class="content" style="display:grid;grid-template-columns:minmax(0,260px) minmax(0,1fr);gap:16px">
        <div>${D.runs.length ? D.runs.map((x) => `<div class="row" data-run="${x.id}" style="cursor:pointer;padding:4px 6px;border-radius:6px;${r && r.id === x.id ? "background:var(--accent-soft)" : ""}">
            ${statusPill(x.status)}<span class="grow small">${esc(x.trigger_type)}</span><span class="muted small">${ago(x.started_at)}</span></div>`).join("")
          : `<p class="muted small">No runs yet.</p>`}</div>
        <div>${r ? `<div class="row"><strong>Run ${esc(r.id)}</strong>${statusPill(r.status)}<span class="muted small">${esc(r.trigger_type)} · ${new Date(r.started_at).toLocaleString()}</span></div>
          ${r.error ? `<pre class="out" style="color:var(--bad)">${esc(r.error)}</pre>` : ""}
          ${r.steps.map((s) => `<div class="step ${s.status}"><strong>${esc(s.name)}</strong> <span class="muted small">${esc(s.type)} · ${s.ms ?? 0} ms${s.branch ? ` · branch ${s.branch}` : ""}</span>
            <pre class="out">${esc(s.error || JSON.stringify(s.output, null, 2))}</pre></div>`).join("")}
          <label>Output</label><pre class="out">${esc(JSON.stringify(r.output, null, 2))}</pre>` : `<p class="muted small">Select a run to see each step.</p>`}</div>
      </div>` : ""}`;
    $("#refreshRuns").addEventListener("click", () => loadRuns(false));
    $("#toggleRuns").addEventListener("click", () => { D.runsOpen = !D.runsOpen; drawRuns(); });
    $$("[data-run]").forEach((el) => el.addEventListener("click", () => { D.run = D.runs.find((x) => x.id === el.dataset.run); drawRuns(); draw(); }));
  }

  /* toolbar */
  async function save(quiet) {
    const body = { name: D.wf.name, description: D.wf.description, trigger: D.wf.trigger, nodes: D.wf.nodes, edges: D.wf.edges };
    const r = await attempt(() => api("PUT", `/api/workflows/${id}`, body), quiet ? null : "Saved");
    if (!r) return false;
    D.wf = r; D.dirty = false; header();
    return true;
  }
  $("#wfName").addEventListener("input", (e) => { D.wf.name = e.target.value; markDirty(); });
  if (canEdit) {
    $("#save").addEventListener("click", () => save(false));
    $("#validate").addEventListener("click", async () => {
      if (D.dirty && !(await save(true))) return;
      const v = await attempt(() => api("POST", `/api/workflows/${id}/validate`));
      if (v) v.valid ? toast("Looks good ✓") : modal(`<h2>Needs attention</h2><ul>${v.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><div class="actions"><button data-close class="primary">OK</button></div>`);
    });
    $("#activate").addEventListener("click", async () => {
      if (D.dirty && !(await save(true))) return;
      const r = await attempt(() => api("POST", `/api/workflows/${id}/${D.wf.active ? "deactivate" : "activate"}`), D.wf.active ? "Deactivated" : "Activated ⚡");
      if (r) { D.wf = r; header(); drawProps(); }
    });
    $("#runBtn").addEventListener("click", () => {
      modal(`<h2>Test run</h2><label>Input (JSON) — available as <code>{{input}}</code></label>
        <textarea id="runInput" rows="8" class="mono">${esc(D.sampleInput)}</textarea>
        <div class="actions"><button data-close>Cancel</button><button class="primary" id="go">▶ Run</button></div>`, (el, close) => {
        $("#go", el).addEventListener("click", async () => {
          let input;
          try { input = JSON.parse($("#runInput", el).value || "{}"); } catch (_) { toast("Input must be valid JSON", true); return; }
          D.sampleInput = $("#runInput", el).value; store(`studio.sample.${id}`, D.sampleInput);
          close();
          if (D.dirty && !(await save(true))) return;
          toast("Running…");
          const run = await attempt(() => api("POST", `/api/workflows/${id}/run`, { input }));
          if (run) {
            toast(run.status === "succeeded" ? "Run succeeded ✓" : `Run failed: ${run.error}`, run.status !== "succeeded");
            D.runsOpen = true; await loadRuns(true); draw();
          }
        });
      });
    });
  }
  $("#shareBtn").addEventListener("click", () => shareWorkflowDialog(D.wf));
  if ($("#del")) $("#del").addEventListener("click", () => confirmDialog(`Delete “${D.wf.name}”? Its run history is removed too.`, async () => {
    if (await attempt(() => api("DELETE", `/api/workflows/${id}`), "Workflow deleted")) { D.dirty = false; location.hash = "#/workflows"; }
  }));

  guardUnsaved(() => D.dirty);
  header(); draw(); loadRuns(false);
}

function shareWorkflowDialog(wf) {
  const isOwner = wf.role === "owner";
  modal(`<h2>Share “${esc(wf.name)}”</h2>
    <p class="muted">Editors can change, run and activate the workflow. Viewers can inspect its design and runs.</p>
    ${isOwner ? `<form class="row" id="shareForm"><input name="username" placeholder="username" list="userList2" required class="grow"><datalist id="userList2"></datalist>
      <select name="role" style="width:110px"><option value="viewer">Viewer</option><option value="editor">Editor</option></select><button class="primary">Share</button></form>` : ""}
    <div id="shareList" style="margin-top:12px"></div>
    <div class="actions"><button data-close>Done</button></div>`, (el) => {
    const load = async () => {
      const list = await attempt(() => api("GET", `/api/workflows/${wf.id}/shares`));
      if (list) renderShareList($("#shareList", el), list, isOwner, async (u) => {
        if (await attempt(() => api("DELETE", `/api/workflows/${wf.id}/shares/${encodeURIComponent(u)}`), "Access removed")) load();
      });
    };
    const form = $("#shareForm", el);
    if (form) {
      bindUserSuggest($("input[name=username]", form), $("#userList2", el));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (await attempt(() => api("POST", `/api/workflows/${wf.id}/shares`, Object.fromEntries(new FormData(form))), "Shared")) { form.reset(); load(); }
      });
    }
    load();
  });
}

/* ------------------------------------------------------------------ router */
let lastHash = location.hash;
async function route() {
  if (!S.token) { renderAuth(); return; }
  if (!S.user) {
    try { S.user = await api("GET", "/api/me"); } catch (_) { return; }
  }
  if (!S.meta) S.meta = await api("GET", "/api/meta");
  unsavedGuard = null;
  lastHash = location.hash;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const [section, id] = parts;
  if (section === "agents" && id) return renderAgentEditor(id);
  if (section === "agents") return renderAgents();
  if (section === "workflows" && id) return renderDesigner(id);
  if (section === "workflows") return renderWorkflows();
  if (section === "catalog") return renderCatalog();
  if (section === "chat" && id) return renderConsumerChat(id);
  return renderHome();
}

window.addEventListener("hashchange", () => {
  if (unsavedGuard && unsavedGuard() && !confirm("You have unsaved changes. Leave anyway?")) {
    history.replaceState(null, "", lastHash || "#/");
    return;
  }
  route();
});

S.token = store("studio.token");
route();
