/* Codeskys dashboard — single-page app over the Codeskys JSON API. */
(function () {
  "use strict";
  const svg = Icons.svg;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const layer = document.getElementById("layer");

  // ---------------------------------------------------------------- state
  const State = {
    user: null, status: null, notifUnread: 0,
    cache: {},
  };

  // ---------------------------------------------------------------- dom helpers
  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

  // ---------------------------------------------------------------- formatting
  function timeAgo(iso) {
    if (!iso) return "—";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 0) return "just now";
    if (s < 60) return s + "s ago";
    const m = Math.round(s / 60); if (m < 60) return m + "m ago";
    const h = Math.round(m / 60); if (h < 24) return h + "h ago";
    const dd = Math.round(h / 24); if (dd < 7) return dd + "d ago";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function dateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function duration(ms) {
    if (!ms && ms !== 0) return "—";
    const s = Math.round(ms / 1000);
    if (s < 60) return s + "s";
    return Math.floor(s / 60) + "m " + (s % 60) + "s";
  }
  const STATUS_LABEL = { ready: "Ready", failed: "Failed", building: "Building", queued: "Queued",
    cancelled: "Cancelled", expired: "Expired" };
  function expiryLabel(iso) {
    if (!iso) return null;
    const ms = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").getTime() - Date.now();
    if (ms <= 0) return "expired";
    const total = Math.max(1, Math.floor(ms / 60000));   // whole minutes left, never "60m"
    const h = Math.floor(total / 60), m = total % 60;
    if (h >= 48) return `expires in ${Math.floor(h / 24)}d ${h % 24}h`;
    return h >= 1 ? `expires in ${h}h ${m}m` : `expires in ${m}m`;
  }
  function simBadge(dep) {
    return dep && dep.is_simulated ? "<span class='badge warn' title='Built by the Codeskys simulator, not a real server'>Simulated</span>" : "";
  }
  // Where visitors reach the project: the live deployment's own URL (https://<slug>.apps.<domain>
  // for apps, /live/<slug>/ for uploaded sites); the built-in site page only without one.
  // Paid hosting (₹510): while it runs, the deployments are not limited to 24 hours.
  function hostingActive(iso) { return !!iso && new Date(iso).getTime() + 24 * 3600000 > Date.now(); }
  function liveUrl(p) { const d = p && p.production; return d && !d.is_simulated && d.url ? d.url : "/sites/" + p.slug + "/"; }
  function liveHost(p) { const u = liveUrl(p); return (/^https?:/.test(u) ? u.replace(/^https?:\/\//, "") : (typeof location !== "undefined" ? location.host : "codeskys.com") + u).replace(/\/$/, ""); }
  function isLifetime(iso) { return !!iso && new Date(iso).getFullYear() >= 2100; }
  // The address a visitor opens for a project (the real link, whichever way it was published).
  function liveHref(p) { return liveUrl(p); }
  function absUrl(u) { return u && u.startsWith("/") ? location.origin + u : u; }
  function openNew(u) { window.open(u, "_blank", "noopener,noreferrer"); }
  function shortDate(iso) { return iso ? dateTime(iso) : "—"; }
  // The one-line plan status shown at the top of a project: how long it stays live, or that the
  // free period has ended.
  function planStrip(p) {
    const a = p.access || {};
    if (a.admin) return el("div", "callout info", svg("shield") + "<div><b>Administrator account.</b> Free-plan limits and expiry do not apply here, so you can test freely.</div>");
    if (a.is_free === false) {
      if (a.lifetime) return el("div", "callout live", svg("check") + "<div><b>Lifetime hosting.</b> Your projects stay live for as long as you keep your account.</div>");
      if (a.expires_at) return el("div", "callout live", svg("check") + "<div><b>Paid hosting.</b> Your projects stay live until " + esc(dateTime(a.expires_at)) + ".</div>");
      return null;
    }
    if (a.blocked) {
      const c = el("div", "callout bad plan-strip");
      c.innerHTML = svg("alert") + "<div><b>Your free 24 hours have ended.</b> This project’s link now shows a 404 page. Upgrade to bring it back online.</div>";
      c.append(K.btn("Upgrade", { cls: "accent sm", icon: "bolt", href: "#/billing" }));
      return c;
    }
    if (a.expires_at) {
      const c = el("div", "callout info plan-strip");
      c.innerHTML = svg("clock") + "<div><b>Free live for 24 hours.</b> Expires <b>" + esc(dateTime(a.expires_at)) + "</b> (" + esc((expiryLabel(a.expires_at) || "").replace("expires ", "")) + "). " +
        "You can update and redeploy as many times as you like before then; the expiry stays the same.</div>";
      c.append(K.btn("Upgrade", { cls: "sm", icon: "bolt", href: "#/billing" }));
      return c;
    }
    return el("div", "callout info", svg("clock") + "<div><b>Free plan.</b> Your project goes live for 24 hours as soon as it is published.</div>");
  }
  function statusChip(s) { return '<span class="status ' + s + '"><i></i>' + (STATUS_LABEL[s] || s) + "</span>"; }
  function fwLogo(fw, cls) {
    const letters = { nextjs: "N", react: "⚛", cra: "⚛", vue: "V", nuxt: "N", svelte: "S", angular: "A", astro: "A",
      gatsby: "G", node: "⬡", flask: "Fl", fastapi: "Fa", streamlit: "St", django: "Dj", docker: "D", static: "{}" };
    return '<span class="fw-logo fw-' + fw + " " + (cls || "") + '">' + (letters[fw] || "{}") + "</span>";
  }

  // ---------------------------------------------------------------- toast
  const toasts = document.getElementById("toasts");
  function toast(msg, kind) {
    const t = el("div", "toast " + (kind === "error" ? "error" : ""));
    t.innerHTML = svg(kind === "error" ? "alert" : "check", "sm") + "<span>" + esc(msg) + "</span>";
    toasts.append(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .2s"; setTimeout(() => t.remove(), 220); }, 3600);
  }

  // ---------------------------------------------------------------- overlays
  function closeLayer() { clear(layer); document.removeEventListener("keydown", onLayerKey); }
  function onLayerKey(e) { if (e.key === "Escape") closeLayer(); }
  function openModal({ title, desc, body, foot, wide }) {
    clear(layer);
    const overlay = el("div", "overlay");
    const modal = el("div", "modal" + (wide ? " wide" : ""));
    const head = el("div", "modal-head", "<h3>" + esc(title) + "</h3>" + (desc ? "<p>" + esc(desc) + "</p>" : ""));
    const mbody = el("div", "modal-body");
    if (typeof body === "string") mbody.innerHTML = body; else if (body) mbody.append(body);
    modal.append(head, mbody);
    if (foot) { const mfoot = el("div", "modal-foot"); mfoot.append(...foot); modal.append(mfoot); }
    overlay.append(modal);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeLayer(); });
    layer.append(overlay);
    document.addEventListener("keydown", onLayerKey);
    const first = mbody.querySelector("input,select,textarea,button");
    if (first) setTimeout(() => first.focus(), 40);
    return { modal, body: mbody, close: closeLayer };
  }
  function confirmModal({ title, desc, body, confirmLabel, danger, onConfirm }) {
    const btn = el("button", "btn " + (danger ? "danger" : "accent"), esc(confirmLabel || "Confirm"));
    const cancel = el("button", "btn ghost", "Cancel");
    cancel.onclick = closeLayer;
    btn.onclick = async () => {
      btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Working…';
      try { await onConfirm(); closeLayer(); }
      catch (e) { toast(e.message, "error"); btn.disabled = false; btn.textContent = confirmLabel || "Confirm"; }
    };
    openModal({ title, desc, body: body || "", foot: [cancel, btn] });
    return btn;
  }
  function dropdown(anchor, items) {
    clear(layer);
    const overlay = el("div", "overlay");
    overlay.style.background = "transparent"; overlay.style.backdropFilter = "none"; overlay.style.padding = "0";
    const dd = el("div", "dropdown");
    const r = anchor.getBoundingClientRect();
    dd.style.top = r.bottom + 6 + "px";
    dd.style.left = Math.min(r.left, window.innerWidth - 240) + "px";
    items.forEach((it) => {
      if (it.sep) { dd.append(el("div", "sep")); return; }
      if (it.head) { dd.append(el("div", "head", "<b>" + esc(it.head) + "</b><span>" + esc(it.sub || "") + "</span>")); return; }
      const b = el("button", it.danger ? "danger" : "", (it.icon ? svg(it.icon, "sm") : "") + "<span>" + esc(it.label) + "</span>");
      b.onclick = () => { closeLayer(); it.action && it.action(); };
      dd.append(b);
    });
    overlay.append(dd);
    overlay.addEventListener("mousedown", (e) => { if (!dd.contains(e.target)) closeLayer(); });
    layer.append(overlay);
    document.addEventListener("keydown", onLayerKey);
  }

  // ---------------------------------------------------------------- routing
  const routes = [];
  function route(pattern, handler) {
    const keys = [];
    const rx = new RegExp("^" + pattern.replace(/:[^/]+/g, (m) => { keys.push(m.slice(1)); return "([^/]+)"; }) + "$");
    routes.push({ rx, keys, handler });
  }
  function parseHash() {
    let h = location.hash.replace(/^#\/?/, "");
    const [path, query] = h.split("?");
    const params = {};
    if (query) query.split("&").forEach((p) => { const [k, v] = p.split("="); params[decodeURIComponent(k)] = decodeURIComponent(v || ""); });
    return { path: path || "", params };
  }
  let currentNav = "overview";
  async function render() {
    const { path, params } = parseHash();
    for (const r of routes) {
      const m = r.rx.exec(path);
      if (m) {
        const args = {};
        r.keys.forEach((k, i) => (args[k] = decodeURIComponent(m[i + 1])));
        closeLayer();
        try { await r.handler(args, params); }
        catch (e) { if (e && e.status !== 401) showError(e); }
        $("#view").focus({ preventScroll: true });
        return;
      }
    }
    location.hash = "#/";
  }
  function go(hash) { location.hash = hash; }
  function showError(e) {
    setView(view(el("div", "", '<div class="empty"><div class="art">' + svg("alert") + '</div><h3>Something went wrong</h3><p>' + esc(e.message || "Unexpected error.") + '</p><button class="btn" onclick="location.reload()">Reload</button></div>')));
  }

  // ---------------------------------------------------------------- layout chrome
  function view(...nodes) { const c = el("div", "content"); c.append(...nodes); return c; }
  function viewWide(...nodes) { const c = el("div", "content wide"); c.append(...nodes); return c; }
  function setView(node) { const v = $("#view"); clear(v); v.append(node); v.scrollTop = 0; }
  function setCrumbs(parts) {
    const c = clear($("#crumbs"));
    parts.forEach((p, i) => {
      if (i) c.append(el("span", "sep", svg("chevron", "sm")));
      if (p.href !== undefined && i < parts.length - 1) {
        const a = el("a", i === 0 ? "hide-sm" : "", esc(p.label)); a.href = p.href; c.append(a);
      } else c.append(el("strong", "", esc(p.label)));
    });
  }
  function pageHead(title, desc, actions) {
    const h = el("div", "page-head");
    const left = el("div");
    left.append(el("h1", "", esc(title)));
    if (desc) left.append(el("p", "", esc(desc)));
    h.append(left);
    if (actions && actions.length) { const a = el("div", "actions"); a.append(...actions); h.append(a); }
    return h;
  }
  function btn(label, { cls, icon, onClick, href } = {}) {
    const b = el(href ? "a" : "button", "btn " + (cls || ""), (icon ? svg(icon, "sm") : "") + (label ? "<span>" + esc(label) + "</span>" : ""));
    // Anything that is not a page inside this dashboard (#/...) opens in a new tab: live sites,
    // domains, repositories and external pages never replace the dashboard.
    if (href) { b.href = href; if (!href.startsWith("#")) { b.target = "_blank"; b.rel = "noopener noreferrer"; } }
    if (onClick) b.onclick = onClick;
    return b;
  }
  function spin(b, label) { b.disabled = true; b.dataset.label = b.innerHTML; b.innerHTML = '<span class="spinner"></span> ' + (label || "Working…"); }
  function unspin(b) { if (b.dataset.label) b.innerHTML = b.dataset.label; b.disabled = false; }

  const NAV = [
    { id: "overview", label: "Overview", href: "#/", icon: "overview" },
    { id: "projects", label: "Projects", href: "#/projects", icon: "projects" },
    { id: "deployments", label: "Deployments", href: "#/deployments", icon: "deployments" },
    { id: "analytics", label: "Analytics", href: "#/analytics", icon: "analytics" },
    { id: "activity", label: "Activity", href: "#/activity", icon: "activity" },
    { id: "platform-suite", label: "Platform features", href: "#/platform-suite", icon: "overview" },
  ];
  const NAV_MANAGE = [
    { id: "platform", label: "Platform", href: "#/platform", icon: "shield", admin: true },
    { id: "team", label: "Team", href: "#/team", icon: "team", admin: true },
    { id: "integrations", label: "Integrations", href: "#/integrations", icon: "integrations", admin: true },
    { id: "settings", label: "Settings", href: "#/settings", icon: "settings" },
    { id: "payments", label: "Payments", href: "#/payments", icon: "billing", admin: true },
    { id: "billing", label: "Usage & Billing", href: "#/billing", icon: "billing" },
  ];

  function buildSidebar() {
    const s = clear($("#sidebar"));
    s.append(el("a", "brand", '<span class="brand-mark"><img src="/static/images/codeskys-mark.png" alt="" style="width:100%;height:100%;object-fit:contain"></span><span>Codeskys</span><small>BETA</small>'));
    const wsBtn = el("button", "ws-switch", '<span class="ws-avatar">' + esc((State.user.name[0] || "W")) + '</span><span><b>' +
      esc(State.user.name.split(" ")[0]) + "’s workspace</b><span>" + esc(planName(State.user.plan) + " plan") + "</span></span>" + svg("chevrondown", "sm"));
    wsBtn.onclick = () => go("#/billing");
    $(".brand", s).href = "#/";
    s.append(wsBtn);
    const mk = (items, label) => {
      if (label) s.append(el("div", "nav-label", label));
      const nav = el("nav", "nav");
      items.forEach((it) => {
        if (it.admin && State.user.role !== "admin") return;
        const a = el("a", it.id === currentNav ? "active" : "", svg(it.icon, "sm") + "<span>" + esc(it.label) + "</span>");
        a.href = it.href;
        if (it.id === "deployments" && State.buildingCount) a.append(el("span", "count", String(State.buildingCount)));
        if (it.id === "payments") { a.id = "nav-payments"; if (State.pendingPayments) a.append(el("span", "count hot", String(State.pendingPayments))); }
        nav.append(a);
      });
      s.append(nav);
    };
    mk(NAV);
    mk(NAV_MANAGE, "Manage");
    // Administrators see how many UPI payments are waiting for approval.
    if (State.user.role === "admin") {
      Api.get("/api/admin/upi?status=submitted").then((r) => {
        State.pendingPayments = (r.counts || {}).submitted || 0;
        const link = $("#nav-payments");
        if (link) { const old = $(".count", link); if (old) old.remove(); if (State.pendingPayments) link.append(el("span", "count hot", String(State.pendingPayments))); }
      }).catch(() => {});
    }
    // The build engine is an operator detail, so only administrators see it.
    if (State.user.role === "admin") {
      const foot = el("div", "side-foot");
      const prov = State.status && State.status.provider === "coolify";
      foot.append(el("div", "provider-pill", svg(prov ? "shield" : "bolt", "sm") +
        "<span>Engine: <b style='color:var(--ink)'>" + engineLabel(prov ? "coolify" : "simulator") + "</b></span>"));
      s.append(foot);
    }
  }
  function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }
  // Students never see the backend product name; administrators do.
  function engineLabel(provider) {
    if (provider !== "coolify") return cap(provider || "simulator");
    return State.user && State.user.role === "admin" ? "Coolify" : "Codeskys";
  }
  function setNav(id) { currentNav = id; buildSidebar(); }

  // ---------------------------------------------------------------- top bar wiring
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    const ic = $("#theme-icon");
    if (ic) ic.outerHTML = svg(t === "dark" ? "moon" : "sun").replace("<svg", '<svg id="theme-icon"');
    try { localStorage.setItem("kova-theme", t); } catch (_) {}
  }
  function initChrome() {
    try { applyTheme(localStorage.getItem("kova-theme") || "dark"); } catch (_) { applyTheme("dark"); }
    $("#theme-toggle").onclick = () => applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
    $("#burger").onclick = () => $("#shell").classList.toggle("nav-open");
    $("#view").addEventListener("click", (e) => { if ($("#shell").classList.contains("nav-open") && e.target.closest("a")) $("#shell").classList.remove("nav-open"); });
    document.addEventListener("click", (e) => { const a = e.target.closest("#sidebar a"); if (a) $("#shell").classList.remove("nav-open"); });
    $("#search-trigger").onclick = openPalette;
    $("#notif-btn").onclick = openNotifications;
    $("#avatar-btn").onclick = openAccountMenu;
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
    });
    $("#avatar-btn").textContent = State.user.name[0] || "C";
  }

  function openAccountMenu() {
    dropdown($("#avatar-btn"), [
      { head: State.user.name, sub: State.user.email },
      { label: "Account settings", icon: "user", action: () => go("#/settings?s=account") },
      { label: "API tokens", icon: "key", action: () => go("#/settings?s=tokens") },
      { label: document.documentElement.getAttribute("data-theme") === "dark" ? "Light theme" : "Dark theme", icon: "sun",
        action: () => applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark") },
      { sep: true },
      { label: "Sign out", icon: "logout", danger: true, action: async () => { await Api.post("/api/auth/logout"); location.assign("/login"); } },
    ]);
  }

  async function refreshNotifBadge() {
    try {
      const n = await Api.get("/api/notifications");
      State.notifUnread = n.unread;
      $("#notif-dot").classList.toggle("hidden", !n.unread);
    } catch (_) {}
  }
  async function openNotifications() {
    let data;
    try { data = await Api.get("/api/notifications"); } catch (e) { return toast(e.message, "error"); }
    const body = el("div");
    if (!data.notifications.length) body.innerHTML = '<div class="empty"><div class="art">' + svg("notifications") + '</div><h3>No notifications</h3><p>Deployment results and account activity will show up here.</p></div>';
    else {
      const ul = el("ul", "feed");
      data.notifications.forEach((n) => {
        const li = el("li", n.is_read ? "" : "unread");
        const kind = n.kind.includes("ready") ? "live" : n.kind.includes("failed") ? "bad" : "accent";
        li.innerHTML = '<span class="ico ' + kind + '">' + svg(kind === "bad" ? "alert" : "rocket", "sm") + '</span>' +
          '<div class="txt"><b>' + esc(n.title) + "</b><div class='muted small'>" + esc(n.body) + "</div></div>" +
          '<span class="when">' + timeAgo(n.created_at) + "</span>";
        if (n.link) { li.style.cursor = "pointer"; li.onclick = () => { closeLayer(); go(n.link.replace(/^#/, "#")); }; }
        ul.append(li);
      });
      body.append(ul);
    }
    const readAll = el("button", "btn ghost sm", "Mark all read");
    readAll.onclick = async () => { await Api.post("/api/notifications/read", {}); refreshNotifBadge(); closeLayer(); };
    openModal({ title: "Notifications", body, foot: [readAll, btn("Close", { cls: "accent", onClick: closeLayer })] });
  }

  // ---------------------------------------------------------------- command palette
  async function openPalette() {
    let projects = [];
    try { projects = (await Api.get("/api/projects")).projects; } catch (_) {}
    clear(layer);
    const overlay = el("div", "overlay");
    const modal = el("div", "modal palette");
    const input = el("input"); input.placeholder = "Search projects or jump to…"; input.autofocus = true;
    const inWrap = el("div", "pal-input", svg("search", "sm")); inWrap.append(input);
    const list = el("div", "pal-list");
    modal.append(inWrap, list);
    overlay.append(modal);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeLayer(); });
    layer.append(overlay);
    const commands = [
      { group: "Actions", label: "New project", icon: "plus", go: "#/new" },
      { group: "Go to", label: "Overview", icon: "overview", go: "#/" },
      { group: "Go to", label: "Projects", icon: "projects", go: "#/projects" },
      { group: "Go to", label: "Deployments", icon: "deployments", go: "#/deployments" },
      { group: "Go to", label: "Analytics", icon: "analytics", go: "#/analytics" },
      { group: "Go to", label: "Activity", icon: "activity", go: "#/activity" },
      { group: "Go to", label: "Settings", icon: "settings", go: "#/settings" },
    ];
    if (State.user.role === "admin") {
      commands.push({ group: "Go to", label: "Platform", icon: "shield", go: "#/platform" });
      commands.push({ group: "Go to", label: "Team", icon: "team", go: "#/team" });
      commands.push({ group: "Go to", label: "Integrations", icon: "integrations", go: "#/integrations" });
    }
    let active = 0, flat = [];
    function paint() {
      const q = input.value.trim().toLowerCase();
      clear(list); flat = [];
      const projMatches = projects.filter((p) => !q || p.name.toLowerCase().includes(q) || p.slug.includes(q));
      const cmdMatches = commands.filter((c) => !q || c.label.toLowerCase().includes(q));
      if (projMatches.length) {
        list.append(el("div", "pal-group", "Projects"));
        projMatches.slice(0, 6).forEach((p) => addItem({ icon: null, fw: p.framework, label: p.name, hint: p.slug, go: "#/projects/" + p.slug }));
      }
      let lastGroup = "";
      cmdMatches.forEach((c) => {
        if (c.group !== lastGroup) { list.append(el("div", "pal-group", c.group)); lastGroup = c.group; }
        addItem(c);
      });
      highlight();
    }
    function addItem(it) {
      const b = el("button", "pal-item", (it.fw ? fwLogo(it.fw) : svg(it.icon || "chevron", "sm")) + "<span>" + esc(it.label) + "</span>" + (it.hint ? '<span class="hint">' + esc(it.hint) + "</span>" : ""));
      const idx = flat.length;
      b.onmousemove = () => { active = idx; highlight(); };
      b.onclick = () => { closeLayer(); go(it.go); };
      list.append(b); flat.push(b);
    }
    function highlight() { flat.forEach((b, i) => b.classList.toggle("on", i === active)); if (flat[active]) flat[active].scrollIntoView({ block: "nearest" }); }
    input.addEventListener("input", () => { active = 0; paint(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, flat.length - 1); highlight(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
      else if (e.key === "Enter") { e.preventDefault(); if (flat[active]) flat[active].click(); }
      else if (e.key === "Escape") closeLayer();
    });
    paint(); setTimeout(() => input.focus(), 30);
  }

  // expose shared helpers to the rest of the module (loaded below)
  window.Codeskys = {
    State, el, esc, clear, svg, $, timeAgo, dateTime, duration, statusChip, fwLogo, toast, STATUS_LABEL,
    openModal, confirmModal, dropdown, closeLayer, route, go, render, view, viewWide, setView, setCrumbs,
    pageHead, btn, spin, unspin, setNav, cap, refreshNotifBadge,
  };

  // ================================================================ PAGES
  const K = window.Codeskys;

  function skeletonPage() { setView(view(el("div", "skel h", "&nbsp;"), el("div", "skel box"))); }

  function emptyState(icon, title, desc, action) {
    const e = el("div", "empty", '<div class="art">' + svg(icon) + "</div><h3>" + esc(title) + "</h3><p>" + esc(desc) + "</p>");
    if (action) e.append(action);
    return e;
  }

  function bars(daily) {
    const w = 100, h = 42, max = Math.max(1, ...daily.map((d) => d.ready + d.failed + d.other));
    const bw = w / daily.length;
    let r = '<svg class="chart" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" style="height:120px">';
    daily.forEach((d, i) => {
      const x = i * bw, total = d.ready + d.failed + d.other;
      let y = h;
      [["bar-ok", d.ready], ["bar-other", d.other], ["bar-bad", d.failed]].forEach(([c, v]) => {
        if (!v) return; const bh = (v / max) * (h - 4); y -= bh;
        r += '<rect class="' + c + '" x="' + (x + bw * 0.14) + '" y="' + y + '" width="' + bw * 0.72 + '" height="' + bh + '" rx="0.6"/>';
      });
    });
    return r + "</svg>";
  }

  function lineChart(points, labelFn) {
    const w = 320, h = 120, pad = 6;
    const max = Math.max(1, ...points.map((p) => p.v));
    const step = points.length > 1 ? (w - pad * 2) / (points.length - 1) : 0;
    const xy = points.map((p, i) => [pad + i * step, h - pad - (p.v / max) * (h - pad * 2 - 10)]);
    const line = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const area = line + " L" + xy[xy.length - 1][0].toFixed(1) + " " + (h - pad) + " L" + xy[0][0].toFixed(1) + " " + (h - pad) + " Z";
    let r = '<svg class="chart" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" style="height:150px">';
    r += '<defs><linearGradient id="kova-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity="0.28"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>';
    r += '<path class="area" d="' + area + '"/><path class="line" d="' + line + '"/>';
    xy.forEach((p) => (r += '<circle class="pt" cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="2.4"/>'));
    return r + "</svg>";
  }

  // ---------------------------------------------------------------- OVERVIEW
  K.route("", async () => {
    K.setNav("overview"); skeletonPage();
    const d = await Api.get("/api/overview");
    State.buildingCount = d.stats.building; State.status = State.status || {}; State.status.provider = d.provider;
    K.setNav("overview");
    K.setCrumbs([{ label: "Overview" }]);
    const hour = new Date().getHours();
    const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    const head = K.pageHead(greet + ", " + State.user.name.split(" ")[0], "Here’s what’s happening across your workspace.",
      [K.btn("Browse templates", { cls: "ghost", icon: "book", href: "#/new?tab=templates" }), K.btn("New project", { cls: "accent", icon: "plus", href: "#/new" })]);

    const s = d.stats;
    const stats = el("div", "stats");
    stats.append(
      stat("projects", "Projects", String(s.projects), s.live + " live"),
      stat("deployments", "Deploys · 30d", String(s.deploys_30d), s.building ? s.building + " in progress" : "All settled"),
      stat("analytics", "Success rate", s.success_rate == null ? "—" : s.success_rate + "%", "last 30 days"),
      stat("clock", "Avg build", s.avg_build_ms ? duration(s.avg_build_ms) : "—", s.build_minutes + " build min used"));

    const grid = el("div", "layout-2");
    // left: deploy activity + recent projects
    const left = el("div", "stack");
    const chartCard = el("div", "card");
    chartCard.append(el("div", "card-head", "<div><h3>Deployment activity</h3><p>Last 14 days</p></div>" +
      '<div class="actions"><span class="legend"><span><i class="ok"></i>Ready</span><span><i class="bad"></i>Failed</span><span><i class="other"></i>Other</span></span></div>'));
    const cp = el("div", "card-pad"); cp.innerHTML = bars(d.daily); chartCard.append(cp);
    left.append(chartCard);

    const projCard = el("div", "card");
    projCard.append(el("div", "card-head", "<div><h3>Recent projects</h3></div><div class='actions'></div>"));
    $(".actions", projCard.querySelector(".card-head")).append(K.btn("View all", { cls: "ghost sm", href: "#/projects" }));
    if (!d.projects.length) projCard.append(emptyState("rocket", "No projects yet", "Import a Git repository to create your first deployment.", K.btn("New project", { cls: "accent", icon: "plus", href: "#/new" })));
    else { const ul = el("div", "list"); d.projects.forEach((p) => ul.append(projectRow(p))); projCard.append(ul); }
    left.append(projCard);

    // right: recent deployments + plan
    const right = el("div", "stack");
    const recent = el("div", "card");
    recent.append(el("div", "card-head", "<div><h3>Latest deployments</h3></div>"));
    if (!d.recent.length) recent.append(el("div", "card-pad muted", "No deployments yet."));
    else { const ul = el("ul", "feed"); d.recent.forEach((dep) => ul.append(deployFeedItem(dep))); recent.append(ul); }
    right.append(recent);

    const planCard = el("div", "card card-pad");
    const lim = d.limits;
    planCard.innerHTML = "<div class='row'><h3 style='margin:0'>" + planName(d.plan) + " plan</h3><span class='spacer'></span>" +
      "<a class='link small' href='#/billing'>Manage</a></div>";
    const usg = el("div"); usg.style.marginTop = "14px";
    usg.append(usageRow("Projects", s.projects, lim.projects));
    usg.append(usageRow("Build minutes · 30d", Math.round(s.build_minutes), lim.build_minutes));
    usg.append(usageRow("Concurrent builds", s.building, lim.concurrent));
    planCard.append(usg);
    right.append(planCard);

    if (d.provider !== "coolify" && State.user.role === "admin") {
      const co = el("div", "callout info"); co.innerHTML = svg("info") + "<div>Deployments currently run on the <b>built-in simulator</b>. Connect your Coolify server in <a class='link' href='#/integrations'>Integrations</a> to run real builds.</div>";
      right.append(co);
    }

    grid.append(left, right);
    setView(view(head, stats, grid));
    refreshNotifBadge();
  });

  function stat(icon, label, value, foot) {
    return el("article", "card stat", '<div class="label">' + svg(icon, "sm") + esc(label) + "</div><div class='value'>" + esc(value) + "</div><div class='foot'>" + esc(foot) + "</div>");
  }
  function usageRow(label, used, total) {
    const pct = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
    const r = el("div"); r.style.margin = "10px 0";
    r.innerHTML = "<div class='row small muted' style='justify-content:space-between;margin-bottom:5px'><span>" + esc(label) + "</span><b style='color:var(--ink)'>" + used + " / " + total + "</b></div><div class='meter " + (pct > 85 ? "warn" : "") + "'><div style='width:" + pct + "%'></div></div>";
    return r;
  }
  function projectRow(p) {
    const a = el("a"); a.href = "#/projects/" + p.slug;
    const st = p.production ? p.production.status : p.latest ? p.latest.status : null;
    a.innerHTML = fwLogo(p.framework) + "<div class='txt' style='flex:1;min-width:0'><div class='row'><b class='ellipsis'>" + esc(p.name) + "</b>" +
      (p.is_paused ? "<span class='badge warn'>Paused</span>" : "") + "</div><div class='muted small ellipsis'>" + esc(liveHost(p)) + "</div></div>" +
      "<div class='row' style='gap:14px'>" + (st ? statusChip(st) : "<span class='muted small'>No deploys</span>") + "<span class='muted small' style='width:64px;text-align:right'>" + timeAgo(p.updated_at) + "</span></div>";
    return a;
  }
  function deployFeedItem(dep) {
    const li = el("li"); li.style.cursor = dep.is_latest === false ? "default" : "pointer";
    if (dep.is_latest === false) li.style.opacity = ".75";
    const kind = dep.status === "ready" ? "live" : dep.status === "failed" ? "bad" : "accent";
    li.innerHTML = "<span class='ico " + kind + "'>" + svg(dep.status === "failed" ? "alert" : "rocket", "sm") + "</span>" +
      "<div class='txt'><b>" + esc(dep.project_name) + "</b> <span class='muted small'>#" + dep.number + " · " + (dep.target) + "</span>" +
      "<div class='muted small ellipsis'>" + esc(dep.commit_message || dep.branch) + "</div></div>" +
      "<span class='when'>" + timeAgo(dep.created_at) + "</span>";
    if (dep.is_latest !== false) li.onclick = () => go("#/projects/" + dep.project_slug + "/deployments/" + dep.id);
    return li;
  }

  // ---------------------------------------------------------------- PROJECTS LIST
  K.route("projects", async (_a, params) => {
    K.setNav("projects"); skeletonPage();
    const scope = params.scope === "mine" ? "?scope=mine" : "";
    const d = await Api.get("/api/projects" + scope);
    K.setCrumbs([{ label: "Projects" }]);
    const head = K.pageHead("Projects", d.projects.length + (d.projects.length === 1 ? " project" : " projects") + " in your workspace",
      [K.btn("New project", { cls: "accent", icon: "plus", href: "#/new" })]);

    const toolbar = el("div", "toolbar");
    const search = el("div", "search", svg("search", "sm"));
    const input = el("input", "input"); input.placeholder = "Search projects…"; input.value = params.q || "";
    search.append(input); toolbar.append(search);
    const fwSel = el("select", "select"); fwSel.style.maxWidth = "170px";
    fwSel.innerHTML = "<option value=''>All frameworks</option>" + [...new Set(d.projects.map((p) => p.framework))].map((f) => "<option value='" + f + "'>" + esc(d.projects.find((p) => p.framework === f).framework_name) + "</option>").join("");
    toolbar.append(fwSel);
    if (State.user.role === "admin") {
      const seg = el("div", "seg");
      const all = el("button", scope ? "" : "on", "All"); const mine = el("button", scope ? "on" : "", "Mine");
      all.onclick = () => go("#/projects"); mine.onclick = () => go("#/projects?scope=mine");
      seg.append(all, mine); toolbar.append(seg);
    }

    const grid = el("div", "project-grid");
    const empty = emptyState("rocket", "No projects yet", "Import a Git repository and Codeskys will build and deploy it automatically.", K.btn("Import a repository", { cls: "accent", icon: "git", href: "#/new" }));
    function paint() {
      const q = input.value.trim().toLowerCase(); const fw = fwSel.value;
      clear(grid);
      const items = d.projects.filter((p) => (!q || p.name.toLowerCase().includes(q) || p.slug.includes(q) || (p.repo_full_name || "").toLowerCase().includes(q)) && (!fw || p.framework === fw));
      if (!d.projects.length) { setBody(empty); return; }
      if (!items.length) { setBody(el("div", "card card-pad muted", "No projects match your filters.")); return; }
      items.forEach((p) => grid.append(projectCard(p)));
      setBody(grid);
    }
    let bodyNode;
    function setBody(node) { if (bodyNode) bodyNode.replaceWith(node); bodyNode = node; }
    bodyNode = grid;
    input.oninput = paint; fwSel.onchange = paint;
    setView(view(head, toolbar, grid)); paint();
  });

  function projectCard(p) {
    const card = el("div", "pcard");
    const top = el("div", "top", fwLogo(p.framework, "lg") + "<div style='flex:1;min-width:0'><div class='name ellipsis'>" + esc(p.name) + "</div><div class='url ellipsis'>" + esc(p.repo_full_name || p.repo_url) + "</div></div>");
    const menu = el("button", "icon-btn", svg("dots", "sm")); menu.style.width = "30px"; menu.style.height = "30px";
    menu.onclick = (e) => { e.preventDefault(); e.stopPropagation(); projectMenu(menu, p); };
    top.append(menu);
    card.append(top);
    const st = p.production ? p.production.status : p.latest ? p.latest.status : null;
    const thumb = el("div", "preview-thumb " + (st === "failed" && !p.production ? "failed" : p.production ? "" : "empty"));
    if (p.production) thumb.innerHTML = "<div class='bar'></div><div class='l1'></div><div class='l2'></div><div class='l3'></div>";
    else thumb.textContent = "No production deployment";
    card.append(thumb);
    const body = el("div", "body");
    body.append(el("div", "row", (st ? statusChip(st) : "<span class='muted small'>Not deployed</span>") + "<span class='spacer'></span>" + (p.is_paused ? "<span class='badge warn'>Paused</span>" : "")));
    card.append(body);
    const foot = el("div", "foot", svg("branch", "sm") + "<span>" + esc(p.branch) + "</span><span class='spacer'></span>" + svg("clock", "sm") + "<span>" + timeAgo(p.updated_at) + "</span>");
    card.append(foot);
    card.style.cursor = "pointer";
    card.onclick = () => go("#/projects/" + p.slug);
    return card;
  }
  function projectMenu(anchor, p) {
    K.dropdown(anchor, [
      { label: "Open", icon: "external", action: () => go("#/projects/" + p.slug) },
      { label: "Deploy now", icon: "rocket", action: () => triggerDeploy(p.slug) },
      { label: "Settings", icon: "settings", action: () => go("#/projects/" + p.slug + "/settings") },
      { label: "Visit Live Site", icon: "globe", action: () => (p.production && p.production.status === "ready" ? openNew(liveUrl(p)) : toast("This project is not live yet.", "error")) },
    ]);
  }
  async function triggerDeploy(slug, opts) {
    try { const r = await Api.post("/api/projects/" + slug + "/deploy", opts || {}); toast("Deployment started"); go("#/projects/" + slug + "/deployments/" + r.deployment.id); }
    catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- NEW PROJECT
  K.route("new", async (_a, params) => {
    K.setNav("projects");
    K.setCrumbs([{ label: "Projects", href: "#/projects" }, { label: "New project" }]);
    const fwRes = await Api.get("/api/frameworks");
    const fw = fwRes.frameworks;
    State.maxUploadMb = fwRes.max_upload_mb || 100;
    const tab = ["templates", "connected", "upload"].includes(params.tab) ? params.tab : "import";
    const head = K.pageHead("Start a new project", "Upload your website files, import a Git repository, or begin from a template.");
    const tabs = el("div", "tabs");
    const tConn = el("a", tab === "connected" ? "on" : "", "My repositories"); tConn.href = "#/new?tab=connected";
    const tImport = el("a", tab === "import" ? "on" : "", "Paste a URL"); tImport.href = "#/new";
    const tTpl = el("a", tab === "templates" ? "on" : "", "Templates"); tTpl.href = "#/new?tab=templates";
    const tUp = el("a", tab === "upload" ? "on" : "", "Upload files"); tUp.href = "#/new?tab=upload";
    tabs.append(tUp, tConn, tImport, tTpl);
    const wrap = el("div");
    setView(view(head, tabs, wrap));
    if (tab === "templates") renderTemplates(wrap, fw);
    else if (tab === "upload") renderUpload(wrap);
    else if (tab === "connected") renderConnected(wrap, fw);
    else renderImport(wrap, fw, params.repo, params.private === "1");
  });

  // ---------------------------------------------------------------- FILE UPLOAD
  function fmtBytes(n) { return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB"; }

  // A drag-and-drop zip chooser shared by "new project" and "upload new version".
  // Accepts a .zip OR a whole project folder (chosen or dragged in). A folder is packed into a
  // zip in the browser (see zipper.js) just before it is sent. `ready()` gives the File to send.
  function zipPicker() {
    const box = el("label", "drop");
    const input = el("input"); input.type = "file"; input.accept = ".zip,application/zip"; input.hidden = true;
    const dirInput = el("input"); dirInput.type = "file"; dirInput.hidden = true; dirInput.multiple = true;
    dirInput.setAttribute("webkitdirectory", ""); dirInput.setAttribute("directory", "");
    const msg = el("div", "drop-msg");
    box.append(input, msg);
    const links = el("div", "drop-links");
    const pickDir = el("button", "link-btn", "choose a folder"); pickDir.type = "button";
    const pickZip = el("button", "link-btn", "choose a .zip"); pickZip.type = "button";
    links.append(document.createTextNode("Or "), pickDir, document.createTextNode(" · "), pickZip);
    links.addEventListener("click", (e) => e.stopPropagation());
    pickDir.onclick = (e) => { e.preventDefault(); e.stopPropagation(); dirInput.click(); };
    pickZip.onclick = (e) => { e.preventDefault(); e.stopPropagation(); input.click(); };
    let file = null, folder = null;   // folder = { entries, rootName }
    const maxMb = () => State.maxUploadMb || 100;
    function show() {
      if (folder) {
        const bytes = folder.entries.reduce((n, e) => n + e.file.size, 0);
        msg.innerHTML = svg("check") + "<div><b>" + esc(folder.rootName || "Your folder") + "</b><div class='muted small'>" + folder.entries.length + " files · " + fmtBytes(bytes) +
          (folder.skipped ? " · node_modules and hidden system files left out" : "") + " · click to choose again</div></div>";
      } else if (file) {
        msg.innerHTML = svg("check") + "<div><b>" + esc(file.name) + "</b><div class='muted small'>" + fmtBytes(file.size) + " · click to choose a different file</div></div>";
      } else {
        msg.innerHTML = svg("upload") + "<div><b>Drop your project folder or .zip here</b><div class='muted small'>or click to choose a .zip</div></div>";
      }
    }
    function setZip(f) {
      if (f && !f.name.toLowerCase().endsWith(".zip")) { toast("That isn’t a .zip file. Drop a .zip, or drop your whole project folder.", "error"); return; }
      file = f || null; folder = null; show();
    }
    function setFolder(res, hadSkips) {
      if (!res.entries.length) { toast("That folder has no files to upload.", "error"); return; }
      res.skipped = hadSkips; folder = res; file = null; show();
    }
    input.onchange = () => setZip(input.files[0]);
    dirInput.onchange = () => { const all = dirInput.files.length; const r = ZipTools.fromInput(dirInput.files); setFolder(r, r.entries.length < all); dirInput.value = ""; };
    ["dragenter", "dragover"].forEach((ev) => box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.remove("over"); }));
    box.addEventListener("drop", async (e) => {
      const items = [...(e.dataTransfer.items || [])];
      const hasDir = items.some((i) => i.webkitGetAsEntry && i.webkitGetAsEntry() && i.webkitGetAsEntry().isDirectory);
      if (hasDir) {
        msg.innerHTML = "<span class='spinner'></span><div><b>Reading your folder…</b></div>";
        try { const r = await ZipTools.fromDrop(e.dataTransfer); setFolder(r, true); } catch (err) { toast("Couldn’t read that folder. Try ‘choose a folder’ instead.", "error"); show(); }
      } else setZip(e.dataTransfer.files[0]);
    });
    show();
    const node = el("div"); node.append(box, links, dirInput);
    return {
      node,
      has: () => !!(file || folder),
      // The file to send: the chosen zip, or the folder packed into a zip.
      async ready(onPack) {
        if (file) return file;
        if (!folder) return null;
        const total = folder.entries.reduce((n, e) => n + e.file.size, 0);
        if (total > maxMb() * 1024 * 1024) throw new Error("This folder is " + fmtBytes(total) + ". The limit is " + maxMb() + " MB. Remove large files such as videos or node_modules and try again.");
        const blob = await ZipTools.build(folder.entries, onPack);
        return new File([blob], (folder.rootName || "project") + ".zip", { type: "application/zip" });
      },
    };
  }

  function progressBar() {
    const wrap = el("div", "upload-progress"); wrap.hidden = true;
    const bar = el("div", "bar"); const fill = el("div", "fill"); bar.append(fill);
    const txt = el("div", "muted small"); wrap.append(bar, txt);
    return { node: wrap, set(f) { wrap.hidden = false; fill.style.width = Math.round(f * 100) + "%"; txt.textContent = f < 1 ? "Uploading… " + Math.round(f * 100) + "%" : "Upload complete — starting your build…"; } };
  }

  function renderUpload(wrap) {
    const card = el("div", "card card-pad");
    card.innerHTML = "<h3 style='margin:0 0 4px'>Upload your website files</h3>" +
      "<p class='muted' style='margin:0 0 16px'>Drop your project <b>folder</b> or a <b>.zip</b> — HTML, CSS and JavaScript, or the built <b>dist</b> / <b>build</b> folder of a React, Vue or Vite project. Up to <b>" + (State.maxUploadMb || 100) + " MB</b>. Your link goes live in moments.</p>";
    const nf = el("div", "field"); nf.innerHTML = "<label>Project name</label>";
    const ni = el("input", "input"); ni.placeholder = "My portfolio"; nf.append(ni);
    const lf = el("div", "field"); lf.innerHTML = "<label>Link name <span class='muted small'>(optional)</span></label>";
    const li = el("input", "input mono"); li.placeholder = "rahul-portfolio"; lf.append(li);
    const hint = el("div", "muted small", "Your link will be built from your project name."); lf.append(hint);
    li.oninput = () => { hint.textContent = li.value.trim() ? "Your link: …/" + li.value.trim().toLowerCase() : "Your link will be built from your project name."; };
    const picker = zipPicker(); const prog = progressBar();
    const go1 = el("button", "btn accent", "Upload & go live");
    go1.style.marginTop = "14px";
    go1.onclick = async () => {
      if (!ni.value.trim()) return toast("Give your project a name.", "error");
      if (!picker.has()) return toast("Choose your project folder or a .zip first.", "error");
      spin(go1, "Preparing…");
      try {
        const upFile = await picker.ready();
        const form = new FormData();
        form.append("name", ni.value.trim()); form.append("slug", li.value.trim()); form.append("file", upFile);
        spin(go1, "Uploading…");
        const r = await Api.upload("/api/projects/upload", form, prog.set);
        toast("Files received");
        go("#/projects/" + r.project.slug + "/deployments/" + r.deployment_id);
      } catch (e) { toast(e.message, "error"); unspin(go1); }
    };
    const ff = el("div", "field"); ff.innerHTML = "<label>Your files</label>"; ff.append(picker.node);
    card.append(nf, lf, ff, prog.node, go1);
    wrap.append(card);
  }

  // Upload a new version of an uploaded project (free projects keep their original 24 hours).
  function openUploadModal(p) {
    const b = el("div");
    b.append(el("p", "muted", "Upload a new folder or .zip to replace your live site. The link stays the same, and the new version appears as soon as it is ready."));
    const picker = zipPicker(); const prog = progressBar();
    b.append(picker.node, prog.node);
    const go1 = el("button", "btn accent", "Upload & update");
    go1.onclick = async () => {
      if (!picker.has()) return toast("Choose your project folder or a .zip first.", "error");
      spin(go1, "Preparing…");
      try {
        const form = new FormData(); form.append("file", await picker.ready());
        spin(go1, "Uploading…");
        const r = await Api.upload("/api/projects/" + p.slug + "/upload", form, prog.set);
        K.closeLayer(); toast("Files received");
        go("#/projects/" + p.slug + "/deployments/" + r.deployment.id);
      } catch (e) { toast(e.message, "error"); unspin(go1); }
    };
    openModal({ title: "Upload new files", desc: p.name, body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), go1] });
  }

  const TEMPLATES = [
    { name: "Next.js Starter", fw: "nextjs", desc: "Full-stack React framework with server rendering and file-based routing.", repo: "https://github.com/vercel/next-learn-starter", tags: ["React", "SSR"] },
    { name: "Vite + React", fw: "react", desc: "Lightning-fast React single-page app bundled with Vite.", repo: "https://github.com/vitejs/vite-plugin-react", tags: ["React", "SPA"] },
    { name: "Vue 3 SPA", fw: "vue", desc: "Modern Vue 3 single-page application with the composition API.", repo: "https://github.com/coollabsio/coolify-examples", tags: ["Vue"] },
    { name: "SvelteKit", fw: "svelte", desc: "The fastest way to build Svelte apps with routing and SSR.", repo: "https://github.com/sveltejs/template", tags: ["Svelte"] },
    { name: "Nuxt", fw: "nuxt", desc: "Intuitive Vue framework for server-rendered applications.", repo: "https://github.com/nuxt/starter", tags: ["Vue", "SSR"] },
    { name: "Astro Blog", fw: "astro", desc: "Content-focused static site with islands architecture.", repo: "https://github.com/coollabsio/coolify-examples", tags: ["Static"] },
    { name: "Gatsby", fw: "gatsby", desc: "React-based static site generator with a rich plugin ecosystem.", repo: "https://github.com/gatsbyjs/gatsby-starter-default", tags: ["React", "Static"] },
    { name: "Node.js API", fw: "node", desc: "Express-style Node.js server ready to deploy as a web service.", repo: "https://github.com/heroku/node-js-getting-started", tags: ["Node", "API"] },
    { name: "Python Flask", fw: "flask", desc: "Minimal Python web app served with Gunicorn.", repo: "https://github.com/heroku/python-getting-started", tags: ["Python"] },
    { name: "Static HTML", fw: "static", desc: "A plain HTML/CSS/JS site served straight from your repo.", repo: "https://github.com/cloudacademy/static-website-example", tags: ["Static"] },
  ];
  function renderTemplates(wrap, fw) {
    const grid = el("div", "tpl-grid");
    TEMPLATES.forEach((t) => {
      const c = el("div", "card tpl");
      c.append(el("div", "row", fwLogo(t.fw, "lg") + "<div><h4>" + esc(t.name) + "</h4></div>"));
      c.append(el("p", "", esc(t.desc)));
      c.append(el("div", "tags", t.tags.map((x) => "<span class='badge'>" + esc(x) + "</span>").join("")));
      const use = K.btn("Use template", { cls: "accent block", icon: "rocket", onClick: () => go("#/new?repo=" + encodeURIComponent(t.repo)) });
      c.append(use);
      grid.append(c);
    });
    wrap.append(grid);
  }

  async function renderConnected(wrap, fw) {
    const card = el("div", "card");
    card.append(el("div", "card-head", "<div><h3>Your repositories</h3><p>Pick a repository from a connected account — private ones included.</p></div>"));
    const body = el("div", "card-pad");
    card.append(body);
    wrap.append(card);
    let provs = [];
    try { provs = (await Api.get("/api/git/providers")).providers; }
    catch (e) { body.append(el("p", "muted", "Could not load providers.")); return; }
    const connected = provs.filter((p) => p.connected);
    if (!connected.length) {
      clear(body);
      const box = emptyState("git", "No Git account connected",
        "Connect GitHub or GitLab to browse your repositories and deploy private ones.");
      const btns = el("div", "np-connect");
      provs.forEach((pr) => btns.append(K.btn("Connect your " + pr.label + " account", { cls: pr.provider === "github" ? "accent" : "", icon: "git", onClick: () => connectGit(pr) })));
      box.append(btns);
      body.append(box);
      return;
    }
    const bar = el("div", "toolbar");
    const seg = el("div", "seg");
    let active = connected[0].provider;
    connected.forEach((p) => {
      const b = el("button", p.provider === active ? "on" : "", esc(p.label) + " · " + esc(p.username));
      b.onclick = () => { active = p.provider; seg.querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); load(); };
      seg.append(b);
    });
    const search = el("div", "search", svg("search", "sm"));
    const input = el("input", "input"); input.placeholder = "Filter repositories…";
    search.append(input);
    bar.append(seg, search);
    const list = el("div");
    clear(body); body.append(bar, list);
    let repos = [];
    function paint() {
      const q = input.value.trim().toLowerCase();
      clear(list);
      const items = repos.filter((r) => !q || r.full_name.toLowerCase().includes(q));
      if (!items.length) { list.append(el("p", "muted", "No repositories match.")); return; }
      items.slice(0, 60).forEach((r) => {
        const row = el("div", "row");
        row.style.cssText = "padding:12px 0;border-top:1px solid var(--line);gap:12px";
        row.innerHTML = svg("git", "sm") + "<div style='flex:1;min-width:0'><div class='row'><b class='ellipsis'>" +
          esc(r.full_name) + "</b>" + (r.private ? "<span class='badge'>Private</span>" : "") +
          (r.language ? "<span class='badge'>" + esc(r.language) + "</span>" : "") +
          "</div><div class='muted small ellipsis'>" + esc(r.description || "No description") + "</div></div>" +
          "<span class='muted small'>" + timeAgo(r.updated_at) + "</span>";
        row.append(K.btn("Import", { cls: "accent sm", icon: "rocket", onClick: () => importRepo(r, active, fw) }));
        list.append(row);
      });
    }
    input.oninput = paint;
    async function load() {
      clear(list); list.append(el("div", "skel h"), el("div", "skel h"));
      try { repos = (await Api.get("/api/git/" + active + "/repos")).repos; paint(); }
      catch (e) { clear(list); list.append(el("div", "callout bad", svg("alert") + "<div>" + esc(e.message) + "</div>")); }
    }
    load();
  }

  async function importRepo(repo, provider, fw) {
    const b = el("div");
    b.innerHTML = "<div class='repo-preview'>" + svg("git", "lg") + "<div style='flex:1;min-width:0'><b>" +
      esc(repo.full_name) + "</b>" + (repo.private ? " <span class='badge'>Private</span>" : "") +
      "<div class='muted small ellipsis'>" + esc(repo.description || "No description") + "</div></div></div>";
    const nf = el("div", "field"); nf.innerHTML = "<label>Project name</label>";
    const ni = el("input", "input"); ni.value = repo.name; nf.append(ni);
    const bf = el("div", "field"); bf.innerHTML = "<label>Branch</label>";
    const bs = el("select", "select"); bs.innerHTML = "<option>" + esc(repo.default_branch) + "</option>"; bf.append(bs);
    const ff = el("div", "field"); ff.innerHTML = "<label>Framework preset</label>";
    const fs = el("select", "select");
    fs.innerHTML = fw.map((f) => "<option value='" + f.id + "'>" + esc(f.name) + "</option>").join("");
    ff.append(fs);
    const g = el("div", "grid-2"); g.append(nf, bf);
    b.append(g, ff);
    const go1 = el("button", "btn accent", "Create & Deploy");
    openModal({ title: "Import " + repo.name, body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), go1] });
    // real branch list from the provider
    try {
      const { branches } = await Api.get("/api/git/" + provider + "/branches?repo=" + encodeURIComponent(repo.full_name));
      bs.innerHTML = branches.map((x) => "<option " + (x === repo.default_branch ? "selected" : "") + ">" + esc(x) + "</option>").join("");
    } catch (_) {}
    go1.onclick = async () => {
      spin(go1, "Creating…");
      try {
        const r = await Api.post("/api/projects", {
          name: ni.value.trim(), repo_url: repo.url, branch: bs.value, framework: fs.value,
          git_provider: provider, repo_external_id: repo.external_id, is_private: repo.private, deploy: true,
        });
        K.closeLayer(); toast("Project created");
        if (r.deployment_id) go("#/projects/" + r.project.slug + "/deployments/" + r.deployment_id);
        else go("#/projects/" + r.project.slug);
      } catch (e) { toast(e.message, "error"); unspin(go1); }
    };
  }

  function renderImport(wrap, fw, prefillRepo) {
    const hero = el("div", "import-hero");
    const form = el("div", "card card-pad");
    const side = el("div", "card card-pad");
    hero.append(form, side);
    wrap.append(hero);

    const model = { repo: null, framework: "static", name: "", slug: "", branch: "main", env: [],
      install_command: "", build_command: "", output_dir: "", start_command: "", root_dir: "", port: "", deploy: true };

    form.innerHTML = "<h3 style='margin:0 0 4px'>Import Git repository</h3><p class='muted' style='margin:0 0 16px'>Paste a public GitHub URL to get started.</p>";
    const repoField = el("div", "field");
    repoField.innerHTML = "<label>Repository URL</label>";
    const repoGroup = el("div", "input-group right");
    const repoInput = el("input", "input mono"); repoInput.placeholder = "https://github.com/owner/repo"; repoInput.value = prefillRepo || "";
    const repoBtn = el("button", "btn accent", "Fetch");
    repoGroup.append(repoInput, repoBtn); repoField.append(repoGroup);
    const repoHint = el("div", "hint", "Codeskys reads public repository metadata to detect the framework."); repoField.append(repoHint);
    form.append(repoField);

    const detail = el("div"); form.append(detail);

    async function fetchRepo() {
      const url = repoInput.value.trim();
      if (!url) return;
      spin(repoBtn, "Fetching…");
      try {
        const { repo } = await Api.get("/api/git/inspect?url=" + encodeURIComponent(url));
        model.repo = repo; model.framework = repo.framework || "static";
        model.name = repo.repo; model.branch = repo.default_branch || "main"; if (repo.root_dir) model.root_dir = repo.root_dir;
        applyFrameworkDefaults();
        paintDetail(repo);
      } catch (e) { toast(e.message, "error"); }
      finally { unspin(repoBtn); }
    }
    repoBtn.onclick = fetchRepo;
    repoInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); fetchRepo(); } });

    function applyFrameworkDefaults() {
      const f = fw.find((x) => x.id === model.framework) || {};
      model.install_command = f.install || ""; model.build_command = f.build || "";
      model.output_dir = f.output || ""; model.start_command = f.start || ""; model.port = f.port || "";
    }

    function paintDetail(repo) {
      clear(detail);
      if (repo.reachable === false) {
        const c = el("div", "callout warn"); c.dataset.keep = "1"; c.innerHTML = svg("info") + "<div>Couldn’t read this repository from GitHub right now. You can still deploy it — pick the framework and build settings below.</div>";
        detail.append(c);
      } else {
        const prev = el("div", "repo-preview");
        prev.innerHTML = svg("git", "lg") + "<div style='flex:1;min-width:0'><div class='row'><b>" + esc(repo.full_name) + "</b>" + (repo.language ? "<span class='badge'>" + esc(repo.language) + "</span>" : "") + "</div>" +
          "<div class='muted small ellipsis'>" + esc(repo.description || "No description") + "</div>" +
          (repo.latest_commit ? "<div class='muted small' style='margin-top:4px'>" + svg("history", "sm") + " " + esc(repo.latest_commit.message) + "</div>" : "") + "</div>";
        detail.append(prev);
      }
      // name + branch
      const g = el("div", "grid-2");
      g.append(field("Project name", (i) => { i.value = model.name; i.oninput = () => (model.name = i.value); }),
        selField("Branch", (repo.branches || [model.branch]), model.branch, (v) => (model.branch = v)));
      detail.append(g);

      detail.append(el("label", "field", "<label>Framework preset</label>"));
      const pick = el("div", "fw-pick");
      fw.forEach((f) => {
        const o = el("button", "fw-opt" + (f.id === model.framework ? " on" : ""), fwLogo(f.id) + "<span>" + esc(f.name) + "</span>");
        o.onclick = () => { model.framework = f.id; applyFrameworkDefaults(); pick.querySelectorAll(".fw-opt").forEach((x) => x.classList.remove("on")); o.classList.add("on"); refreshAdv(); };
        pick.append(o);
      });
      detail.append(pick);

      const adv = el("details", "adv");
      adv.innerHTML = "<summary>" + svg("chevron", "sm") + "Build & output settings</summary>";
      const inner = el("div", "inner"); adv.append(inner);
      function refreshAdv() {
        clear(inner);
        inner.append(
          field("Root directory", (i) => { i.placeholder = "./"; i.value = model.root_dir; i.oninput = () => (model.root_dir = i.value); }, model.root_dir, true),
          field("Install command", (i) => { i.value = model.install_command; i.oninput = () => (model.install_command = i.value); }, "", true),
          field("Build command", (i) => { i.value = model.build_command; i.oninput = () => (model.build_command = i.value); }, "", true));
        const g2 = el("div", "grid-2");
        g2.append(field("Output directory", (i) => { i.value = model.output_dir; i.oninput = () => (model.output_dir = i.value); }, "", true),
          field("Port", (i) => { i.type = "number"; i.value = model.port; i.oninput = () => (model.port = i.value); }));
        inner.append(g2);
        inner.append(field("Start command", (i) => { i.value = model.start_command; i.placeholder = "(static sites need none)"; i.oninput = () => (model.start_command = i.value); }, "", true));
      }
      refreshAdv(); detail.append(adv);

      // env vars
      const envWrap = el("div", "field");
      envWrap.innerHTML = "<label>Environment variables</label><div class='hint'>Available to your build and runtime. Add secrets like API keys here.</div>";
      const envList = el("div"); envWrap.append(envList);
      function addEnvRow(k, v) {
        const row = el("div", "env-row");
        const ik = el("input", "input mono"); ik.placeholder = "KEY"; ik.value = k || "";
        const iv = el("input", "input mono"); iv.placeholder = "value"; iv.value = v || "";
        const del = el("button", "icon-btn", svg("x", "sm")); del.style.width = "34px"; del.style.height = "36px";
        del.onclick = () => row.remove();
        row.append(ik, iv, del); envList.append(row);
        return { ik, iv };
      }
      const addBtn = K.btn("Add variable", { cls: "ghost sm", icon: "plus", onClick: () => addEnvRow() });
      envWrap.append(addBtn);
      detail.append(envWrap);
      model._collectEnv = () => Array.from(envList.querySelectorAll(".env-row")).map((r) => {
        const [k, v] = r.querySelectorAll("input"); return { key: k.value.trim(), value: v.value };
      }).filter((e) => e.key);

      const deployCheck = el("label", "check");
      deployCheck.innerHTML = "<input type='checkbox' checked><div><b>Deploy immediately</b><span>Build and publish as soon as the project is created.</span></div>";
      deployCheck.querySelector("input").onchange = (e) => (model.deploy = e.target.checked);
      detail.append(deployCheck);

      const create = K.btn("Create & Deploy", { cls: "accent lg", icon: "rocket" });
      create.onclick = async () => {
        if (!model.name.trim()) return toast("Give your project a name.", "error");
        spin(create, "Creating…");
        try {
          const payload = { name: model.name, repo_url: model.repo.url, framework: model.framework, branch: model.branch,
            install_command: model.install_command, build_command: model.build_command, output_dir: model.output_dir,
            start_command: model.start_command, root_dir: model.root_dir, port: model.port || undefined,
            env: model._collectEnv(), deploy: model.deploy };
          const r = await Api.post("/api/projects", payload);
          if (r.deploy_error) toast("Project created, but it could not be deployed: " + r.deploy_error, "error");
          else toast("Project created");
          if (r.deployment_id) go("#/projects/" + r.project.slug + "/deployments/" + r.deployment_id);
          else go("#/projects/" + r.project.slug);
        } catch (e) { toast(e.message, "error"); unspin(create); }
      };
      const foot = el("div", "row"); foot.style.marginTop = "18px"; foot.append(create, K.btn("Cancel", { cls: "ghost", href: "#/projects" }));
      detail.append(foot);
    }

    // side panel
    side.innerHTML = "<h3 style='margin:0 0 14px'>How it works</h3>";
    const steps = el("ol", "steps-v");
    [["Connect", "Paste a public Git repository URL."], ["Configure", "Codeskys detects your framework and fills in build commands."],
     ["Deploy", "We clone, build and publish — with live logs you can watch."], ["Share", "Get a live URL instantly, then add your own domain."]].forEach(([t, d], i) => {
      const li = el("li", i === 0 ? "on" : ""); li.innerHTML = "<span class='num'>" + (i + 1) + "</span><div><b>" + t + "</b>" + esc(d) + "</div>"; steps.append(li);
    });
    side.append(steps);
    const co = el("div", "callout info"); co.style.marginTop = "16px";
    co.innerHTML = svg("info") + "<div>Only <b>public</b> repositories are supported in this version. Private repo support arrives with Git provider sign-in.</div>";
    side.append(co);

    if (prefillRepo) fetchRepo();

    function field(label, setup, _v, mono) {
      const f = el("div", "field"); f.innerHTML = "<label>" + esc(label) + "</label>";
      const i = el("input", "input" + (mono ? " mono" : "")); setup(i); f.append(i); return f;
    }
    function selField(label, opts, val, onCh) {
      const f = el("div", "field"); f.innerHTML = "<label>" + esc(label) + "</label>";
      const s = el("select", "select"); s.innerHTML = opts.map((o) => "<option " + (o === val ? "selected" : "") + ">" + esc(o) + "</option>").join("");
      s.onchange = () => onCh(s.value); f.append(s); return f;
    }
  }

  // ---------------------------------------------------------------- PROJECT DETAIL (tabs)
  const PROJECT_TABS = [["", "Overview"], ["deployments", "Deployments"], ["domains", "Domains"], ["env", "Environment"], ["activity", "Activity"], ["settings", "Settings"]];
  const PROJECT_NAV = [
    { group: "Workspace", items: [["", "Project overview"], ["settings", "Project configuration"]] },
    { group: "Build & delivery", items: [["deployments", "Deployments"], ["previews", "Deploy previews"], ["agents", "Agent / build runners"], ["devops", "DevOps"]] },
    { group: "Infrastructure", items: [["compute", "Cloud compute"], ["database", "Database"], ["storage", "Data & storage"], ["functions", "Functions"]] },
    { group: "Security & network", items: [["identity", "Identity & analytics"], ["security", "Web security"], ["env", "Environment variables"], ["domains", "Domain management"], ["edge", "Edge network"]] },
    { group: "Operations", items: [["forms", "Forms"], ["observability", "Observability"], ["activity", "Activity logs"], ["docs", "Documentation"]] },
  ];
  async function projectShell(slug, activeTab, builder) {
    // A project's Deployments tab belongs under "Deployments" in the left menu.
    K.setNav(activeTab === "deployments" ? "deployments" : "projects");
    let p;
    try { p = (await Api.get("/api/projects/" + slug)).project; }
    catch (e) { if (e.status === 404) return setView(view(emptyState("alert", "Project not found", "This project may have been deleted.", K.btn("Back to projects", { cls: "accent", href: "#/projects" })))); throw e; }
    K.setCrumbs([{ label: "Projects", href: "#/projects" }, { label: p.name }]);
    const head = el("div", "proj-head");
    head.append(el("span", "", fwLogo(p.framework, "lg")).firstChild);
    const t = el("div", "", "<h1>" + esc(p.name) + "</h1><div class='muted small'>" + svg("git", "sm") + " " + esc(p.repo_full_name || p.repo_url) + "</div>");
    t.style.minWidth = "0"; head.append(t);
    const actions = el("div", "actions");
    const liveNow = !!p.production && p.production.status === "ready";
    const visit = K.btn("Visit Live Site", { cls: "ghost", icon: "external", href: liveNow ? liveHref(p) : undefined });
    if (!liveNow) { visit.disabled = true; visit.title = "Not live yet"; }
    actions.append(visit);
    if (p.access && p.access.blocked) actions.append(K.btn("Upgrade to continue", { cls: "accent", icon: "bolt", href: "#/billing" }));
    else actions.append(K.btn(p.source_type === "upload" ? "Upload new files" : "Deploy", { cls: "accent", icon: p.source_type === "upload" ? "upload" : "rocket", onClick: () => openDeployModal(p) }));
    const more = el("button", "icon-btn", svg("dots", "sm")); more.onclick = () => projectDetailMenu(more, p); actions.append(more); head.append(actions);

    const layout = el("div", "layout-side project-workspace");
    const nav = el("nav", "subnav project-subnav"); nav.setAttribute("aria-label", "Project navigation");
    PROJECT_NAV.forEach((group) => {
      nav.append(el("div", "project-subnav-label", group.group));
      group.items.forEach(([key, label]) => {
        const href = "#/projects/" + slug + (key ? "/" + key : "");
        const a = el("a", key === activeTab ? "on" : "", esc(label)); a.href = href;
        if (key === activeTab) a.setAttribute("aria-current", "page");
        nav.append(a);
      });
    });
    const body = el("div", "stack project-workspace-body");
    layout.append(nav, body);
    const strip = planStrip(p);
    if (strip) strip.style.marginBottom = "16px";
    setView(view(...[head, strip, layout].filter(Boolean)));
    await builder(p, body);
  }

  function projectDetailMenu(anchor, p) {
    K.dropdown(anchor, [
      { label: p.access && p.access.blocked ? "Upgrade to continue" : p.source_type === "upload" ? "Upload new files" : "Deploy now", icon: p.access && p.access.blocked ? "bolt" : p.source_type === "upload" ? "upload" : "rocket", action: () => openDeployModal(p) },
      ...(p.source_type === "upload" ? [] : [{ label: "Open repository", icon: "git", action: () => openNew(p.repo_url) }]),
      { label: p.is_paused ? "Resume project" : "Pause project", icon: p.is_paused ? "play" : "pause", action: () => togglePause(p) },
      { sep: true },
      { label: "Project settings", icon: "settings", action: () => go("#/projects/" + p.slug + "/settings") },
      { label: "Delete project", icon: "trash", danger: true, action: () => deleteProject(p) },
    ]);
  }
  async function togglePause(p) {
    try { await Api.patch("/api/projects/" + p.slug, { is_paused: !p.is_paused }); toast(p.is_paused ? "Project resumed" : "Project paused"); K.render(); }
    catch (e) { toast(e.message, "error"); }
  }
  function deleteProject(p) {
    const b = el("div");
    const a = p.access || {};
    // The free plan includes ONE project for life. Deleting it does not give a second one, so
    // say so clearly before the person confirms.
    if (a.is_free && !a.admin && (a.expires_at || a.blocked)) {
      const w = el("div", "callout bad delete-warning");
      w.innerHTML = svg("alert") + "<div><b>This is your only free project.</b> The Hobby plan includes one project. If you delete it, you will not be able to upload a new project on the Hobby plan. You will need to upgrade to publish another one.</div>";
      b.append(w);
    }
    b.append(el("p", "muted", "This permanently removes the project, its deployments and domains. This cannot be undone."));
    const f = el("div", "field"); f.innerHTML = "<label>Type <b>" + esc(p.slug) + "</b> to confirm</label>";
    const i = el("input", "input mono"); i.placeholder = p.slug; f.append(i); b.append(f);
    confirmModal({ title: a.is_free && !a.admin && (a.expires_at || a.blocked) ? "Delete your only free project?" : "Delete " + p.name, body: b, confirmLabel: "Delete project", danger: true,
      onConfirm: async () => { await Api.del("/api/projects/" + p.slug, { confirm: i.value.trim() }); toast("Project deleted"); go("#/projects"); } });
  }
  function openDeployModal(p) {
    if (p.access && p.access.blocked) { toast(p.access.reason || "Your free 24 hours have ended. Upgrade to continue.", "error"); return go("#/billing"); }
    if (p.source_type === "upload") return openUploadModal(p);
    const b = el("div");
    const g = el("div", "field"); g.innerHTML = "<label>Branch</label>";
    const branch = el("input", "input mono"); branch.value = p.branch; g.append(branch);
    const target = el("div", "field"); target.innerHTML = "<label>Target</label>";
    const sel = el("select", "select"); sel.innerHTML = "<option value='production'>Production</option><option value='preview'>Preview</option>"; target.append(sel);
    const cc = el("label", "check"); cc.innerHTML = "<input type='checkbox'><div><b>Clear build cache</b><span>Force a clean rebuild from scratch.</span></div>";
    b.append(g, target, cc);
    const go1 = el("button", "btn accent", "Deploy");
    go1.onclick = async () => {
      spin(go1, "Starting…");
      try { const r = await Api.post("/api/projects/" + p.slug + "/deploy", { branch: branch.value.trim(), target: sel.value, clear_cache: cc.querySelector("input").checked });
        K.closeLayer(); toast("Deployment started"); go("#/projects/" + p.slug + "/deployments/" + r.deployment.id); }
      catch (e) { toast(e.message, "error"); unspin(go1); }
    };
    openModal({ title: "Deploy " + p.name, desc: "Trigger a new build and deployment.", body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), go1] });
  }

  // project overview tab
  K.route("projects/:slug", (a) => projectShell(a.slug, "", async (p, body) => {
    const dd = await Api.get("/api/projects/" + p.slug + "/deployments");
    const prod = p.production;
    const card = el("div", "card prod-card");
    const shot = el("div", "shot");
    const thumb = el("div", "preview-thumb " + (prod ? "" : "empty"));
    if (prod) thumb.innerHTML = "<div class='bar'></div><div class='l1'></div><div class='l2'></div><div class='l3'></div>"; else thumb.textContent = "No production deployment yet";
    shot.append(thumb);
    if (prod && prod.status === "ready") { const vb = K.btn("Visit Live Site", { cls: "primary block", icon: "external", href: liveUrl(p) }); vb.style.marginTop = "12px"; shot.append(vb); }
    if (prod && prod.status === "ready" && !prod.is_simulated && !hostingActive(p.hosting_until) && !(p.access && p.access.admin)) {
      // Only when Paid Hosting can really be bought (payments configured and checkout open).
      Api.get("/api/billing").then((bill) => {
        const offer = bill && bill.enabled && bill.checkout_open && (bill.catalog || []).find((c) => c.grants_plan === "hosting");
        if (!offer) return;
        const price = "₹" + (offer.amount / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
        const ub = K.btn("Upgrade to Paid Hosting " + price, { cls: "accent block", icon: "rocket", href: "#/billing" });
        ub.title = "Keep this deployment live for " + (offer.period_days >= 3650 ? "life" : offer.period_days + " days") + " instead of 24 hours"; ub.style.marginTop = "8px"; shot.append(ub);
      }).catch(() => {});
    }
    card.append(shot);
    const info = el("div", "info");
    const acc = p.access || {};
    const planBadge = acc.admin ? "<span class='badge'>Admin</span>" : acc.lifetime ? "<span class='badge live'>Lifetime</span>" : acc.is_free ? "<span class='badge accent'>Free · 24 hours</span>" : "<span class='badge live'>Paid hosting</span>";
    info.append(kv("Status", (prod ? statusChip(prod.status) : (acc.blocked ? "<span class='status expired'><i></i>Expired</span>" : "<span class='muted'>Not deployed</span>")) + " " + simBadge(prod) + " " + planBadge, true));
    const liveAddr = absUrl(liveUrl(p));
    info.append(kv("Live URL", prod && prod.status === "ready"
      ? "<a class='link' href='" + esc(liveAddr) + "' target='_blank' rel='noopener noreferrer'>" + esc(liveHost(p)) + "</a>"
      : "<span class='muted'>" + esc(liveHost(p)) + "</span>"));
    info.append(kv("Framework", p.framework_name));
    info.append(kv("Branch", "<span class='branch'>" + svg("branch", "sm") + esc(p.branch) + "</span>"));
    if (prod) { info.append(kv("Last deploy", dateTime(prod.finished_at || prod.created_at))); info.append(kv("Build time", duration(prod.duration_ms)));
      info.append(kv("Expires", acc.admin || acc.lifetime ? "Never" : prod.expires_at ? (esc(expiryLabel(prod.expires_at)) + " <span class='muted small'>(" + dateTime(prod.expires_at) + ")</span>") : "—"));
      if (hostingActive(p.hosting_until) && !isLifetime(p.hosting_until)) info.append(kv("Paid hosting", "until " + esc(dateTime(p.hosting_until)) + " <span class='muted small'>(+24 h grace)</span>", true));
      info.append(kv("Commit", "<span class='sha'>" + esc((prod.commit_sha || "").slice(0, 7)) + "</span> " + esc(prod.commit_message || ""), true)); }
    card.append(info);
    body.append(card);

    const recent = el("div", "card"); recent.style.marginTop = "16px";
    const ch = el("div", "card-head", "<div><h3>History</h3><p>Only your latest deployment can be opened and redeployed. Older ones are kept as a record.</p></div><div class='actions'></div>");
    $(".actions", ch).append(K.btn("View all", { cls: "ghost sm", href: "#/projects/" + p.slug + "/deployments" }));
    recent.append(ch);
    if (!dd.deployments.length) recent.append(el("div", "card-pad muted", "No deployments yet."));
    else dd.deployments.slice(0, 5).forEach((d) => recent.append(depRow(p, d, dd.current_id, dd.latest_id)));
    body.append(recent);
  }));
  function kv(k, v, full) { return el("div", "kv" + (full ? " full" : ""), "<div class='k'>" + esc(k) + "</div><div class='v'>" + v + "</div>"); }
  // One line of the deployment history. Only the project's LATEST deployment is clickable;
  // older ones are shown as a plain record (they cannot be opened or redeployed).
  function depRow(p, d, currentId, latestId) {
    const latest = latestId === undefined || d.id === latestId;
    const row = el("div", "dep-row" + (latest ? "" : " history"));
    if (!latest) row.title = "Older deployments are kept as history. Open the latest deployment to make changes.";
    const exp = d.expires_at ? " · " + (d.status === "expired" ? "expired " : "expires ") + shortDate(d.expires_at) : "";
    row.innerHTML = "<div><div class='row'><span class='id'>#" + d.number + "</span>" + (latest ? "<span class='badge accent'>Latest</span>" : "") + (d.id === currentId ? "<span class='badge live'>Production</span>" : d.target === "preview" ? "<span class='badge'>Preview</span>" : "") + "</div><div class='meta'>" + esc(d.trigger) + esc(exp) + "</div></div>" +
      "<div>" + statusChip(d.status) + "</div>" +
      "<div class='commit'><span class='sha'>" + esc((d.commit_sha || "").slice(0, 7)) + "</span><span class='msg'>" + esc(d.commit_message || d.branch) + "</span></div>" +
      "<div class='meta'><div class='branch'>" + svg("branch", "sm") + esc(d.branch) + "</div><div>" + timeAgo(d.created_at) + "</div></div>" +
      "<div>" + (latest ? svg("chevron", "sm") : "") + "</div>";
    if (latest) row.onclick = () => go("#/projects/" + p.slug + "/deployments/" + d.id);
    return row;
  }

  // deployments tab
  K.route("projects/:slug/deployments", (a) => projectShell(a.slug, "deployments", async (p, body) => {
    const dd = await Api.get("/api/projects/" + p.slug + "/deployments");
    const card = el("div", "card");
    card.append(el("div", "card-head", "<div><h3>Deployment history</h3><p>" + dd.deployments.length + " total · only the latest deployment can be opened or redeployed</p></div>"));
    if (!dd.deployments.length) card.append(emptyState("deployments", "No deployments", "Deploy this project to see its build history here.", K.btn("Deploy now", { cls: "accent", icon: "rocket", onClick: () => openDeployModal(p) })));
    else dd.deployments.forEach((d) => card.append(depRow(p, d, dd.current_id, dd.latest_id)));
    body.append(card);
  }));

  // activity tab
  K.route("projects/:slug/activity", (a) => projectShell(a.slug, "activity", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/activity");
    const card = el("div", "card");
    card.append(el("div", "card-head", "<div><h3>Activity</h3></div>"));
    if (!d.activity.length) card.append(el("div", "card-pad muted", "No activity recorded yet."));
    else { const ul = el("ul", "feed"); d.activity.forEach((x) => ul.append(activityItem(x))); card.append(ul); }
    body.append(card);
  }));
  function activityItem(x) {
    const li = el("li");
    li.innerHTML = "<span class='ico'>" + svg(actIcon(x.action), "sm") + "</span><div class='txt'><div><b>" + esc(x.actor) + "</b> <span class='muted'>" + esc(actLabel(x.action)) + "</span> " + (x.target ? "<span class='sha'>" + esc(x.target) + "</span>" : "") + "</div>" + (x.detail ? "<div class='muted small'>" + esc(x.detail) + "</div>" : "") + "</div><span class='when'>" + timeAgo(x.ts) + "</span>";
    return li;
  }
  function actIcon(a) { if (a.startsWith("deploy")) return "rocket"; if (a.startsWith("domain")) return "globe"; if (a.startsWith("env")) return "key"; if (a.startsWith("project")) return "projects"; if (a.startsWith("login") || a.startsWith("logout")) return "user"; if (a.startsWith("user")) return "team"; return "activity"; }
  function actLabel(a) { return ({ "project.created": "created the project", "project.updated": "updated settings", "project.deleted": "deleted a project", "deploy.triggered": "started deployment", "deploy.hook": "triggered via hook", "deploy.promoted": "promoted deployment", "deploy.cancelled": "cancelled deployment", "env.saved": "saved environment variables", "env.deleted": "removed a variable", "env.revealed": "revealed a secret", "domain.added": "added a domain", "domain.verified": "verified a domain", "domain.removed": "removed a domain", "login.ok": "signed in", "logout": "signed out", "user.created": "added a member", "user.updated": "updated a member" }[a] || a.replace(/[._]/g, " ")); }

  // ---------------------------------------------------------------- DEPLOYMENT DETAIL + LIVE LOGS
  const STAGES = ["queued", "initializing", "cloning", "installing", "building", "deploying", "ready"];
  let logPoll = null;
  let buildTimer = null;
  function stopPoll() { if (logPoll) { clearTimeout(logPoll); logPoll = null; } if (buildTimer) { clearInterval(buildTimer); buildTimer = null; } }
  // For regular people, every "not available yet" warning is shown as an upgrade message instead.
  // Administrators keep the original warnings so that each one can be found and fixed later.
  function softenWarnings(root) {
    if (!State.user || State.user.role === "admin") return;
    root.querySelectorAll(".callout.warn:not([data-keep])").forEach((c) => {
      c.className = "callout info upgrade-note";
      c.innerHTML = svg("bolt") + "<div><b>Included in paid plans.</b> Upgrade your plan to unlock this feature.</div>";
      c.append(K.btn("View plans", { cls: "sm accent", href: "#/billing" }));
    });
  }

  K.route("projects/:slug/deployments/:id", async (a) => {
    stopPoll();
    K.setNav("deployments");   // a deployment page is part of "Deployments" in the left menu
    let d, logs, access = {};
    try { const r = await Api.get("/api/deployments/" + a.id); d = r.deployment; logs = r.logs; access = r.access || {}; }
    catch (e) { if (e.status === 404) return setView(view(emptyState("alert", "Deployment not found", "", K.btn("Back", { cls: "accent", href: "#/projects/" + a.slug + "/deployments" })))); throw e; }
    K.setCrumbs([{ label: "Deployments", href: "#/deployments" }, { label: d.project_name, href: "#/projects/" + a.slug }, { label: "Deploy #" + d.number }]);

    const head = el("div", "proj-head");
    head.append(el("div", "", "<h1>Deployment #" + d.number + "</h1><div class='muted small'>" + esc(d.project_name) + " · " + esc(d.trigger) + "</div>"));
    const actions = el("div", "actions");
    head.append(actions);
    const bodyWrap = el("div");
    setView(viewWide(head, bodyWrap));

    function paintActions() {
      clear(actions);
      if (d.status === "building" || d.status === "queued") actions.append(K.btn("Cancel", { cls: "danger-ghost", icon: "x", onClick: cancelDep }));
      if (d.url && d.status === "ready" && (d.is_latest || d.is_current)) actions.append(K.btn("Visit", { cls: "ghost", icon: "external", href: d.url }));
      if (!d.is_latest) return;   // older deployments are view-only history: no actions
      if (access.blocked && ["ready", "failed", "cancelled", "expired"].includes(d.status)) {
        actions.append(K.btn("Upgrade to continue", { cls: "accent", icon: "bolt", href: "#/billing" }));
        return;
      }
      if (d.is_upload && ["ready", "failed", "cancelled"].includes(d.status)) actions.append(K.btn("Upload new files", { cls: "accent", icon: "upload", onClick: () => openUploadModal({ slug: a.slug, name: d.project_name }) }));
      if (d.status === "ready" && !d.is_current && !d.is_upload) actions.append(K.btn("Promote to production", { cls: "accent", icon: "rocket", onClick: promote }));
      if (!d.is_upload && ["ready", "failed", "cancelled"].includes(d.status)) actions.append(K.btn("Redeploy", { cls: d.status === "ready" && d.is_current ? "accent" : "", icon: "refresh", onClick: redeploy }));
    }
    async function cancelDep() { try { await Api.post("/api/deployments/" + d.id + "/cancel"); toast("Cancelling…"); } catch (e) { toast(e.message, "error"); } }
    async function promote() { try { const r = await Api.post("/api/deployments/" + d.id + "/promote"); toast(r.mode === "instant" ? "Promoted to production" : "Rebuilding for production"); if (r.mode === "rebuild") go("#/projects/" + a.slug + "/deployments/" + r.deployment_id); else K.render(); } catch (e) { toast(e.message, "error"); } }
    async function redeploy() { try { const r = await Api.post("/api/deployments/" + d.id + "/redeploy"); toast("Redeploying"); go("#/projects/" + a.slug + "/deployments/" + r.deployment.id); } catch (e) { toast(e.message, "error"); } }

    // summary header
    const summary = el("div", "card"); const dh = el("div", "deploy-head");
    function paintSummary() {
      clear(dh);
      dh.append(cell("Status", statusChip(d.status) + (d.is_current ? " <span class='badge live'>Production</span>" : "") + " " + simBadge(d)));
      dh.append(cell("Target", d.target === "preview" ? "Preview" : "Production"));
      dh.append(cell("Commit", "<span class='sha'>" + esc((d.commit_sha || "").slice(0, 7)) + "</span>"));
      dh.append(cell("Duration", d.duration_ms ? duration(d.duration_ms) : (d.status === "building" ? "running…" : "—")));
      dh.append(cell("Created", timeAgo(d.created_at)));
      if (d.status === "expired") dh.append(cell("Expired", timeAgo(d.expired_at) + (d.teardown_note ? "<div class='muted small'>" + esc(d.teardown_note) + "</div>" : "")));
      else if (d.expires_at) dh.append(cell("Lifetime", esc(expiryLabel(d.expires_at) || "—")));
    }
    function cell(k, v) { return el("div", "", "<div class='k muted small' style='margin-bottom:4px'>" + esc(k) + "</div><div>" + v + "</div>"); }
    summary.append(dh);

    // commit line
    const commitLine = el("div", "card card-pad"); commitLine.style.margin = "16px 0";
    commitLine.innerHTML = "<div class='row'>" + svg("git") + "<div style='flex:1;min-width:0'><b>" + esc(d.commit_message || "Deploy " + d.branch) + "</b><div class='muted small'><span class='branch'>" + svg("branch", "sm") + esc(d.branch) + "</span>" + (d.triggered_by ? " · by " + esc(d.triggered_by) : "") + "</div></div>" + (d.is_upload ? "" : "<a class='link small' href='" + esc(d.repo_url) + "' target='_blank'>View source </a>") + "</div>";

    // steps
    const stepsWrap = el("div", "steps");
    function paintSteps() {
      clear(stepsWrap);
      const curIdx = STAGES.indexOf(d.stage);
      STAGES.filter((s) => s !== "queued").forEach((s) => {
        const idx = STAGES.indexOf(s);
        let cls = "step";
        if (d.status === "failed" && idx === curIdx) cls += " fail";
        else if (d.status === "ready" || idx < curIdx) cls += " done";
        else if (idx === curIdx && (d.status === "building")) cls += " active";
        const st = el("div", cls, "<div class='bar'></div><div class='lbl'>" + cap(s) + "</div>");
        stepsWrap.append(st);
      });
    }

    // log console
    const logCard = el("div", "log");
    const bar = el("div", "log-bar");
    const filter = el("input", "input"); filter.placeholder = "Filter logs…";
    const liveBadge = el("span", "badge"); 
    const barLeft = el("span", "row", svg("terminal", "sm") + "<span>Build logs</span>");
    const barGap = el("span"); barGap.style.flex = "1";
    bar.append(barLeft, liveBadge, barGap, filter);
    const copyBtn = el("button", "btn sm", svg("copy", "sm")); bar.append(copyBtn);
    const logBody = el("div", "log-body");
    logCard.append(bar, logBody);
    let allLogs = [], lastId = 0;
    function appendLogs(entries) {
      entries.forEach((l) => {
        allLogs.push(l); lastId = l.id;
        const line = el("div", "log-line " + (l.level || ""));
        line.dataset.text = l.line.toLowerCase();
        line.innerHTML = "<span class='n'>" + allLogs.length + "</span><span class='t'>" + (l.ts ? l.ts.slice(11, 19) : "") + "</span><span class='m'>" + esc(l.line) + "</span>";
        logBody.append(line);
      });
      applyFilter();
      logBody.scrollTop = logBody.scrollHeight;
    }
    function applyFilter() {
      const q = filter.value.trim().toLowerCase();
      logBody.querySelectorAll(".log-line").forEach((l) => l.classList.toggle("hidden", q && !l.dataset.text.includes(q)));
    }
    filter.oninput = applyFilter;
    copyBtn.onclick = () => { navigator.clipboard.writeText(allLogs.map((l) => l.line).join("\n")).then(() => toast("Logs copied")); };

    // The "building" screen. People never see queue jargon: while a project waits or builds they see
    // a calm progress page. The percentage is REAL (it follows the build stage), and after a while a
    // clear note explains that larger projects take longer and asks them to keep the page open.
    const hall = el("div", "build-card"); hall.hidden = true;
    const PCT = { queued: 6, initializing: 14, cloning: 30, installing: 48, building: 70, deploying: 90 };
    const COPY = {
      queued: ["Preparing your build", "Your project is next in line and will start in a moment."],
      initializing: ["Setting things up", "Preparing a clean, secure build environment for your project."],
      cloning: ["Fetching your code", "Bringing your project files onto the build server."],
      installing: ["Installing dependencies", "Downloading the packages your project needs. Projects with many dependencies take longer here."],
      building: ["Building your project", "Compiling and packaging your application."],
      deploying: ["Going live", "Starting your app and creating your live link."],
    };
    const TIPS = ["Your live link will appear on this page the moment it is ready.", "Every build runs in its own isolated environment.", "You can follow every step in the build log below.", "Nothing is lost if your connection drops; the build keeps running."];
    const LONG_AFTER_MIN = 12;
    let hallTick = 0, hallReady = false;
    const H = {};
    function mountHall() {
      hall.innerHTML = "<div class='bc-head'><span class='bc-ico'></span><div class='bc-title'><b></b><span></span></div><div class='bc-pct'></div></div>" +
        "<div class='bc-bar'><i></i></div>" +
        "<div class='bc-meta'><span class='bc-stage'></span><span class='bc-time'></span></div>" +
        "<div class='bc-tip'></div>" +
        "<div class='bc-long' hidden></div>";
      H.ico = $(".bc-ico", hall); H.title = $(".bc-title b", hall); H.sub = $(".bc-title span", hall); H.pct = $(".bc-pct", hall);
      H.fill = $(".bc-bar i", hall); H.stage = $(".bc-stage", hall); H.time = $(".bc-time", hall); H.tip = $(".bc-tip", hall); H.long = $(".bc-long", hall);
      H.ico.innerHTML = svg("rocket");
      hallReady = true;
    }
    function hallMinutes() { const t = Date.parse(d.started_at || d.created_at); return isNaN(t) ? 0 : Math.max(0, (Date.now() - t) / 60000); }
    function paintHall() {
      const waiting = d.status === "queued" || d.status === "building";
      hall.hidden = !waiting;
      if (!waiting) { clearInterval(buildTimer); buildTimer = null; return; }
      if (!hallReady) mountHall();
      const stage = COPY[d.stage] ? d.stage : (d.status === "queued" ? "queued" : "building");
      const pct = PCT[stage] || 6;
      const ahead = d.builds_ahead || 0;
      H.title.textContent = COPY[stage][0];
      H.sub.textContent = stage === "queued" && ahead > 0 ? (ahead === 1 ? "One project is being built just before yours." : ahead + " projects are being built just before yours.") : COPY[stage][1];
      H.pct.textContent = pct + "%";
      H.fill.style.width = pct + "%";
      H.stage.textContent = "Step " + Math.max(1, Math.min(6, STAGES.indexOf(stage) + (stage === "queued" ? 1 : 0))) + " of 6";
      tickHall();
      if (!buildTimer) buildTimer = setInterval(tickHall, 1000);
    }
    function tickHall() {
      if (!hallReady) return;
      const mins = hallMinutes(), s = Math.floor(mins * 60);
      H.time.textContent = "Elapsed " + String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
      if (s % 6 === 0) H.tip.textContent = TIPS[hallTick++ % TIPS.length];
      if (!H.tip.textContent) H.tip.textContent = TIPS[0];
      if (mins >= LONG_AFTER_MIN) {
        H.long.hidden = false;
        if (!H.long.dataset.shown) {
          H.long.dataset.shown = "1";
          H.long.innerHTML = svg("info") + "<div><b>This build is taking longer than usual.</b> Your project is large or has many dependencies, so it needs more time to build and deploy. This is normal for complex projects and can take 20–30 minutes. <b>Please keep this page open</b>; your live link appears here automatically as soon as it is ready.</div>";
        }
      } else H.long.hidden = true;
    }
    if (d.is_latest === false) {
      const old = el("div", "callout info"); old.style.marginBottom = "16px";
      old.innerHTML = svg("info") + "<div><b>This is an older deployment, kept as history.</b> It can’t be redeployed. Open the project’s latest deployment to make changes.</div>";
      old.append(K.btn("Go to deployments", { cls: "sm", href: "#/projects/" + a.slug + "/deployments" }));
      bodyWrap.append(old);
    }
    bodyWrap.append(hall, summary, commitLine, stepsWrap, logCard);
    paintActions(); paintSummary(); paintSteps(); paintHall();
    liveBadge.textContent = d.status === "building" || d.status === "queued" ? "● Live" : cap(d.status);
    liveBadge.className = "badge " + (d.status === "ready" ? "live" : d.status === "failed" ? "bad" : d.status === "building" ? "warn" : "");
    appendLogs(logs);

    async function poll() {
      try {
        const r = await Api.get("/api/deployments/" + d.id + "?after=" + lastId);
        d = r.deployment; access = r.access || access;
        if (r.logs.length) appendLogs(r.logs);
        paintActions(); paintSummary(); paintSteps(); paintHall();
        liveBadge.textContent = d.status === "building" || d.status === "queued" ? "● Live" : cap(d.status);
        liveBadge.className = "badge " + (d.status === "ready" ? "live" : d.status === "failed" ? "bad" : d.status === "building" ? "warn" : "");
        if (d.status === "building" || d.status === "queued") logPoll = setTimeout(poll, 1000);
        else { stopPoll(); refreshNotifBadge(); }
      } catch (e) { stopPoll(); }
    }
    if (d.status === "building" || d.status === "queued") logPoll = setTimeout(poll, 1000);
  });

  // ---------------------------------------------------------------- ENV TAB
  K.route("projects/:slug/env", (a) => projectShell(a.slug, "env", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/env");
    const card = el("div", "card");
    const ch = el("div", "card-head", "<div><h3>Environment variables</h3><p>Injected into builds and runtime for the selected targets.</p></div><div class='actions'></div>");
    $(".actions", ch).append(K.btn("Bulk edit", { cls: "ghost", icon: "terminal", onClick: () => bulkEnv(p) }), K.btn("Add variable", { cls: "accent", icon: "plus", onClick: () => editEnv(p) }));
    card.append(ch);
    if (!d.env.length) card.append(emptyState("key", "No variables yet", "Add API keys, database URLs and other configuration your app needs.", K.btn("Add variable", { cls: "accent", icon: "plus", onClick: () => editEnv(p) })));
    else {
      const table = el("table", "table");
      table.innerHTML = "<thead><tr><th>Key</th><th>Value</th><th>Targets</th><th></th></tr></thead>";
      const tb = el("tbody");
      d.env.forEach((e) => {
        const tr = el("tr");
        const valCell = el("td");
        if (e.is_secret) {
          valCell.innerHTML = "<span class='muted mono'>" + "•".repeat(Math.min(14, e.length || 8)) + "</span>";
          const rb = el("button", "btn sm ghost", svg("eye", "sm")); rb.style.marginLeft = "8px";
          rb.onclick = async () => { try { const r = await Api.get("/api/projects/" + p.slug + "/env/" + e.id + "/reveal"); valCell.innerHTML = "<span class='secret-reveal'>" + esc(r.env.value) + "</span>"; } catch (err) { toast(err.message, "error"); } };
          valCell.append(rb);
        } else valCell.innerHTML = "<span class='mono'>" + esc(e.value) + "</span>";
        tr.innerHTML = "<td><b class='mono'>" + esc(e.key) + "</b></td>";
        tr.append(valCell);
        const tg = el("td", "", e.targets.map((t) => "<span class='badge'>" + esc(t) + "</span>").join(" "));
        tr.append(tg);
        const act = el("td"); act.style.textAlign = "right";
        const edit = el("button", "btn sm ghost", svg("settings", "sm")); edit.onclick = () => editEnv(p, e);
        const del = el("button", "btn sm danger-ghost", svg("trash", "sm")); del.onclick = () => confirmModal({ title: "Delete " + e.key + "?", desc: "Remove this variable from the project.", confirmLabel: "Delete", danger: true, onConfirm: async () => { await Api.del("/api/projects/" + p.slug + "/env/" + e.id); toast("Variable deleted"); K.render(); } });
        act.append(edit, del); tr.append(act);
        tb.append(tr);
      });
      table.append(tb); const tw = el("div", "table-wrap"); tw.append(table); card.append(tw);
    }
    body.append(card);
  }));
  function editEnv(p, e) {
    const b = el("div");
    const kf = el("div", "field"); kf.innerHTML = "<label>Key</label>"; const ki = el("input", "input mono"); ki.placeholder = "DATABASE_URL"; ki.value = e ? e.key : ""; if (e) ki.disabled = true; kf.append(ki);
    const vf = el("div", "field"); vf.innerHTML = "<label>Value</label>"; const vi = el("textarea", "textarea"); vi.style.minHeight = "80px"; vi.value = e ? (e.value || "") : ""; if (e && e.is_secret && e.value == null) vi.placeholder = "Leave blank to keep the current secret"; vf.append(vi);
    const sf = el("div", "field"); sf.innerHTML = "<label>Targets</label>";
    const tgs = el("div", "row");
    const prod = mkCheck("Production", !e || e.targets.includes("production"));
    const prev = mkCheck("Preview", !e || e.targets.includes("preview"));
    tgs.append(prod.wrap, prev.wrap); sf.append(tgs);
    const secret = el("label", "check"); secret.innerHTML = "<input type='checkbox' " + (!e || e.is_secret ? "checked" : "") + "><div><b>Encrypt as secret</b><span>Hide the value in the dashboard after saving.</span></div>";
    b.append(kf, vf, sf, secret);
    const save = el("button", "btn accent", "Save variable");
    save.onclick = async () => {
      const targets = []; if (prod.input.checked) targets.push("production"); if (prev.input.checked) targets.push("preview");
      const item = { key: ki.value.trim(), value: vi.value, is_secret: secret.querySelector("input").checked, targets };
      if (e && e.is_secret && vi.value === "") { toast("Enter a value or delete the variable.", "error"); return; }
      spin(save, "Saving…");
      try { await Api.post("/api/projects/" + p.slug + "/env", item); K.closeLayer(); toast("Variable saved"); K.render(); }
      catch (err) { toast(err.message, "error"); unspin(save); }
    };
    openModal({ title: e ? "Edit " + e.key : "Add environment variable", body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), save] });
  }
  function mkCheck(label, on) { const wrap = el("label", "check"); const input = el("input"); input.type = "checkbox"; input.checked = on; wrap.append(input, el("div", "", "<b>" + esc(label) + "</b>")); return { wrap, input }; }
  function bulkEnv(p) {
    const b = el("div");
    b.innerHTML = "<p class='muted small'>Paste variables in <b>.env</b> format — one KEY=value per line.</p>";
    const ta = el("textarea", "textarea"); ta.placeholder = "API_URL=https://api.example.com\nSECRET_KEY=..."; ta.style.minHeight = "200px"; b.append(ta);
    const save = el("button", "btn accent", "Import variables");
    save.onclick = async () => {
      const items = ta.value.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#") && l.includes("=")).map((l) => { const i = l.indexOf("="); return { key: l.slice(0, i).trim(), value: l.slice(i + 1).trim().replace(/^["']|["']$/g, "") }; });
      if (!items.length) return toast("No variables found.", "error");
      spin(save, "Importing…");
      try { await Api.post("/api/projects/" + p.slug + "/env", { items }); K.closeLayer(); toast(items.length + " variables imported"); K.render(); }
      catch (e) { toast(e.message, "error"); unspin(save); }
    };
    openModal({ title: "Bulk import variables", body: b, wide: true, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), save] });
  }

  // ---------------------------------------------------------------- DOMAINS TAB
  K.route("projects/:slug/domains", (a) => projectShell(a.slug, "domains", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/domains");
    const card = el("div", "card");
    const ch = el("div", "card-head", "<div><h3>Domains</h3><p>Connect your own domain or use the free Codeskys subdomain.</p></div><div class='actions'></div>");
    $(".actions", ch).append(K.btn("Add domain", { cls: "accent", icon: "plus", onClick: () => addDomain(p) }));
    card.append(ch);
    const list = el("div", "list");
    d.domains.forEach((dom) => list.append(domainRow(p, dom)));
    card.append(list);
    body.append(card);
  }));
  function domainRow(p, dom) {
    const li = el("div"); li.style.cssText = "padding:16px 20px;border-top:1px solid var(--line)";
    const top = el("div", "row");
    const statusBadge = dom.status === "active" ? "<span class='badge live'>" + svg("check", "sm") + "Active</span>" : "<span class='badge warn'>Pending verification</span>";
    top.innerHTML = svg("globe") + "<div style='flex:1;min-width:0'><div class='row'><b>" + esc(dom.hostname) + "</b>" + (dom.is_primary ? "<span class='badge accent'>Primary</span>" : "") + (dom.is_system ? "<span class='badge'>Codeskys subdomain</span>" : "") + "</div></div>" + statusBadge;
    li.append(top);
    const act = el("div", "row"); act.style.marginTop = "10px"; act.style.gap = "8px";
    if (dom.status === "active" && !dom.is_primary) act.append(K.btn("Make primary", { cls: "ghost sm", onClick: async () => { await Api.post("/api/projects/" + p.slug + "/domains/" + dom.id + "/primary"); toast("Primary domain updated"); K.render(); } }));
    if (!dom.is_system) {
      if (dom.status !== "active") act.append(K.btn("Verify", { cls: "accent sm", icon: "refresh", onClick: () => verifyDomain(p, dom) }));
      act.append(K.btn("Remove", { cls: "danger-ghost sm", icon: "trash", onClick: () => confirmModal({ title: "Remove " + dom.hostname + "?", confirmLabel: "Remove", danger: true, onConfirm: async () => { await Api.del("/api/projects/" + p.slug + "/domains/" + dom.id); toast("Domain removed"); K.render(); } }) }));
      act.append(K.btn("DNS instructions", { cls: "ghost sm", icon: "info", onClick: () => showDns(dom) }));
    } else {
      act.append(K.btn("Open", { cls: "ghost sm", icon: "external", href: "/sites/" + p.slug + "/" }));
    }
    li.append(act);
    return li;
  }
  function showDns(dom) {
    if (!dom.dns) return;
    const b = el("div");
    b.innerHTML = "<p class='muted small'>Add these records at your DNS provider, then verify. Changes can take a few minutes to propagate.</p>";
    const t = el("table", "dns");
    t.innerHTML = "<thead><tr><th>Type</th><th>Name</th><th>Value</th></tr></thead><tbody>" +
      "<tr><td>" + dom.dns.cname.type + "</td><td>" + esc(dom.dns.cname.name) + "</td><td>" + esc(dom.dns.cname.value) + "</td></tr>" +
      "<tr><td>" + dom.dns.txt.type + "</td><td>" + esc(dom.dns.txt.name) + "</td><td>" + esc(dom.dns.txt.value) + "</td></tr></tbody>";
    b.append(t);
    openModal({ title: "DNS records for " + dom.hostname, body: b, wide: true, foot: [K.btn("Done", { cls: "accent", onClick: K.closeLayer })] });
  }
  function addDomain(p) {
    const b = el("div");
    const f = el("div", "field"); f.innerHTML = "<label>Domain</label><div class='hint'>Enter a domain or subdomain you own, like www.example.com.</div>";
    const i = el("input", "input mono"); i.placeholder = "www.example.com"; f.append(i); b.append(f);
    const save = el("button", "btn accent", "Add domain");
    save.onclick = async () => { spin(save, "Adding…"); try { await Api.post("/api/projects/" + p.slug + "/domains", { hostname: i.value.trim() }); K.closeLayer(); toast("Domain added — add the DNS records to verify"); K.render(); } catch (e) { toast(e.message, "error"); unspin(save); } };
    openModal({ title: "Add a domain", body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), save] });
  }
  async function verifyDomain(p, dom) {
    toast("Checking DNS…");
    try { const r = await Api.post("/api/projects/" + p.slug + "/domains/" + dom.id + "/verify"); if (r.verified) { toast("Domain verified"); } else { toast("DNS records not found yet — check and try again", "error"); showDns(dom); } K.render(); }
    catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- PROJECT WORKSPACE MODULES
  function moduleHeader(title, description, state, setupText) {
    const wrap = el("div", "card");
    const head = el("div", "card-head");
    head.innerHTML = "<div><h3>" + esc(title) + "</h3><p>" + esc(description) + "</p></div>";
    const badge = el("span", "badge " + (state === "Available" ? "live" : "warn"), state);
    head.append(badge); wrap.append(head);
    if (setupText) wrap.append(el("div", "card-pad muted", setupText));
    return wrap;
  }
  function featureUnavailable(body, title, description, requirements) {
    const card = moduleHeader(title, description, "Requires integration", "");
    const pad = el("div", "card-pad stack");
    pad.append(el("p", "muted", "This module is not active on the current Codeskys instance. No demo data or fake operational status is shown."));
    const req = el("div", "callout info");
    req.innerHTML = svg("info") + "<div><b>What is needed</b><br>" + esc(requirements) + "</div>";
    pad.append(req);
    card.append(pad); body.append(card);
  }

  K.route("projects/:slug/previews", (a) => projectShell(a.slug, "previews", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/deployments");
    const rows = d.deployments.filter((x) => x.target === "preview");
    const card = el("div", "card");
    card.append(el("div", "card-head", "<div><h3>Deploy previews</h3><p>Preview builds are isolated from the production deployment.</p></div><span class='badge live'>Available</span>"));
    if (!rows.length) card.append(emptyState("deployments", "No preview deployments", "Choose Preview as the target when starting a deployment.", K.btn("Create preview", { cls: "accent", icon: "plus", onClick: () => openDeployModal(p) })));
    else rows.forEach((d1) => card.append(depRow(p, d1, d.current_id)));
    body.append(card);
    const info = el("div", "callout info"); info.innerHTML = svg("info") + "<div>Preview builds use the same configured deployment engine as production. Their URL is only live after the provider reports a successful build. Each deployment follows the project's configured expiry policy.</div>"; body.append(info);
  }));

  K.route("projects/:slug/agents", (a) => projectShell(a.slug, "agents", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/deployments");
    const running = d.deployments.filter((x) => ["queued", "building"].includes(x.status));
    const card = moduleHeader("Build runners", "Codeskys's durable workers execute deployment jobs and recover work after restarts.", "Available", "");
    const pad = el("div", "card-pad stack");
    const stats = el("div", "stats");
    stats.append(stat("deployments", "Queued / building", String(running.length), "For this project"),
      stat("history", "Deployment jobs", String(d.deployments.length), "All recorded runs"));
    pad.append(stats);
    if (!running.length) pad.append(el("div", "callout info", svg("check") + "<div>No build jobs are currently running for this project.</div>"));
    else running.forEach((x) => pad.append(depRow(p, x, d.current_id)));
    card.append(pad); body.append(card);
    const note = el("div", "callout warn"); note.innerHTML = svg("info") + "<div><b>Scope:</b> These are real deployment build workers. A general-purpose AI coding-agent runner (which edits repositories, executes tools and proposes code changes) is not implemented or connected yet.</div>"; body.append(note);
    body.append(K.btn("Open deployment history", { cls: "ghost", icon: "history", href: "#/projects/" + p.slug + "/deployments" }));
  }));

  K.route("projects/:slug/devops", (a) => projectShell(a.slug, "devops", async (p, body) => {
    const provs = await Api.get("/api/git/providers");
    const acc = provs.providers.find((x) => x.provider === (p.git_provider || "github"));
    const git = moduleHeader("Source control", "Repository connection and branch automation for this project.", p.git_account ? "Available" : "Needs connection", "");
    const pad = el("div", "card-pad stack");
    pad.append(kv("Provider", cap(p.git_provider || "github")), kv("Repository", esc(p.repo_full_name || p.repo_url)),
      kv("Branch", esc(p.branch)), kv("Account", p.git_account ? esc(p.git_account.username) : "Public repository URL / account not linked"),
      kv("Automatic redeploy", p.auto_deploy ? "Enabled" : "Disabled"));
    if (!p.git_account && acc && acc.configured) pad.append(K.btn("Connect " + acc.label, { cls: "accent", icon: "link", onClick: () => connectGit(acc) }));
    git.append(pad); body.append(git);
    const hooks = moduleHeader("Deployment webhooks", "Verified Git push hooks and CI triggers for this project.", "Available", "");
    const hb = el("div", "card-pad stack");
    hb.append(el("p", "muted small", "Add the matching endpoint in your repository's webhook settings. The project signing secret is never displayed by this page."));
    hb.append(el("div", "field", "<label>GitHub webhook URL</label>").appendChild(copyBox(p.hooks.github)).parentNode);
    hb.append(el("div", "field", "<label>GitLab webhook URL</label>").appendChild(copyBox(p.hooks.gitlab)).parentNode);
    hb.append(el("div", "field", "<label>Generic deploy hook</label>").appendChild(copyBox(p.hooks.generic)).parentNode);
    hb.append(K.btn("Rotate hook URLs", { cls: "danger-ghost", icon: "refresh", onClick: () => confirmModal({ title: "Rotate project hooks?", desc: "Old URLs stop working immediately. Update your repository settings after rotation.", confirmLabel: "Rotate hooks", danger: true, onConfirm: async () => { await Api.post("/api/projects/" + p.slug + "/hook/rotate"); toast("Hook URLs rotated"); K.render(); } }) }));
    hooks.append(hb); body.append(hooks);
    const api = el("div", "callout info"); api.innerHTML = svg("terminal") + "<div>Personal API tokens are available under <a class='link' href='#/settings?s=tokens'>Account settings → API tokens</a>. Use scoped, revocable tokens in CI. Never commit tokens into a repository.</div>"; body.append(api);
  }));

  K.route("projects/:slug/compute", (a) => projectShell(a.slug, "compute", async (p, body) => {
    const status = await Api.get("/api/platform/status");
    const card = moduleHeader("Cloud compute", "Runtime configuration and resource ceilings applied to this project's application.", status.provider === "coolify" ? "Available" : "Needs provider", "");
    const pad = el("div", "card-pad stack");
    pad.append(kv("Deployment engine", engineLabel(status.provider)), kv("Provider status", status.engine && status.engine.configured ? status.engine.state : "Not configured"),
      kv("Memory limit", esc(p.limits_memory || "512M")), kv("CPU limit", esc(p.limits_cpus || "0.5")), kv("Application port", String(p.port || 3000)),
      kv("Runtime", esc(p.node_version || "Provider default")));
    if (State.user.role === "admin") {
      const mem = inputField("Memory limit", p.limits_memory || "512M", "512M");
      const cpu = inputField("CPU limit", p.limits_cpus || "0.5", "0.5");
      const save = K.btn("Save resource limits", { cls: "accent", onClick: async () => { try { await Api.patch("/api/projects/" + p.slug, { limits_memory: mem.input.value.trim(), limits_cpus: cpu.input.value.trim() }); toast("Resource limits saved"); K.render(); } catch (e) { toast(e.message, "error"); } } });
      pad.append(mem.field, cpu.field, save);
    } else pad.append(el("p", "muted small", "Build resource ceilings are managed by the Codeskys administrator."));
    card.append(pad); body.append(card);
    const note = el("div", "callout warn"); note.innerHTML = svg("info") + "<div>Actual CPU and memory consumption is only available when the deployment provider exposes metrics. The values above are configured limits, not live usage measurements.</div>"; body.append(note);
  }));

  K.route("projects/:slug/observability", (a) => projectShell(a.slug, "observability", async (p, body) => {
    const d = await Api.get("/api/analytics?days=30&project=" + encodeURIComponent(p.slug));
    const stats = el("div", "stats");
    stats.append(stat("deployments", "Deployments", String(d.total), "Last 30 days"), stat("check", "Successful", String(d.ready), "Builds marked ready"),
      stat("alert", "Failed", String(d.failed), "Builds marked failed"), stat("timer", "Build minutes", String(d.build_minutes), "Recorded build time"));
    body.append(stats);
    const card = el("div", "card"); card.append(el("div", "card-head", "<div><h3>Deployment observability</h3><p>Build outcomes and performance recorded by Codeskys.</p></div><span class='badge live'>Available</span>"));
    const pad = el("div", "card-pad stack");
    if (d.durations.length) d.durations.slice().reverse().forEach((x) => pad.append(el("div", "row", "<span class='muted small'>" + esc(dateTime(x.at)) + "</span><b>" + esc(duration(x.ms)) + "</b><span class='muted small'>#" + x.id + "</span>")));
    else pad.append(el("p", "muted", "No completed deployment timing data yet."));
    card.append(pad); body.append(card);
    const note = el("div", "callout info"); note.innerHTML = svg("info") + "<div>Available today: deployment counts, build outcomes, build duration and project activity. Not yet collected: live application traces, request-level latency, traffic, CPU or memory telemetry. Those require runtime instrumentation or provider metrics.</div>"; body.append(note);
  }));

  K.route("projects/:slug/security", (a) => projectShell(a.slug, "security", async (p, body) => {
    const env = await Api.get("/api/projects/" + p.slug + "/env");
    const sec = moduleHeader("Project security controls", "Webhook integrity, secret handling and project access boundaries.", "Available", "");
    const pad = el("div", "card-pad stack");
    const secretCount = env.env.filter((x) => x.is_secret).length;
    pad.append(kv("Project owner", p.owner ? esc(p.owner.email) : "—"), kv("Webhook signing secret", p.hooks.secret_set ? "Configured · hidden" : "Not configured"),
      kv("Secret environment variables", String(secretCount)), kv("Project paused", p.is_paused ? "Yes" : "No"));
    pad.append(K.btn("Manage environment secrets", { cls: "accent", icon: "key", href: "#/projects/" + p.slug + "/env" }));
    pad.append(K.btn("Rotate webhook URLs", { cls: "ghost", icon: "refresh", onClick: () => confirmModal({ title: "Rotate project hooks?", desc: "Existing hook URLs will stop working.", confirmLabel: "Rotate", danger: true, onConfirm: async () => { await Api.post("/api/projects/" + p.slug + "/hook/rotate"); toast("Hooks rotated"); K.render(); } }) }));
    sec.append(pad); body.append(sec);
    const note = el("div", "callout warn"); note.innerHTML = svg("info") + "<div>Project-level password protection for previews, secret scanning, dependency scanning and a managed WAF are not currently implemented. Do not treat these as enabled.</div>"; body.append(note);
  }));

  K.route("projects/:slug/identity", (a) => projectShell(a.slug, "identity", async (p, body) => {
    const card = moduleHeader("Identity & analytics", "Codeskys workspace identity and deployment analytics for this project.", "Available", "");
    const pad = el("div", "card-pad stack");
    pad.append(kv("Project owner", p.owner ? esc(p.owner.name) + " · " + esc(p.owner.email) : "—"), kv("Authentication boundary", "Codeskys student account and project ownership"));
    pad.append(K.btn("Open account security", { cls: "accent", href: "#/settings?s=security" }));
    pad.append(K.btn("Open project analytics", { cls: "ghost", href: "#/projects/" + p.slug + "/observability" }));
    card.append(pad); body.append(card);
    const note = el("div", "callout info"); note.innerHTML = svg("info") + "<div>Codeskys account sign-in and project ownership are active. An end-user authentication service (for users of the website you deploy) is a separate product module and is not yet provided by Codeskys.</div>"; body.append(note);
  }));

  K.route("projects/:slug/database", (a) => projectShell(a.slug, "database", async (p, body) => {
    featureUnavailable(body, "Managed database", "Provision and manage a database attached to this application.", "A database provider such as a self-hosted PostgreSQL service or a managed database API, plus isolated per-project provisioning and backup credentials.");
    body.append(K.btn("Configure database connection variables", { cls: "accent", icon: "key", href: "#/projects/" + p.slug + "/env" }));
    const warning = el("div", "callout warn"); warning.innerHTML = svg("info") + "<div>The SQLite database currently configured in Codeskys stores Codeskys's own metadata. It is not a managed database for your deployed application.</div>"; body.append(warning);
  }));
  K.route("projects/:slug/storage", (a) => projectShell(a.slug, "storage", async (_p, body) => {
    featureUnavailable(body, "Data & storage", "Manage object storage and persistent volumes for this project.", "An object storage or volume provider, storage credentials, lifecycle policies, access controls and backup configuration.");
  }));
  K.route("projects/:slug/functions", (a) => projectShell(a.slug, "functions", async (_p, body) => {
    featureUnavailable(body, "Functions", "Deploy request handlers, scheduled jobs and background functions.", "A sandboxed function runtime and lifecycle controller. The current container deployment must not be mislabeled as serverless functions.");
  }));
  K.route("projects/:slug/edge", (a) => projectShell(a.slug, "edge", async (_p, body) => {
    featureUnavailable(body, "Edge network", "Configure CDN caching and global content delivery.", "A CDN/edge provider, domain delegation, cache invalidation integration and edge analytics.");
  }));
  K.route("projects/:slug/docs", (a) => projectShell(a.slug, "docs", async (p, body) => {
    const card = el("div", "card");
    card.append(el("div", "card-head", "<div><h3>Project documentation</h3><p>Guides for the workflows available in this project.</p></div>"));
    const pad = el("div", "card-pad stack");
    [["Project overview", "#/projects/" + p.slug], ["Project configuration", "#/projects/" + p.slug + "/settings"],
     ["Environment variables", "#/projects/" + p.slug + "/env"], ["Domains", "#/projects/" + p.slug + "/domains"],
     ["Deployments & logs", "#/projects/" + p.slug + "/deployments"], ["DevOps & webhooks", "#/projects/" + p.slug + "/devops"],
     ["Forms", "#/projects/" + p.slug + "/forms"]].forEach(([label, href]) => pad.append(K.btn(label, { cls: "ghost", href })));
    card.append(pad); body.append(card);
  }));

  // ---------------------------------------------------------------- PROJECT FORMS
  K.route("projects/:slug/forms", (a) => projectShell(a.slug, "forms", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/forms");
    const card = el("div", "card");
    const head = el("div", "card-head", "<div><h3>Forms</h3><p>Collect submissions from your deployed site through a Codeskys-hosted endpoint.</p></div><div class='actions'></div>");
    $(".actions", head).append(K.btn("Create form", { cls: "accent", icon: "plus", onClick: () => openFormEditor(p) })); card.append(head);
    if (!d.forms.length) card.append(emptyState("forms", "No forms yet", "Create a form endpoint, then POST JSON from your website to its public URL.", K.btn("Create your first form", { cls: "accent", icon: "plus", onClick: () => openFormEditor(p) })));
    else d.forms.forEach((f) => {
      const row = el("div", "form-module-row");
      const info = el("div", "form-module-info", "<b>" + esc(f.name) + "</b><div class='muted small'>" + f.fields.length + " field(s) · " + f.submissions + " submission(s) · " + (f.is_active ? "Active" : "Paused") + "</div><code>" + esc(location.origin + f.endpoint) + "</code>");
      const actions = el("div", "actions");
      actions.append(K.btn("Copy endpoint", { cls: "ghost sm", icon: "copy", onClick: () => navigator.clipboard.writeText(location.origin + f.endpoint).then(() => toast("Form endpoint copied")) }));
      actions.append(K.btn("Submissions", { cls: "accent sm", icon: "inbox", href: "#/projects/" + p.slug + "/forms/" + f.id }));
      actions.append(K.btn(f.is_active ? "Pause" : "Activate", { cls: "ghost sm", onClick: async () => { await Api.patch("/api/projects/" + p.slug + "/forms/" + f.id, { is_active: !f.is_active }); toast("Form updated"); K.render(); } }));
      actions.append(K.btn("Delete", { cls: "danger-ghost sm", icon: "trash", onClick: () => confirmModal({ title: "Delete form?", desc: "All stored submissions for this form will also be deleted.", confirmLabel: "Delete form", danger: true, onConfirm: async () => { await Api.del("/api/projects/" + p.slug + "/forms/" + f.id); toast("Form deleted"); K.render(); } }) }));
      row.append(info, actions); card.append(row);
    });
    body.append(card);
    const docs = el("div", "callout info"); docs.innerHTML = svg("code") + "<div>Submission endpoint: <code>POST /api/public/forms/&lt;form-key&gt;</code>. Send JSON fields matching the form definition. The form endpoint is rate-limited; submissions are visible only to this project's owner and administrators.</div>"; body.append(docs);
  }));

  K.route("projects/:slug/forms/:formId", async (a) => projectShell(a.slug, "forms", async (p, body) => {
    const d = await Api.get("/api/projects/" + p.slug + "/forms/" + a.formId + "/submissions");
    const card = el("div", "card");
    const head = el("div", "card-head", "<div><h3>Form submissions</h3><p>" + d.total + " total · latest 500 shown</p></div><div class='actions'></div>");
    $(".actions", head).append(K.btn("Back to forms", { cls: "ghost", href: "#/projects/" + p.slug + "/forms" }));
    $(".actions", head).append(K.btn("Export CSV", { cls: "accent", icon: "download", onClick: () => exportSubmissionsCsv(d.submissions) })); card.append(head);
    if (!d.submissions.length) card.append(el("div", "card-pad muted", "No submissions have been received yet."));
    else d.submissions.forEach((s) => {
      const row = el("div", "form-submission");
      row.innerHTML = "<b>#" + s.id + "</b><span class='muted small'>" + esc(dateTime(s.created_at)) + "</span>";
      const data = el("div", "form-submission-data");
      Object.entries(s.data).forEach(([k, v]) => data.append(el("div", "form-submission-field", "<span>" + esc(k) + "</span><b>" + esc(typeof v === "boolean" ? (v ? "Yes" : "No") : v) + "</b>")));
      row.append(data); card.append(row);
    });
    body.append(card);
  }));

  function openFormEditor(p) {
    const wrap = el("div", "stack");
    const nf = el("div", "field"); nf.innerHTML = "<label>Form name</label>"; const name = el("input", "input"); name.placeholder = "Contact form"; name.value = "Contact form"; nf.append(name); wrap.append(nf);
    const fieldsWrap = el("div", "stack");
    const fields = [
      { name: "name", label: "Name", type: "text", required: true, max_length: 120 },
      { name: "email", label: "Email", type: "email", required: true, max_length: 254 },
      { name: "message", label: "Message", type: "textarea", required: true, max_length: 4000 },
    ];
    function paintFields() {
      clear(fieldsWrap);
      fields.forEach((f, i) => {
        const c = el("div", "form-builder-field");
        const g = el("div", "grid-2");
        const kn = inputField("Field key", f.name, "field_name"); kn.input.oninput = () => f.name = kn.input.value;
        const lb = inputField("Label", f.label, "Field label"); lb.input.oninput = () => f.label = lb.input.value;
        g.append(kn.field, lb.field);
        const g2 = el("div", "grid-2");
        const ty = el("div", "field"); ty.innerHTML = "<label>Type</label>"; const sel = el("select", "select"); sel.innerHTML = ["text", "email", "textarea", "number", "url", "checkbox"].map((x) => "<option value='" + x + "' " + (f.type === x ? "selected" : "") + ">" + x + "</option>").join(""); sel.onchange = () => f.type = sel.value; ty.append(sel);
        const req = mkCheck("Required", f.required); req.input.onchange = () => f.required = req.input.checked;
        g2.append(ty, req.wrap);
        const rm = K.btn("Remove field", { cls: "danger-ghost sm", icon: "trash", onClick: () => { fields.splice(i, 1); paintFields(); } });
        c.append(g, g2, rm); fieldsWrap.append(c);
      });
    }
    paintFields();
    const add = K.btn("Add field", { cls: "ghost", icon: "plus", onClick: () => { fields.push({ name: "field_" + (fields.length + 1), label: "New field", type: "text", required: false, max_length: 1000 }); paintFields(); } });
    wrap.append(el("div", "field", "<label>Fields</label>"), fieldsWrap, add);
    const sf = inputField("Success message", "Thanks — your response has been received.", "Thank you"); wrap.append(sf.field);
    const save = K.btn("Create form", { cls: "accent", icon: "check", onClick: async () => {
      try {
        const r = await Api.post("/api/projects/" + p.slug + "/forms", { name: name.value, fields, success_message: sf.input.value });
        K.closeLayer(); toast("Form created"); K.render();
      } catch (e) { toast(e.message, "error"); }
    } });
    openModal({ title: "Create project form", desc: "Create a public endpoint and manage incoming submissions in Codeskys.", body: wrap, wide: true, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), save] });
  }
  function exportSubmissionsCsv(submissions) {
    if (!submissions.length) return toast("There are no submissions to export.", "error");
    const headers = [...new Set(submissions.flatMap((x) => Object.keys(x.data)))];
    const quoteCsv = (v) => {
      let value = String(v == null ? "" : v);
      // Prevent spreadsheet formula execution when a submission is opened in Excel/Sheets.
      if (/^[\s\u0000-\u001f]*[=+\-@]/.test(value)) value = "'" + value;
      return '"' + value.replace(/"/g, '""') + '"';
    };
    const csv = [ ["id", "created_at", ...headers], ...submissions.map((x) => [x.id, x.created_at, ...headers.map((h) => x.data[h] == null ? "" : x.data[h])]) ].map((row) => row.map(quoteCsv).join(",")).join("\r\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = "codeskys-form-submissions.csv"; a.click(); URL.revokeObjectURL(url);
  }

  // ---------------------------------------------------------------- PROJECT SETTINGS TAB
  K.route("projects/:slug/settings", (a) => projectShell(a.slug, "settings", async (p, body) => {
    const stack = el("div", "stack");
    // ---- Project name: the name AND the live link (checked for availability as you type)
    const nameCard = el("div", "card setting"); nameCard.id = "name";
    nameCard.append(el("div", "card-head", "<div><h3>Project name</h3><p>Your project name is also part of its live link. Change the name and the link changes with it.</p></div>"));
    const nb = el("div", "card-pad"); nameCard.append(nb);
    const nameField = el("div", "field"); nameField.innerHTML = "<label for='pname'>Project name</label>";
    const nameWrap = el("div", "name-input");
    const ni = el("input", "input"); ni.id = "pname"; ni.value = p.name; ni.maxLength = 64; ni.autocomplete = "off"; ni.spellcheck = false;
    const mark = el("span", "name-mark"); mark.setAttribute("aria-live", "polite");
    nameWrap.append(ni, mark); nameField.append(nameWrap);
    const linkLine = el("div", "link-preview"); nameField.append(linkLine);
    const nameMsg = el("div", "name-msg"); nameField.append(nameMsg);
    nb.append(nameField);
    const nfoot = el("div", "card-foot"); const nact = el("div", "actions");
    const saveName = el("button", "btn accent", "Save changes"); saveName.disabled = true;
    nfoot.append(el("span", "muted small", p.link_editable ? "" : "This app’s link can’t be renamed yet; only its display name changes."), nact); nact.append(saveName); nameCard.append(nfoot);
    const linkFor = (slug) => absUrl(liveHref(p)).replace("/live/" + p.slug + "/", "/live/" + slug + "/").replace("/sites/" + p.slug + "/", "/sites/" + slug + "/").replace("//" + p.slug + ".", "//" + slug + ".");
    const showLink = (slug) => { linkLine.innerHTML = svg("link", "sm") + "<span>Your link: <b>" + esc(linkFor(slug).replace(/^https?:\/\//, "")) + "</b></span>"; };
    showLink(p.slug);
    let verdict = { ok: false, slug: p.slug, same: true };
    let checkTimer = null, checkSeq = 0;
    function paintVerdict(state, msg) {
      mark.className = "name-mark " + state;
      mark.innerHTML = state === "ok" ? svg("check", "sm") : state === "bad" ? svg("x", "sm") : state === "busy" ? "<span class='spinner'></span>" : "";
      mark.title = state === "ok" ? "Available" : state === "bad" ? "Not available" : "";
      nameMsg.className = "name-msg " + state; nameMsg.textContent = msg || "";
    }
    function refreshSave() {
      const changed = ni.value.trim() !== p.name || (verdict.slug !== p.slug && p.link_editable);
      saveName.disabled = !(verdict.ok && changed);
    }
    async function runCheck() {
      const v = ni.value.trim(); const seq = ++checkSeq;
      if (!v) { verdict = { ok: false, slug: p.slug, same: true }; paintVerdict("bad", "Type a project name."); refreshSave(); return; }
      if (!p.link_editable) { verdict = { ok: true, slug: p.slug, same: true }; paintVerdict("", ""); refreshSave(); return; }
      paintVerdict("busy", "Checking…"); saveName.disabled = true;
      try {
        const r = await Api.get("/api/projects/" + p.slug + "/link-check?name=" + encodeURIComponent(v));
        if (seq !== checkSeq) return;           // a newer keystroke is already being checked
        verdict = { ok: r.available, slug: r.slug || p.slug, same: r.same };
        showLink(r.slug || p.slug);
        if (r.same) paintVerdict("", "This is your current link.");
        else if (r.available) paintVerdict("ok", "Available. Your link will be " + (r.slug || ""));
        else paintVerdict("bad", r.reason || "That name is not available.");
      } catch (e) { if (seq === checkSeq) { verdict = { ok: false, slug: p.slug, same: true }; paintVerdict("bad", e.message); } }
      refreshSave();
    }
    ni.addEventListener("input", () => { clearTimeout(checkTimer); verdict = { ok: false, slug: p.slug, same: true }; saveName.disabled = true; checkTimer = setTimeout(runCheck, 350); });
    saveName.onclick = async () => {
      const newName = ni.value.trim();
      const linkChanges = p.link_editable && verdict.slug !== p.slug;
      const doSave = async () => {
        const body = { name: newName }; if (linkChanges) body.slug = verdict.slug;
        const r = await Api.patch("/api/projects/" + p.slug, body);
        toast(linkChanges ? "Project name and link updated" : "Project name updated");
        if (linkChanges) go("#/projects/" + r.project.slug + "/settings"); else K.render();
      };
      if (!linkChanges) { spin(saveName, "Saving…"); try { await doSave(); } catch (e) { toast(e.message, "error"); unspin(saveName); refreshSave(); } return; }
      const b = el("div"); b.innerHTML = "<p class='muted'>Your live link is changing:</p><div class='link-change'><div><span class='muted small'>Old link</span><code>" + esc(linkFor(p.slug).replace(/^https?:\/\//, "")) + "</code></div><div><span class='muted small'>New link</span><code>" + esc(linkFor(verdict.slug).replace(/^https?:\/\//, "")) + "</code></div></div><p class='muted small' style='margin-top:12px'>The old link stops working straight away, so update anywhere you have shared it.</p>";
      confirmModal({ title: "Change your live link?", body: b, confirmLabel: "Change name and link", onConfirm: doSave });
    };
    refreshSave();

    // ---- Description: its own card, separate from the name
    const descCard = settingCard("Description", "A short note about what this project is. It does not affect your link.");
    const df = el("div", "field"); df.innerHTML = "<label for='pdesc'>Description</label>";
    const di = el("textarea", "textarea"); di.id = "pdesc"; di.rows = 3; di.maxLength = 300; di.value = p.description || ""; di.style.height = "auto"; di.style.padding = "9px 11px";
    df.append(di); descCard.body.append(df);
    descCard.save(async () => { await Api.patch("/api/projects/" + p.slug, { description: di.value }); });

    // build settings
    const build = settingCard("Build & development", "How Codeskys builds and runs your project.");
    const bf = el("div", "field"); bf.innerHTML = "<label>Framework</label>"; const bs = el("select", "select");
    const fws = (await Api.get("/api/frameworks")).frameworks;
    bs.innerHTML = fws.map((f) => "<option value='" + f.id + "' " + (f.id === p.framework ? "selected" : "") + ">" + esc(f.name) + "</option>").join(""); bf.append(bs);
    const brf = el("div", "field"); brf.innerHTML = "<label>Production branch</label>"; const bri = el("input", "input mono"); bri.value = p.branch; brf.append(bri);
    const g2 = el("div", "grid-2"); g2.append(bf, brf);
    const inst = inputField("Install command", p.install_command, p.defaults.install_command);
    const bld = inputField("Build command", p.build_command, p.defaults.build_command);
    const outp = inputField("Output directory", p.output_dir, p.defaults.output_dir);
    const start = inputField("Start command", p.start_command, p.defaults.start_command);
    const g3 = el("div", "grid-2"); const rootF = inputField("Root directory", p.root_dir, "./"); const portF = inputField("Port", String(p.port), "");
    g3.append(rootF.field, portF.field);
    build.body.append(g2, inst.field, bld.field, outp.field, start.field, g3);
    build.save(async () => { await Api.patch("/api/projects/" + p.slug, { framework: bs.value, branch: bri.value.trim(), install_command: inst.input.value, build_command: bld.input.value, output_dir: outp.input.value, start_command: start.input.value, root_dir: rootF.input.value, port: portF.input.value || p.port }); });

    // git / auto deploy
    const git = settingCard("Git & auto-deploy", "Control how new commits are deployed.");
    const auto = toggleRow("Automatic deployments", "Deploy automatically when the deploy hook is called.", p.auto_deploy);
    const prev = toggleRow("Preview deployments", "Create preview deployments for non-production branches.", p.preview_deploys);
    git.body.append(auto.row, prev.row);
    // The free 24 hours are a plan rule, so only an administrator can change a project's lifetime.
    const ttlF = el("div", "field");
    ttlF.innerHTML = "<label>Deployment lifetime (hours) <span class='badge'>Admin</span></label><div class='hint'>Free projects are torn down automatically once they reach this age.</div>";
    const ttlI = el("input", "input"); ttlI.type = "number"; ttlI.min = "1"; ttlI.value = p.ttl_hours; ttlF.append(ttlI);
    if (State.user.role === "admin") git.body.append(ttlF);
    const hook = el("div", "field"); hook.style.marginTop = "6px";
    hook.innerHTML = "<label>Webhook URLs</label><div class='hint'>Add the matching URL in your repository settings so every push redeploys. GitHub signs with the secret; GitLab sends it as a token.</div>";
    hook.append(el("div", "muted small", "GitHub — Settings → Webhooks (content type: application/json)"));
    hook.append(copyBox(p.hooks.github));
    hook.append(el("div", "muted small", "GitLab — Settings → Webhooks (Secret token)"));
    hook.append(copyBox(p.hooks.gitlab));
    hook.append(el("div", "muted small", "Plain CI hook (POST, no signature)"));
    hook.append(copyBox(p.hooks.generic));
    const secretNote = el("div", "callout info");
    secretNote.innerHTML = svg("lock") + "<div>The signing secret is stored on the server and never shown here. Use <b>Rotate</b> to issue a new one, then update it in your repository.</div>";
    hook.append(secretNote);
    const rotate = K.btn("Rotate hook URL", { cls: "ghost sm", icon: "refresh", onClick: () => confirmModal({ title: "Rotate deploy hook?", desc: "The current URL will stop working immediately.", confirmLabel: "Rotate", onConfirm: async () => { await Api.post("/api/projects/" + p.slug + "/hook/rotate"); toast("Hook rotated"); K.render(); } }) });
    hook.append(rotate);
    git.body.append(hook);
    git.save(async () => { await Api.patch("/api/projects/" + p.slug, { auto_deploy: auto.input.checked, preview_deploys: prev.input.checked, ...(State.user.role === "admin" ? { ttl_hours: ttlI.value } : {}) }); });

    // danger
    const danger = el("div", "card setting"); danger.style.borderColor = "var(--bad)";
    danger.append(el("div", "card-head", "<div><h3 style='color:var(--bad)'>Danger zone</h3></div>"));
    const dbody = el("div", "card-pad stack");
    const pauseRow = el("div", "row"); pauseRow.innerHTML = "<div><b>" + (p.is_paused ? "Resume project" : "Pause project") + "</b><div class='muted small'>" + (p.is_paused ? "Allow deployments again." : "Block all new deployments for this project.") + "</div></div><span class='spacer'></span>";
    pauseRow.append(K.btn(p.is_paused ? "Resume" : "Pause", { onClick: () => togglePause(p) }));
    const delRow = el("div", "row"); delRow.innerHTML = "<div><b>Delete this project</b><div class='muted small'>Permanently remove the project and all its data.</div></div><span class='spacer'></span>";
    delRow.append(K.btn("Delete project", { cls: "danger", icon: "trash", onClick: () => deleteProject(p) }));
    dbody.append(pauseRow, el("div", "divider"), delRow); danger.append(dbody);

    stack.append(nameCard, descCard.card, build.card, git.card, authorizationCard(p), liveAppCard(p), danger);
    body.append(stack);
  }));

  // Codeskys authorization: links this project to the student's existing
  // authorization. The credential is sent once to the server, verified by the
  // authorization service, and never stored or shown again.
  function authorizationCard(p) {
    const a = p.authorization || {};
    const card = el("div", "card setting");
    card.append(el("div", "card-head", "<div><h3>Codeskys authorization</h3><p>Lets this project's real deployments use protected Codeskys services.</p></div>"));
    const bd = el("div", "card-pad stack"); card.append(bd);
    if (a.linked) {
      bd.append(el("div", "callout info", svg("lock") + "<div><b>Linked.</b> Repository verified with GitHub" +
        (a.github_repo_id ? " (repository id <code>" + esc(String(a.github_repo_id)) + "</code>)" : " on the next deployment") +
        ". Each real deployment gets its own short-lived credential, which is revoked when the deployment expires, fails or is deleted.</div>"));
      return card;
    }
    bd.append(el("div", "muted small", "Not linked. Your app can still be deployed, but protected Codeskys services will refuse it. " +
      "If you already have a Codeskys deployment credential (it starts with <code>dpl_live_</code>), link it here. " +
      "It is checked once and never stored."));
    const cf = el("div", "field"); cf.innerHTML = "<label>Deployment credential</label>";
    const ci = el("input", "input mono"); ci.type = "password"; ci.autocomplete = "off"; ci.spellcheck = false; ci.placeholder = "dpl_live_…"; cf.append(ci);
    const inf = el("div", "field"); inf.innerHTML = "<label>GitHub App installation id <span class='muted small'>(private repositories only)</span></label>";
    const ii = el("input", "input mono"); ii.inputMode = "numeric"; ii.placeholder = "e.g. 179150342"; inf.append(ii);
    const b = el("button", "btn accent", "Link authorization");
    b.onclick = async () => {
      const credential = ci.value.trim();
      if (!credential) { toast("Enter your deployment credential.", "error"); return; }
      spin(b, "Verifying…");
      try {
        const body = { credential };
        if (ii.value.trim()) body.github_installation_id = ii.value.trim();
        await Api.post("/api/projects/" + p.slug + "/authorization/link", body);
        ci.value = "";
        toast("Authorization linked");
        K.render();
      } catch (e) { ci.value = ""; toast(e.message, "error"); unspin(b); }
    };
    bd.append(cf, inf, b);
    return card;
  }

  // Live application controls, for real (Coolify) deployments only.
  function liveAppCard(p) {
    const card = el("div", "card setting");
    card.append(el("div", "card-head", "<div><h3>Live application</h3><p>Start, stop or restart the running app without rebuilding it.</p></div>"));
    const bd = el("div", "card-pad stack"); card.append(bd);
    if (p.provider !== "coolify" || !p.provider_ref) {
      bd.append(el("div", "muted small", "Available after this project's first real deployment. Simulated deployments do not run an app."));
      return card;
    }
    const state = el("div", "row", "<span class='muted small'>Status: <b>" + esc(p.app_state || "unknown") + "</b></span><span class='spacer'></span>");
    const refresh = K.btn("Refresh", { cls: "ghost sm", icon: "refresh", onClick: async () => {
      try { const s = await Api.get("/api/projects/" + p.slug + "/app"); toast("Status: " + s.status); K.render(); }
      catch (e) { toast(e.message, "error"); } } });
    state.append(refresh);
    const actions = el("div", "row");
    const act = (label, action, cls) => K.btn(label, { cls: cls || "ghost", onClick: async () => {
      try { await Api.post("/api/projects/" + p.slug + "/app/" + action); toast(label + " requested"); K.render(); }
      catch (e) { toast(e.message, "error"); } } });
    actions.append(act("Start", "start"), act("Restart", "restart"),
      K.btn("Stop", { cls: "danger", onClick: () => confirmModal({ title: "Stop the live app?", desc: "Visitors will get an error until you start it again.", confirmLabel: "Stop", onConfirm: async () => { await Api.post("/api/projects/" + p.slug + "/app/stop"); toast("Stopped"); K.render(); } }) }));
    bd.append(state, actions);
    return card;
  }
  function settingCard(title, desc) {
    const card = el("div", "card setting");
    card.append(el("div", "card-head", "<div><h3>" + esc(title) + "</h3>" + (desc ? "<p>" + esc(desc) + "</p>" : "") + "</div>"));
    const bd = el("div", "card-pad"); card.append(bd);
    const foot = el("div", "card-foot"); const actions = el("div", "actions"); foot.append(el("span", "muted small", ""), actions); 
    function save(fn) { const b = el("button", "btn accent", "Save changes"); b.onclick = async () => { spin(b, "Saving…"); try { await fn(); toast("Saved"); unspin(b); } catch (e) { toast(e.message, "error"); unspin(b); } }; actions.append(b); card.append(foot); }
    return { card, body: bd, save };
  }
  function inputField(label, val, placeholder) { const field = el("div", "field"); field.innerHTML = "<label>" + esc(label) + "</label>"; const input = el("input", "input mono"); input.value = val || ""; if (placeholder) input.placeholder = placeholder; field.append(input); return { field, input }; }
  function toggleRow(title, desc, on) { const row = el("div", "row"); row.style.padding = "8px 0"; row.innerHTML = "<div style='flex:1'><b>" + esc(title) + "</b><div class='muted small'>" + esc(desc) + "</div></div>"; const lab = el("label", "switch"); const input = el("input"); input.type = "checkbox"; input.checked = !!on; lab.append(input, el("span")); row.append(lab); return { row, input }; }
  function copyBox(text) { const box = el("div", "copy"); box.append(el("span", "", esc(text))); const b = el("button", "", svg("copy", "sm")); b.onclick = () => navigator.clipboard.writeText(text).then(() => toast("Copied")); box.append(b); return box; }

  // ---------------------------------------------------------------- GLOBAL DEPLOYMENTS
  K.route("deployments", async (_a, params) => {
    K.setNav("deployments"); skeletonPage();
    const status = params.status || "";
    const d = await Api.get("/api/deployments" + (status ? "?status=" + status : ""));
    State.buildingCount = d.deployments.filter((x) => x.status === "building" || x.status === "queued").length;
    K.setNav("deployments");
    K.setCrumbs([{ label: "Deployments" }]);
    const head = K.pageHead("Deployments", "Every build across your workspace.");
    const seg = el("div", "seg");
    [["", "All"], ["building", "Building"], ["ready", "Ready"], ["failed", "Failed"]].forEach(([k, l]) => { const b = el("button", k === status ? "on" : "", l); b.onclick = () => go("#/deployments" + (k ? "?status=" + k : "")); seg.append(b); });
    const toolbar = el("div", "toolbar"); toolbar.append(seg);
    const card = el("div", "card");
    if (!d.deployments.length) card.append(emptyState("deployments", "No deployments", status ? "No deployments with this status." : "Deploy a project to see builds here.", K.btn("New project", { cls: "accent", icon: "plus", href: "#/new" })));
    else d.deployments.forEach((dep) => {
      const row = el("div", "dep-row" + (dep.is_latest ? "" : " history"));
      if (!dep.is_latest) row.title = "Older deployments are kept as history. Open the project's latest deployment to make changes.";
      row.innerHTML = "<div><div class='row'><b class='ellipsis'>" + esc(dep.project_name) + "</b>" + (dep.is_latest ? "<span class='badge accent'>Latest</span>" : "") + "</div><div class='meta'>#" + dep.number + " · " + esc(dep.trigger) + (dep.is_current ? " · <span style='color:var(--live)'>production</span>" : "") + (dep.expires_at ? " · " + (dep.status === "expired" ? "expired " : "expires ") + shortDate(dep.expires_at) : "") + "</div></div>" +
        "<div>" + statusChip(dep.status) + "</div>" +
        "<div class='commit'><span class='sha'>" + esc((dep.commit_sha || "").slice(0, 7)) + "</span><span class='msg'>" + esc(dep.commit_message || dep.branch) + "</span></div>" +
        "<div class='meta'><div class='branch'>" + svg("branch", "sm") + esc(dep.branch) + "</div><div>" + timeAgo(dep.created_at) + "</div></div><div>" + (dep.is_latest ? svg("chevron", "sm") : "") + "</div>";
      if (dep.is_latest) row.onclick = () => go("#/projects/" + dep.project_slug + "/deployments/" + dep.id);
      card.append(row);
    });
    setView(view(head, toolbar, card));
  });

  // ---------------------------------------------------------------- ANALYTICS
  K.route("analytics", async (_a, params) => {
    K.setNav("analytics"); skeletonPage();
    const days = params.days || "30";
    const d = await Api.get("/api/analytics?days=" + days);
    K.setNav("analytics");
    K.setCrumbs([{ label: "Analytics" }]);
    const seg = el("div", "seg");
    [["7", "7d"], ["30", "30d"], ["90", "90d"]].forEach(([k, l]) => { const b = el("button", k === days ? "on" : "", l); b.onclick = () => go("#/analytics?days=" + k); seg.append(b); });
    const head = K.pageHead("Analytics", "Deployment volume, success rate and build performance.", [seg]);

    const stats = el("div", "stats");
    stats.append(stat("deployments", "Deployments", String(d.total), "in " + d.days + " days"),
      stat("analytics", "Success rate", d.total ? Math.round((d.ready / d.total) * 100) + "%" : "—", d.ready + " succeeded"),
      stat("clock", "Median build", d.p50_ms ? duration(d.p50_ms) : "—", d.p95_ms ? "p95 " + duration(d.p95_ms) : ""),
      stat("bolt", "Build minutes", String(d.build_minutes), "compute used"));

    const grid = el("div", "layout-2");
    const left = el("div", "stack");
    const volCard = el("div", "card");
    volCard.append(el("div", "card-head", "<div><h3>Deployments over time</h3></div><div class='actions'><span class='legend'><span><i class='ok'></i>Ready</span><span><i class='bad'></i>Failed</span></span></div>"));
    const vp = el("div", "card-pad"); vp.innerHTML = bars(d.daily); volCard.append(vp); left.append(volCard);

    const projCard = el("div", "card");
    projCard.append(el("div", "card-head", "<div><h3>By project</h3></div>"));
    const pbody = el("div", "card-pad");
    if (!d.projects.length) pbody.innerHTML = "<p class='muted'>No data yet.</p>";
    else { const max = Math.max(...d.projects.map((p) => p.total)); d.projects.slice(0, 8).forEach((p) => { const h = el("div", "hbar"); h.innerHTML = "<span class='ellipsis'>" + esc(p.name) + "</span><div class='track'><div class='fill' style='width:" + Math.round((p.total / max) * 100) + "%'></div></div><span class='num'>" + p.total + "</span>"; pbody.append(h); }); }
    projCard.append(pbody); left.append(projCard);

    const right = el("div", "stack");
    const perfCard = el("div", "card");
    perfCard.append(el("div", "card-head", "<div><h3>Build duration</h3><p>Last " + d.durations.length + " successful builds</p></div>"));
    const perfBody = el("div", "card-pad");
    if (d.durations.length > 1) perfBody.innerHTML = lineChart(d.durations.map((x) => ({ v: x.ms })));
    else perfBody.innerHTML = "<p class='muted'>Not enough builds yet.</p>";
    perfCard.append(perfBody); right.append(perfCard);

    const fwCard = el("div", "card");
    fwCard.append(el("div", "card-head", "<div><h3>Frameworks</h3></div>"));
    const fwBody = el("div", "card-pad");
    if (d.frameworks.length) { const max = Math.max(...d.frameworks.map((f) => f.count)); d.frameworks.forEach((f) => { const h = el("div", "hbar"); h.innerHTML = "<span class='ellipsis'>" + esc(f.name) + "</span><div class='track'><div class='fill' style='width:" + Math.round((f.count / max) * 100) + "%'></div></div><span class='num'>" + f.count + "</span>"; fwBody.append(h); }); } else fwBody.innerHTML = "<p class='muted'>No data yet.</p>";
    fwCard.append(fwBody); right.append(fwCard);

    grid.append(left, right);
    setView(view(head, stats, grid));
  });

  // ---------------------------------------------------------------- ACTIVITY
  K.route("activity", async () => {
    K.setNav("activity"); skeletonPage();
    const d = await Api.get("/api/activity");
    K.setNav("activity");
    K.setCrumbs([{ label: "Activity" }]);
    const head = K.pageHead("Activity", "A log of everything happening across your workspace.");
    const card = el("div", "card");
    if (!d.activity.length) card.append(el("div", "card-pad muted", "No activity yet."));
    else { const ul = el("ul", "feed"); d.activity.forEach((x) => { const li = activityItem(x); if (x.project) { const b = li.querySelector(".txt"); b.append(el("div", "muted small", "in " + x.project)); } ul.append(li); }); card.append(ul); }
    setView(view(head, card));
  });

  // ---------------------------------------------------------------- TEAM (admin)
  K.route("team", async () => {
    K.setNav("team"); skeletonPage();
    const d = await Api.get("/api/admin/users");
    K.setNav("team");
    K.setCrumbs([{ label: "Team" }]);
    const head = K.pageHead("Team", d.users.length + " members in this workspace", [K.btn("Invite member", { cls: "accent", icon: "plus", onClick: () => addMember() })]);
    const card = el("div", "card");
    const table = el("table", "table");
    table.innerHTML = "<thead><tr><th>Member</th><th>Role</th><th>Plan</th><th>Projects</th><th>Last active</th><th></th></tr></thead>";
    const tb = el("tbody");
    d.users.forEach((u) => {
      const tr = el("tr");
      tr.innerHTML = "<td><div class='row'><span class='avatar sm'>" + esc(u.name[0] || "?") + "</span><div style='min-width:0'><b class='ellipsis'>" + esc(u.name) + "</b><div class='muted small ellipsis'>" + esc(u.email) + "</div></div></div></td>" +
        "<td><span class='badge " + (u.role === "admin" ? "accent" : "") + "'>" + cap(u.role) + "</span></td>" +
        "<td>" + planName(u.plan) + "</td><td>" + u.projects + "</td>" +
        "<td class='muted small'>" + (u.last_login_at ? timeAgo(u.last_login_at) : "never") + "</td>";
      const act = el("td"); act.style.textAlign = "right";
      if (!u.is_active) act.append(el("span", "badge bad", "Deactivated"));
      if (u.locked) act.append(el("span", "badge warn", "Locked"));
      const m = el("button", "btn sm ghost", svg("dots", "sm")); m.onclick = () => memberMenu(m, u); act.append(m);
      tr.append(act); tb.append(tr);
    });
    table.append(tb);
    const tw = el("div", "table-wrap"); tw.append(table); card.append(tw);
    setView(view(head, card));
  });
  function memberMenu(anchor, u) {
    const items = [{ head: u.name, sub: u.email }];
    items.push({ label: u.role === "admin" ? "Make member" : "Make admin", icon: "shield", action: () => patchUser(u.id, { role: u.role === "admin" ? "student" : "admin" }) });
    items.push({ label: "Change plan", icon: "billing", action: () => changePlan(u) });
    items.push({ label: "Reset password", icon: "key", action: () => patchUser(u.id, { reset_password: true }, true) });
    if (u.locked) items.push({ label: "Unlock account", icon: "lock", action: () => patchUser(u.id, { unlock: true }) });
    items.push({ sep: true });
    items.push({ label: u.is_active ? "Deactivate" : "Reactivate", icon: u.is_active ? "pause" : "play", danger: u.is_active, action: () => patchUser(u.id, { is_active: !u.is_active }) });
    K.dropdown(anchor, items);
  }
  async function patchUser(id, payload, showTemp) {
    try { const r = await Api.patch("/api/admin/users/" + id, payload);
      if (showTemp && r.temporary_password) showSecret("Temporary password", "Share this with the member. It won’t be shown again.", r.temporary_password);
      else toast("Member updated");
      K.render(); }
    catch (e) { toast(e.message, "error"); }
  }
  function changePlan(u) {
    const b = el("div"); const f = el("div", "field"); f.innerHTML = "<label>Plan</label>";
    const s = el("select", "select"); s.innerHTML = [{id:"hobby",label:"Free"},{id:"pro",label:"Pro"},{id:"team",label:"Premium"}].map((p) => "<option value='" + p.id + "' " + (p.id === u.plan ? "selected" : "") + ">" + p.label + "</option>").join(""); f.append(s); b.append(f);
    confirmModal({ title: "Change plan for " + u.name, body: b, confirmLabel: "Update plan", onConfirm: async () => { await Api.patch("/api/admin/users/" + u.id, { plan: s.value }); toast("Plan updated"); K.render(); } });
  }
  function addMember() {
    const b = el("div");
    const nf = el("div", "field"); nf.innerHTML = "<label>Full name</label>"; const ni = el("input", "input"); nf.append(ni);
    const ef = el("div", "field"); ef.innerHTML = "<label>Email address</label>"; const ei = el("input", "input"); ei.type = "email"; ef.append(ei);
    const g = el("div", "grid-2");
    const rf = el("div", "field"); rf.innerHTML = "<label>Role</label>"; const rs = el("select", "select"); rs.innerHTML = "<option value='student'>Member</option><option value='admin'>Admin</option>"; rf.append(rs);
    const pf = el("div", "field"); pf.innerHTML = "<label>Plan</label>"; const ps = el("select", "select"); ps.innerHTML = "<option value='hobby'>Free</option><option value='pro'>Student (6 months)</option><option value='lifetime'>Lifetime</option><option value='team'>Premium</option>"; pf.append(ps);
    g.append(rf, pf);
    b.append(nf, ef, g);
    const co = el("div", "callout info"); co.innerHTML = svg("info") + "<div>Codeskys generates a temporary password you can share. The member can change it after signing in.</div>";
    b.append(co);
    const save = el("button", "btn accent", "Create member");
    save.onclick = async () => {
      spin(save, "Creating…");
      try { const r = await Api.post("/api/admin/users", { name: ni.value.trim(), email: ei.value.trim(), role: rs.value, plan: ps.value });
        K.closeLayer(); showSecret("Member created", "Share this temporary password with " + r.user.name + ".", r.temporary_password); K.render(); }
      catch (e) { toast(e.message, "error"); unspin(save); }
    };
    openModal({ title: "Invite a member", body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), save] });
  }
  function showSecret(title, desc, secret) {
    const b = el("div");
    const box = el("div", "token-new"); box.append(el("div", "secret-reveal", esc(secret)));
    const cp = K.btn("Copy", { cls: "sm", icon: "copy", onClick: () => navigator.clipboard.writeText(secret).then(() => toast("Copied")) });
    cp.style.marginTop = "10px"; box.append(cp);
    b.append(box);
    openModal({ title, desc, body: b, foot: [K.btn("Done", { cls: "accent", onClick: K.closeLayer })] });
  }

  // ---------------------------------------------------------------- INTEGRATIONS (admin)
  K.route("integrations", async () => {
    K.setNav("integrations"); skeletonPage();
    const d = await Api.get("/api/admin/integrations");
    K.setNav("integrations");
    K.setCrumbs([{ label: "Integrations" }]);
    const head = K.pageHead("Integrations", "Connect the infrastructure that builds and hosts your projects.");

    // engine selector
    const engine = el("div", "card");
    engine.append(el("div", "card-head", "<div><h3>Deployment engine</h3><p>Where builds actually run.</p></div>"));
    const ebody = el("div", "card-pad");
    const activeCoolify = d.active_provider === "coolify";
    const banner = el("div", "callout " + (activeCoolify ? "live" : "info"));
    banner.innerHTML = svg(activeCoolify ? "check" : "info") + "<div>" + (activeCoolify
      ? "Builds are running on <b>your Coolify server</b>." 
      : "Builds are running on the <b>built-in simulator</b> — full pipeline, logs and URLs, without real containers. Connect Coolify below to build for real.") + "</div>";
    ebody.append(banner);
    const seg = el("div", "seg"); seg.style.marginTop = "14px";
    const simB = el("button", d.provider === "simulator" ? "on" : "", svg("bolt", "sm") + " Simulator");
    const cooB = el("button", d.provider === "coolify" ? "on" : "", svg("shield", "sm") + " Coolify");
    simB.onclick = () => setProvider("simulator"); cooB.onclick = () => setProvider("coolify");
    seg.append(simB, cooB); ebody.append(seg);
    engine.append(ebody);

    // coolify config
    const cool = el("div", "card");
    const ch = el("div", "card-head", "<div><h3>" + svg("cube", "sm") + " Coolify</h3><p>Self-hosted PaaS that runs your builds and containers.</p></div><div class='actions'></div>");
    if (d.coolify.last_version) $(".actions", ch).append(el("span", "badge live", "v" + d.coolify.last_version));
    cool.append(ch);
    const cbody = el("div", "card-pad");
    const urlF = inputField("Coolify instance URL", d.coolify.url, "https://coolify.yourdomain.com");
    const tokF = inputField("API token", "", d.coolify.token_set ? "Saved (" + (d.coolify.token_hint || "····") + ") — enter a new token to replace" : "Bearer token from Coolify → Keys & Tokens");
    tokF.input.type = "password";
    const g = el("div", "grid-2");
    const projF = inputField("Project UUID", d.coolify.project_uuid, "from Coolify project URL");
    const servF = inputField("Server UUID", d.coolify.server_uuid, "from Coolify server settings");
    g.append(projF.field, servF.field);
    const g2 = el("div", "grid-2");
    const envF = inputField("Environment name", d.coolify.environment_name, "production");
    const domF = inputField("Wildcard base domain", d.coolify.base_domain, "apps.yourdomain.com");
    g2.append(envF.field, domF.field);
    cbody.append(urlF.field, tokF.field, g, g2);
    const hint = el("div", "callout info");
    hint.innerHTML = svg("info") + "<div>In Coolify, create an API token with <b>deploy</b>, <b>read</b> and <b>write</b> permissions. The base domain should point a wildcard DNS record (<code>*.apps.yourdomain.com</code>) at your Coolify server so each project gets its own URL.</div>";
    cbody.append(hint);
    cool.append(cbody);
    const cfoot = el("div", "card-foot");
    const statusTxt = el("span", "muted small", d.coolify.last_check ? "Last check: " + esc(d.coolify.last_check) : "Not tested yet");
    const cact = el("div", "actions");
    const testBtn = K.btn("Test connection", { icon: "refresh" });
    const saveBtn = K.btn("Save", { cls: "accent" });
    cact.append(testBtn, saveBtn); cfoot.append(statusTxt, cact); cool.append(cfoot);

    async function saveCoolify() {
      const payload = { url: urlF.input.value.trim(), project_uuid: projF.input.value.trim(), server_uuid: servF.input.value.trim(), environment_name: envF.input.value.trim() || "production", base_domain: domF.input.value.trim() };
      if (tokF.input.value.trim()) payload.token = tokF.input.value.trim();
      await Api.post("/api/admin/integrations/coolify", payload);
    }
    saveBtn.onclick = async () => { spin(saveBtn, "Saving…"); try { await saveCoolify(); toast("Coolify settings saved"); K.render(); } catch (e) { toast(e.message, "error"); unspin(saveBtn); } };
    testBtn.onclick = async () => {
      spin(testBtn, "Testing…");
      try { await saveCoolify(); const r = await Api.post("/api/admin/integrations/coolify/test");
        unspin(testBtn); showCoolifyTest(r, projF, servF); }
      catch (e) { unspin(testBtn); toast(e.message, "error"); }
    };
    async function setProvider(p) {
      try { const r = await Api.post("/api/admin/integrations/provider", { provider: p }); if (r.warning) toast(r.warning, "error"); else toast("Engine set to " + cap(p)); K.render(); }
      catch (e) { toast(e.message, "error"); }
    }

    // other integrations (roadmap, honest about status)
    const others = el("div", "card");
    others.append(el("div", "card-head", "<div><h3>More integrations</h3><p>Planned for upcoming phases.</p></div>"));
    const ol = el("div", "list");
    [["git", "GitHub App", "Private repositories, PR previews and commit status checks", "Phase 2"],
     ["billing", "Payments", "Plan upgrades, invoices and metered build minutes", "Phase 4"],
     ["key", "Private credentials", "Per-project credential issuing and revocation", "Phase 4"],
     ["notifications", "Slack & email alerts", "Deployment notifications to your team channels", "Phase 3"]].forEach(([ic, name, desc, phase]) => {
      const li = el("div"); li.style.cssText = "display:flex;gap:14px;align-items:center;padding:14px 20px;border-top:1px solid var(--line)";
      li.innerHTML = "<span class='feed'><span class='ico'>" + svg(ic, "sm") + "</span></span><div style='flex:1'><b>" + esc(name) + "</b><div class='muted small'>" + esc(desc) + "</div></div><span class='badge'>" + esc(phase) + "</span>";
      ol.append(li);
    });
    others.append(ol);
    const stack = el("div", "stack");
    stack.append(engine, cool, others);
    setView(view(head, stack));
  });
  function showCoolifyTest(r, projF, servF) {
    const b = el("div");
    b.innerHTML = "<div class='callout live'>" + svg("check") + "<div>Connected to Coolify <b>" + esc(String(r.version)) + "</b></div></div>";
    function pick(title, rows, field) {
      const c = el("div"); c.style.marginTop = "14px";
      c.append(el("div", "nav-label", title));
      if (!rows.length) { c.append(el("p", "muted small", "None found.")); return c; }
      rows.forEach((x) => {
        const row = el("button", "pal-item", svg("cube", "sm") + "<span>" + esc(x.name || x.uuid) + "</span><span class='hint'>" + esc(x.uuid || "") + "</span>");
        row.onclick = () => { field.input.value = x.uuid; toast("Selected " + (x.name || x.uuid)); };
        c.append(row);
      });
      return c;
    }
    b.append(pick("Servers — click to use", r.servers || [], servF));
    b.append(pick("Projects — click to use", r.projects || [], projF));
    const note = el("p", "muted small", "After selecting, press Save to store the UUIDs.");
    b.append(note);
    openModal({ title: "Coolify connection", body: b, wide: true, foot: [K.btn("Done", { cls: "accent", onClick: K.closeLayer })] });
  }

  // ---------------------------------------------------------------- ACCOUNT SETTINGS
  K.route("settings", async (_a, params) => {
    K.setNav("settings"); skeletonPage();
    const tokens = (await Api.get("/api/tokens")).tokens;
    K.setNav("settings");
    K.setCrumbs([{ label: "Settings" }]);
    const head = K.pageHead("Settings", "Your account, security and developer access.");
    const layout = el("div", "layout-side");
    const nav = el("div", "subnav");
    [["account", "Account"], ["git", "Git accounts"], ["security", "Security"], ["tokens", "API tokens"],
     ["appearance", "Appearance"]].forEach(([id, label]) => {
      const a = el("a", (params.s || "account") === id ? "on" : "", label); a.href = "#/settings?s=" + id; nav.append(a);
    });
    const main = el("div", "stack");

    // account
    const acc = settingCard("Profile", "How you appear across Codeskys.");
    const nf = el("div", "field"); nf.innerHTML = "<label>Full name</label>"; const ni = el("input", "input"); ni.value = State.user.name; nf.append(ni);
    const ef = el("div", "field"); ef.innerHTML = "<label>Email address</label>"; const ei = el("input", "input"); ei.value = State.user.email; ei.disabled = true; ef.append(ei);
    ef.append(el("div", "hint", "Contact an administrator to change your email address."));
    acc.body.append(nf, ef);
    acc.save(async () => { const r = await Api.patch("/api/account", { name: ni.value.trim() }); State.user = r.user; buildSidebar(); $("#avatar-btn").textContent = r.user.name[0]; });
    acc.card.id = "account";

    // git accounts
    const git = el("div", "card setting"); git.id = "git";
    git.append(el("div", "card-head", "<div><h3>Connected Git accounts</h3><p>Connect an account to deploy private repositories and pick from a list instead of pasting URLs.</p></div>"));
    const gbody = el("div", "card-pad stack");
    try {
      const provs = (await Api.get("/api/git/providers")).providers;
      provs.forEach((pr) => {
        const row = el("div", "row");
        row.style.cssText = "padding:10px 0;gap:12px";
        row.innerHTML = svg("git", "lg") + "<div style='flex:1;min-width:0'><b>" + esc(pr.label) + "</b>" +
          "<div class='muted small'>" + (pr.connected ? "Connected as " + esc(pr.username) : "Not connected") + "</div></div>";
        if (pr.connected) {
          row.append(K.btn("Disconnect", { cls: "danger-ghost sm", icon: "x", onClick: () => disconnectGit(pr) }));
        } else {
          row.append(K.btn("Connect", { cls: "accent sm", icon: "link", onClick: () => connectGit(pr) }));
        }
        gbody.append(row);
      });
      if (!provs.some((x) => x.configured) && State.user.role === "admin") {
        const co = el("div", "callout info");
        co.innerHTML = svg("info") + "<div>No Git provider is configured yet. An administrator sets <b>GITHUB_CLIENT_ID</b> / <b>GITHUB_CLIENT_SECRET</b> (and the GitLab pair) in the environment, using the callback URL shown in the setup guide.</div>";
        gbody.append(co);
      }
    } catch (e) { gbody.append(el("p", "muted", "Could not load Git providers.")); }
    git.append(gbody);

    // security
    const sec = settingCard("Password", "Use at least 12 characters, with upper and lower case letters, a number and a symbol.");
    const cf = el("div", "field"); cf.innerHTML = "<label>Current password</label>"; const ci = el("input", "input"); ci.type = "password"; cf.append(ci);
    const g = el("div", "grid-2");
    const n1 = el("div", "field"); n1.innerHTML = "<label>New password</label>"; const ni1 = el("input", "input"); ni1.type = "password"; n1.append(ni1);
    const n2 = el("div", "field"); n2.innerHTML = "<label>Confirm new password</label>"; const ni2 = el("input", "input"); ni2.type = "password"; n2.append(ni2);
    g.append(n1, n2);
    sec.body.append(cf, g);
    ci.autocomplete = "current-password"; ni1.autocomplete = "new-password"; ni2.autocomplete = "new-password";
    PwTools.eye(ci);
    const w1 = PwTools.eye(ni1), w2 = PwTools.eye(ni2);
    const pm = PwTools.meter(ni1); n1.append(pm.node);
    sec.body.append(PwTools.suggest([ni1, ni2], [w1, w2]));
    sec.save(async () => {
      if (!ci.value) throw new Error("Enter your current password.");
      if (PwTools.score(ni1.value).met < PwTools.RULES.length) throw new Error("Choose a stronger password. Follow the checklist under the new password.");
      if (ni1.value !== ni2.value) throw new Error("The new passwords don’t match.");
      await Api.post("/api/account/password", { current: ci.value, new: ni1.value });
      ci.value = ni1.value = ni2.value = "";
      ni1.dispatchEvent(new Event("input"));
      [w1, w2].forEach((w) => w._reveal && w._reveal(false));
    });
    sec.card.id = "security";

    // tokens
    const tk = el("div", "card setting"); tk.id = "tokens";
    const tkh = el("div", "card-head", "<div><h3>API tokens</h3><p>Use tokens to drive Codeskys from scripts and CI.</p></div><div class='actions'></div>");
    $(".actions", tkh).append(K.btn("Create token", { cls: "accent", icon: "plus", onClick: () => createToken() }));
    tk.append(tkh);
    if (!tokens.length) tk.append(el("div", "card-pad muted", "No tokens yet. Create one to use the Codeskys API."));
    else {
      const list = el("div", "list");
      tokens.forEach((t) => {
        const li = el("div"); li.style.cssText = "display:flex;gap:14px;align-items:center;padding:14px 20px;border-top:1px solid var(--line)";
        li.innerHTML = svg("key", "sm") + "<div style='flex:1;min-width:0'><b>" + esc(t.name) + "</b><div class='muted small mono'>" + esc(t.prefix) + "••••••••</div></div>" +
          "<div class='muted small' style='text-align:right'>Created " + timeAgo(t.created_at) + "<br>" + (t.last_used_at ? "Used " + timeAgo(t.last_used_at) : "Never used") + "</div>";
        const del = el("button", "btn sm danger-ghost", svg("trash", "sm"));
        del.onclick = () => confirmModal({ title: "Revoke " + t.name + "?", desc: "Any script using this token will stop working immediately.", confirmLabel: "Revoke", danger: true, onConfirm: async () => { await Api.del("/api/tokens/" + t.id); toast("Token revoked"); K.render(); } });
        li.append(del); list.append(li);
      });
      tk.append(list);
    }
    const apiHelp = el("div", "card-foot");
    apiHelp.innerHTML = "<span class='muted small mono'>curl -H \"Authorization: Bearer kova_…\" " + esc(location.origin) + "/api/projects</span>";
    tk.append(apiHelp);

    // appearance
    const ap = el("div", "card setting"); ap.id = "appearance";
    ap.append(el("div", "card-head", "<div><h3>Appearance</h3><p>Choose how Codeskys looks on this device.</p></div>"));
    const apb = el("div", "card-pad");
    const themeSeg = el("div", "seg");
    const cur = document.documentElement.getAttribute("data-theme");
    const dk = el("button", cur === "dark" ? "on" : "", svg("moon", "sm") + " Dark");
    const lt = el("button", cur === "light" ? "on" : "", svg("sun", "sm") + " Light");
    dk.onclick = () => { applyTheme("dark"); dk.classList.add("on"); lt.classList.remove("on"); };
    lt.onclick = () => { applyTheme("light"); lt.classList.add("on"); dk.classList.remove("on"); };
    themeSeg.append(dk, lt); apb.append(themeSeg); ap.append(apb);

    main.append(acc.card, git, sec.card, tk, ap);
    layout.append(nav, main);
    setView(view(head, layout));
    const target = params.s && document.getElementById(params.s);
    if (target) target.scrollIntoView({ block: "start", behavior: "smooth" });
  });
  async function connectGit(pr) {
    if (!pr.configured) {
      // Nothing is broken on the person's side: the platform just has not been given this
      // provider's keys yet. Say so plainly and offer the ways that already work.
      const b = el("div");
      const admin = State.user.role === "admin";
      b.innerHTML = "<p class='muted' style='margin-top:0'>Signing in with " + esc(pr.label) + " isn’t switched on for this workspace yet.</p>" +
        (admin ? "<p>To switch it on, add <b>" + esc(pr.provider.toUpperCase()) + "_CLIENT_ID</b> and <b>" + esc(pr.provider.toUpperCase()) + "_CLIENT_SECRET</b> to the server settings, then restart. The setup guide has the exact steps.</p>"
                 : "<p>The Codeskys team needs to finish this setup first. Until then you can <b>upload your files</b> or <b>paste a public GitHub URL</b> to deploy.</p>");
      openModal({ title: "Connect " + pr.label, body: b, foot: [K.btn("Upload files", { cls: "ghost", href: "#/new?tab=upload", onClick: closeLayer }), K.btn("Got it", { cls: "accent", onClick: closeLayer })] });
      return;
    }
    try {
      const { url } = await Api.get("/api/git/" + pr.provider + "/authorize");
      window.location.assign(url);
    } catch (e) { toast(e.message, "error"); }
  }
  function disconnectGit(pr) {
    confirmModal({
      title: "Disconnect " + pr.label + "?",
      desc: "Projects already created keep working, but private repositories will stop deploying until you reconnect.",
      confirmLabel: "Disconnect", danger: true,
      onConfirm: async () => { await Api.del("/api/git/" + pr.provider); toast(pr.label + " disconnected"); K.render(); },
    });
  }

  function createToken() {
    const b = el("div");
    const nf = el("div", "field"); nf.innerHTML = "<label>Token name</label>"; const ni = el("input", "input"); ni.placeholder = "GitHub Actions"; nf.append(ni);
    const ef = el("div", "field"); ef.innerHTML = "<label>Expires</label>"; const es = el("select", "select");
    es.innerHTML = "<option value=''>Never</option><option value='30'>30 days</option><option value='90'>90 days</option><option value='365'>1 year</option>"; ef.append(es);
    b.append(nf, ef);
    const save = el("button", "btn accent", "Create token");
    save.onclick = async () => {
      spin(save, "Creating…");
      try { const r = await Api.post("/api/tokens", { name: ni.value.trim(), expires_days: es.value || null });
        K.closeLayer(); showSecret("Token created", "Copy it now — Codeskys won’t show it again.", r.token); K.render(); }
      catch (e) { toast(e.message, "error"); unspin(save); }
    };
    openModal({ title: "Create API token", body: b, foot: [K.btn("Cancel", { cls: "ghost", onClick: K.closeLayer }), save] });
  }

  // ---------------------------------------------------------------- BILLING
  const PLANS = [
    { id: "hobby", name: "Free", price: "₹0", per: "", feats: ["1 project, live for 24 hours", "Update and redeploy as often as you like inside the 24 hours", "Same expiry for every update", "100 MB per upload", "Free Codeskys link", "Community support"] },
    { id: "pro", name: "Student · 6 months", price: "₹510", per: " / 6 months", feats: ["Your projects stay live for 6 months", "Up to 100 projects", "3,000 build minutes / month", "Custom domains + SSL", "Email support"] },
    { id: "lifetime", name: "Lifetime", price: "₹1,599", per: " · pay once", best: true, feats: ["Your projects stay live for life", "Up to 100 projects", "3,000 build minutes / month", "Custom domains + SSL", "No renewals, ever"] }
  ];
  const planName = (id) => ({ hobby: "Free", pro: "Student", lifetime: "Lifetime", team: "Premium" }[id] || cap(id));
  const periodLabel = (days) => days >= 3650 ? " · pay once" : days === 180 ? " / 6 months" : " / " + days + " days";
  K.route("billing", async () => {
    K.setNav("billing"); skeletonPage();
    const d = await Api.get("/api/overview");
    K.setNav("billing");
    K.setCrumbs([{ label: "Usage & Billing" }]);
    const head = K.pageHead("Usage & billing", "Your plan, current usage and available upgrades.");
    const lim = d.limits;
    const usage = el("div", "card");
    usage.append(el("div", "card-head", "<div><h3>Current usage</h3><p>Billing period: last 30 days</p></div><div class='actions'><span class='badge accent'>" + planName(d.plan) + " plan</span></div>"));
    const ub = el("div", "card-pad");
    ub.append(usageRow("Projects", d.stats.projects, lim.projects));
    ub.append(usageRow("Build minutes", Math.round(d.stats.build_minutes), lim.build_minutes));
    ub.append(usageRow("Concurrent builds", d.stats.building, lim.concurrent));
    ub.append(usageRow("Deployments", d.stats.deploys_30d, lim.build_minutes));
    usage.append(ub);

    let b = { enabled: false, catalog: [], payments: [], account: { plan: d.plan } };
    try { b = await Api.get("/api/billing"); } catch (_) {}
    let U = { enabled: false, offers: [], payments: [] };
    try { U = await Api.get("/api/upi"); } catch (_) {}
    K.setNav("billing");
    const acct = b.account || { plan: d.plan };
    const status = el("div", "callout info");
    const paidNow = hostingActive(acct.hosting_until);
    status.innerHTML = svg("info") + "<div><b>" + esc(cap(acct.plan)) + " plan</b>" +
      (acct.plan_expires_at ? " · active until " + esc(new Date(acct.plan_expires_at).toLocaleDateString()) :
        acct.managed_by_admin ? " · managed by an administrator" :
        paidNow ? "" : " · your project is live for 24 hours; upgrade to keep it online") +
      (paidNow ? (isLifetime(acct.hosting_until) ? " · <b>Lifetime hosting</b>, nothing more to pay" : " · <b>Paid hosting</b> until " + esc(new Date(acct.hosting_until).toLocaleString())) : "") + "</div>";

    const plans = el("div", "plans");
    let note;
    if (b.enabled && b.checkout_open) {
      b.catalog.forEach((c) => {
        const card = el("div", "card plan");
        const price = "₹" + (c.amount / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
        const forever = c.period_days >= 3650;
        card.innerHTML = "<h3>" + esc(c.name) + (forever ? " <span class='badge live'>Best value</span>" : "") + (b.mode === "test" ? " <span class='badge warn'>Test mode</span>" : "") +
          "</h3><div class='price'>" + price + "<small>" + esc(periodLabel(c.period_days)) + "</small></div>";
        const ul = el("ul");
        const hosting = c.grants_plan === "hosting";
        const lim = (PLANS.find((x) => x.id === c.grants_plan) || {}).feats;
        (hosting ? [c.description, forever ? "Pay once. No renewals, ever." : "Renewing adds " + Math.round(c.period_days / 30) + " months to your end date",
                    "No 24-hour limit while it is paid: same live URL, no rebuild", forever ? "Up to 100 projects" : "Up to 100 projects, 24-hour grace after it ends"]
          : [c.description, cap(c.grants_plan) + " plan limits for " + c.period_days + " days", "Renewing extends your current end date"].concat(lim || []))
          .filter(Boolean).forEach((f) => ul.append(el("li", "", svg("check", "sm") + "<span>" + esc(f) + "</span>")));
        card.append(ul);
        const haveLifetime = hosting && isLifetime(acct.hosting_until);
        const label = hosting ? (haveLifetime ? "You have lifetime hosting" : forever ? "Get Lifetime " + price : hostingActive(acct.hosting_until) ? "Renew " + price : "Upgrade to Paid Hosting " + price)
          : acct.plan === c.grants_plan ? "Renew" : "Pay " + price;
        const btn = K.btn(label, { cls: "accent", onClick: () => startCheckout(c, btn) });
        if ((acct.managed_by_admin && !hosting) || haveLifetime) btn.disabled = true;
        card.append(btn);
        plans.append(card);
      });
      note = el("div", "callout info");
      note.innerHTML = svg("info") + "<div>" + (b.mode === "test"
        ? "<b>Test mode.</b> Payments go through the Razorpay sandbox. No real money is charged."
        : "Payments are processed by Razorpay. Your access updates as soon as the payment is confirmed.") + "</div>";
    } else if (U.enabled && U.offers.length) {
      note = upiPlanCards(plans, U, acct);
    } else {
      PLANS.forEach((p) => {
        const c = el("div", "card plan" + (p.id === d.plan ? " current" : ""));
        c.innerHTML = "<h3>" + p.name + (p.id === d.plan ? " <span class='badge accent'>Current</span>" : p.best ? " <span class='badge live'>Best value</span>" : "") + "</h3><div class='price'>" + p.price + "<small>" + p.per + "</small></div>";
        const ul = el("ul"); p.feats.forEach((f) => ul.append(el("li", "", svg("check", "sm") + "<span>" + esc(f) + "</span>")));
        c.append(ul);
        const bt = K.btn(p.id === d.plan ? "Current plan" : p.unavailable ? "Not available yet" : "Upgrade to " + p.name, { cls: p.id === d.plan || p.unavailable ? "" : "accent", onClick: () => toast("Payments are not open yet. Plans are set by an administrator for now.") });
        if (p.id === d.plan || p.unavailable) bt.disabled = true;
        c.append(bt);
        plans.append(c);
      });
      note = el("div", "callout info");
      note.innerHTML = svg("info") + "<div><b>Billing is not connected yet.</b> These are the configured plan prices; no payment is collected from this screen. Plan changes are currently administrator-managed, and plan limits are enforced by the API.</div>";
    }

    const hist = el("div", "card");
    hist.append(el("div", "card-head", "<div><h3>Billing history</h3><p>Payments and receipts</p></div>"));
    if (!b.payments.length) hist.append(el("div", "card-pad muted", "No payments yet."));
    else {
      const table = el("table", "table");
      table.innerHTML = "<thead><tr><th>Date</th><th>Plan</th><th>Amount</th><th>Status</th><th></th></tr></thead>";
      const tb = el("tbody");
      const tone = { paid: "live", pending: "warn", created: "", failed: "bad", refunded: "", partially_refunded: "warn" };
      const label = { created: "Not completed", partially_refunded: "Partly refunded" };
      b.payments.forEach((p) => {
        const tr = el("tr");
        tr.innerHTML = "<td>" + esc(new Date(p.created_at).toLocaleString()) + "</td><td>" + esc(p.plan) + "</td><td>" +
          esc(p.currency) + " " + (p.amount / 100).toFixed(2) + (p.mode === "test" ? " <span class='badge warn'>test</span>" : "") +
          "</td><td><span class='badge " + (tone[p.status] || "") + "'>" + esc(label[p.status] || cap(p.status)) + "</span>" +
          (p.failure_reason ? " <span class='muted small'>" + esc(p.failure_reason) + "</span>" : "") + "</td><td style='text-align:right'>" +
          (p.receipt_available ? "<a class='link small' target='_blank' rel='noopener' href='/billing/receipts/" + encodeURIComponent(p.ref) + "'>Receipt</a>" : "") + "</td>";
        tb.append(tr);
      });
      table.append(tb);
      hist.append(table);
    }
    const waiting = (U.payments || []).find((x) => x.status === "submitted");
    let verifying = null;
    if (waiting) {
      verifying = el("div", "callout warn"); verifying.style.marginBottom = "16px";
      verifying.dataset.keep = "1";
      verifying.innerHTML = svg("clock") + "<div><b>Payment verification in progress.</b> " + esc(money(waiting.amount)) + " for " + esc(waiting.plan_name) + " (" + esc(waiting.ref) + "). Verification takes up to 8 hours; your plan activates automatically once it is approved.</div>";
      verifying.append(K.btn("View status", { cls: "sm", href: "#/pay/" + waiting.ref }));
    }
    const upiHist = upiHistoryCard(U);
    setView(view(...[head, verifying, status, usage, el("div", "divider"), plans, note, upiHist, el("div", "divider"), hist].filter(Boolean)));
  });

  // ---------------------------------------------------------------- UPI PAYMENTS
  // Pay with any UPI app -> type the transaction ID -> an administrator approves it.
  const money = (paise) => "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const durationLabel = (p) => (p.forever || p.period_days >= 3650) ? "Lifetime" : Math.round(p.period_days / 30) + " months";
  const upiTone = { awaiting_payment: "", submitted: "warn", approved: "live", rejected: "bad", cancelled: "" };
  const upiLabel = { awaiting_payment: "Awaiting payment", submitted: "Verifying", approved: "Approved", rejected: "Not verified", cancelled: "Cancelled" };
  function copyButton(text, label) {
    const b = el("button", "copy-btn", svg("copy", "sm")); b.type = "button"; b.title = "Copy " + (label || "");
    b.onclick = () => navigator.clipboard.writeText(text).then(() => toast("Copied")).catch(() => toast("Couldn’t copy. Select the text instead.", "error"));
    return b;
  }
  async function startUpiOrder(code, btn) {
    spin(btn, "Preparing…");
    try { const r = await Api.post("/api/upi/orders", { plan: code }); go("#/pay/" + r.payment.ref); }
    catch (e) { toast(e.message, "error"); unspin(btn); }
  }
  // The three plan cards on Usage & Billing when payments go through UPI.
  function upiPlanCards(plans, U, acct) {
    const paid = hostingActive(acct.hosting_until);
    const free = el("div", "card plan" + (paid ? "" : " current"));
    free.innerHTML = "<h3>" + PLANS[0].name + (paid ? "" : " <span class='badge accent'>Current</span>") + "</h3><div class='price'>₹0</div>";
    const fu = el("ul"); PLANS[0].feats.forEach((f) => fu.append(el("li", "", svg("check", "sm") + "<span>" + esc(f) + "</span>")));
    free.append(fu);
    const fb = K.btn(paid ? "Included" : "Current plan", {}); fb.disabled = true; free.append(fb);
    plans.append(free);
    U.offers.forEach((o) => {
      const forever = o.period_days >= 3650, haveLifetime = isLifetime(acct.hosting_until);
      const card = el("div", "card plan");
      card.innerHTML = "<h3>" + esc(o.name) + (forever ? " <span class='badge live'>Best value</span>" : "") + "</h3><div class='price'>" + esc(money(o.amount).replace(/\.00$/, "")) + "<small>" + esc(forever ? " · pay once" : " / " + durationLabel(o)) + "</small></div>";
      const ul = el("ul");
      [o.description, forever ? "Pay once. No renewals, ever." : "Renewing adds " + durationLabel(o) + " to your end date",
       "No 24-hour limit while it is paid: same live URL, no rebuild", "Up to 100 projects"].forEach((f) => ul.append(el("li", "", svg("check", "sm") + "<span>" + esc(f) + "</span>")));
      card.append(ul);
      const label = haveLifetime ? "You have lifetime hosting" : forever ? "Get Lifetime · " + money(o.amount).replace(/\.00$/, "") : paid ? "Renew · " + money(o.amount).replace(/\.00$/, "") : "Upgrade · " + money(o.amount).replace(/\.00$/, "");
      const btn = K.btn(label, { cls: "accent", onClick: () => startUpiOrder(o.code, btn) });
      if (haveLifetime) btn.disabled = true;
      card.append(btn);
      plans.append(card);
    });
    const note = el("div", "callout info");
    note.innerHTML = svg("info") + "<div><b>Pay with any UPI app</b> (Google Pay, PhonePe, Paytm, BHIM and more). After you pay, enter the transaction ID. Verification takes up to 8 hours, and your plan switches on automatically once it is approved.</div>";
    return note;
  }
  function upiHistoryCard(U) {
    if (!U.payments || !U.payments.length) return null;
    const card = el("div", "card"); card.style.marginTop = "16px";
    card.append(el("div", "card-head", "<div><h3>UPI payments</h3><p>Your payment requests and receipts</p></div>"));
    const table = el("table", "table");
    table.innerHTML = "<thead><tr><th>Date</th><th>Plan</th><th>Amount</th><th>Status</th><th></th></tr></thead>";
    const tb = el("tbody");
    U.payments.filter((p) => p.status !== "cancelled").forEach((p) => {
      const tr = el("tr");
      tr.innerHTML = "<td>" + esc(new Date(p.created_at).toLocaleString()) + "</td><td>" + esc(p.plan_name) + "</td><td>" + esc(money(p.amount)) + "</td><td><span class='badge " + (upiTone[p.status] || "") + "'>" + esc(p.status_label) + "</span></td><td style='text-align:right'></td>";
      const last = tr.lastChild;
      if (p.status === "approved") last.append(Object.assign(el("a", "link small", "Receipt"), { href: "/upi/receipts/" + encodeURIComponent(p.ref), target: "_blank", rel: "noopener" }));
      else last.append(Object.assign(el("a", "link small", p.status === "submitted" ? "View status" : "Continue"), { href: "#/pay/" + p.ref }));
      tb.append(tr);
    });
    table.append(tb); card.append(table);
    return card;
  }

  // The payment page: receipt + "Pay" + "Enter transaction ID" + "Verifying".
  let payTimer = null;
  K.route("pay/:ref", async (a) => {
    clearInterval(payTimer);
    K.setNav("billing"); skeletonPage();
    let pay;
    try { pay = (await Api.get("/api/upi/payments/" + encodeURIComponent(a.ref))).payment; } catch (e) { return showError(e); }
    K.setNav("billing");
    K.setCrumbs([{ label: "Usage & Billing", href: "#/billing" }, { label: "Payment " + pay.ref }]);
    const root = el("div");
    setView(view(K.pageHead("Complete your payment", "Pay with UPI and enter your transaction ID. Your plan switches on automatically once the payment is verified (up to 8 hours)."), root));

    function steps() {
      const now = pay.status === "approved" ? 3 : pay.status === "submitted" ? 2 : pay.status === "cancelled" ? -1 : pay.status === "rejected" ? 1 : 0;
      const ol = el("ol", "pay-steps");
      ["Pay with UPI", "Transaction ID", "Verification", "Plan active"].forEach((t, i) => {
        ol.append(el("li", i < now ? "done" : i === now ? "now" : "", "<span class='n'>" + (i < now ? svg("check", "sm") : (i + 1)) + "</span><span class='t'>" + esc(t) + "</span>"));
      });
      return ol;
    }
    function row(label, value, copyText) {
      const r = el("div", "rc-row"); r.append(el("span", "k", esc(label)));
      const v = el("span", "v", value); if (copyText) v.append(copyButton(copyText, label)); r.append(v); return r;
    }
    function receiptCard() {
      const c = el("div", "card receipt");
      c.append(el("div", "card-head", "<div><h3>Order summary</h3><p>Receipt " + esc(pay.ref) + "</p></div><div class='actions'><span class='badge " + (upiTone[pay.status] || "") + "'>" + esc(pay.status_label) + "</span></div>"));
      const b = el("div", "card-pad rc");
      b.append(row("Order reference", "<b class='mono'>" + esc(pay.ref) + "</b>", pay.ref));
      b.append(row("Billed to", esc(pay.user_name)));
      b.append(row("Plan", esc(pay.plan_name)));
      b.append(row("Hosting period", esc(durationLabel(pay))));
      b.append(row("Pay to", "Codeskys <span class='muted'>· " + esc(pay.upi_id || State.upiId || "UPI") + "</span>"));
      if (pay.utr) b.append(row("Transaction ID", "<b class='mono'>" + esc(pay.utr) + "</b>"));
      if (pay.status === "approved") b.append(row("Hosting active until", pay.period_days >= 3650 ? "Lifetime" : esc(dateTime(pay.entitlement_until))));
      const total = el("div", "rc-total"); total.innerHTML = "<span>Total to pay</span><b>" + esc(money(pay.amount)) + "</b>"; b.append(total);
      b.append(el("p", "muted small", "This is a payment receipt, not a GST tax invoice."));
      c.append(b);
      return c;
    }
    function actionCard() {
      const c = el("div", "card pay-action"); const b = el("div", "card-pad"); c.append(b);
      if (pay.status === "approved") {
        b.append(el("div", "pay-big ok", svg("check")));
        b.append(el("h2", "", "Your plan has been successfully upgraded"));
        b.append(el("p", "muted", pay.period_days >= 3650
          ? "Thank you. Lifetime hosting is now active on your account. Your projects stay live for life, with no renewals."
          : "Thank you. Paid hosting is now active until <b>" + esc(dateTime(pay.entitlement_until)) + "</b>. Your live projects have been extended automatically, with the same links and no rebuild."));
        const act = el("div", "pay-actions");
        act.append(K.btn("Go to dashboard", { cls: "accent", icon: "overview", href: "#/" }),
                   K.btn("View deployments", { icon: "deployments", href: "#/deployments" }),
                   K.btn("Download receipt", { cls: "ghost", icon: "book", href: "/upi/receipts/" + encodeURIComponent(pay.ref) }));
        b.append(act); return c;
      }
      if (pay.status === "submitted") {
        b.append(el("div", "pay-big wait", svg("clock")));
        b.append(el("h2", "", "Payment verification in progress"));
        b.append(el("p", "muted", "Thank you. We have received your transaction ID <b class='mono'>••••••••" + esc((pay.utr || "").slice(-4)) + "</b>. Verification takes <b>up to 8 hours</b>. Your plan activates automatically as soon as it is approved, and you can safely close this page."));
        const tl = el("ul", "pay-timeline");
        tl.innerHTML = "<li class='done'>" + svg("check", "sm") + "<span>Transaction ID received <small>" + esc(timeAgo(pay.submitted_at)) + "</small></span></li><li class='now'><i></i><span>Verification in progress <small>up to 8 hours</small></span></li><li><i></i><span>Plan activated</span></li>";
        b.append(tl);
        const act = el("div", "pay-actions");
        const chk = K.btn("Check status", { cls: "ghost", icon: "refresh", onClick: async () => { spin(chk, "Checking…"); await poll(true); unspin(chk); } });
        act.append(chk, K.btn("Back to billing", { href: "#/billing" })); b.append(act); return c;
      }
      if (pay.status === "cancelled") {
        b.append(el("h2", "", "This payment request was replaced"));
        b.append(el("p", "muted", "You started a newer payment request. Open it from the Billing page."));
        b.append(K.btn("Back to billing", { cls: "accent", href: "#/billing" })); return c;
      }
      if (pay.status === "rejected") {
        const w = el("div", "callout bad"); w.style.marginBottom = "16px";
        w.innerHTML = svg("alert") + "<div><b>We couldn’t verify this payment.</b> " + esc(pay.note || "") + " Check the transaction ID and enter it again. You have " + pay.attempts_left + " " + (pay.attempts_left === 1 ? "try" : "tries") + " left.</div>";
        b.append(w);
      }
      // Step 1: pay (the QR code sits at the top right, so a computer user can scan it with their phone)
      const s1 = el("div", "pay-step"); s1.innerHTML = "<span class='n'>1</span><div><h3>Pay " + esc(money(pay.amount)) + " with your UPI app</h3><p class='muted'>On a phone this opens your UPI app with the amount already filled in.</p></div>";
      const s1head = el("div", "pay-s1head"); s1head.append(s1);
      const qrFile = { 51000: "upi-qr-510.png", 159900: "upi-qr-1599.png" }[pay.amount];
      if (qrFile) {
        const qr = el("button", "pay-qr", "<img src='/static/images/" + qrFile + "' alt='UPI QR code for " + esc(money(pay.amount)) + "' width='128' height='128'><span>" + svg("scan", "sm") + " Scan to pay</span>");
        qr.type = "button"; qr.title = "Click to enlarge";
        qr.onclick = () => {
          const m = el("div", "qr-modal");
          m.innerHTML = "<img src='/static/images/" + qrFile + "' alt='UPI QR code' width='300' height='300'><p><b>" + esc(money(pay.amount)) + "</b> to Codeskys</p><p class='muted small'>Open Google Pay, PhonePe, Paytm or any UPI app on your phone, tap <b>Scan</b>, and pay. Then enter your transaction ID on this page.</p>";
          openModal({ title: "Scan to pay with any UPI app", body: m, foot: [K.btn("Close", { cls: "ghost", onClick: closeLayer })] });
        };
        s1head.append(qr);
      }
      b.append(s1head);
      const methods = el("div", "pay-methods");
      const pl = document.createElement("a"); pl.className = "btn accent lg block"; pl.href = pay.pay_link;
      pl.innerHTML = svg("bolt", "sm") + "<span>Pay " + esc(money(pay.amount)) + " with UPI</span>";
      const nb = el("button", "btn lg block", svg("billing", "sm") + "<span>Pay with Net Banking</span>"); nb.type = "button";
      nb.onclick = () => {
        const m = el("div"); m.innerHTML = "<div class='callout bad'>" + svg("alert") + "<div><b>Net Banking is not available for your account.</b> This payment option is available only for international customers. Customers in India can pay instantly and securely with UPI.</div></div>";
        openModal({ title: "Net Banking unavailable", body: m, foot: [K.btn("Pay with UPI instead", { cls: "accent", href: pay.pay_link, onClick: closeLayer }), K.btn("Close", { cls: "ghost", onClick: closeLayer })] });
      };
      methods.append(pl, nb); b.append(methods);
      b.append(el("p", "muted small pay-mobile", svg("info", "sm") + "<span>UPI payment links work on mobile phones with a UPI app installed (Google Pay, PhonePe, Paytm, BHIM). On a computer, scan the QR code with your phone, or use the UPI ID below.</span>"));
      const alt = el("div", "pay-alt");
      alt.append(row("UPI ID", "<b class='mono'>" + esc(pay.upi_id) + "</b>", pay.upi_id));
      alt.append(row("Amount", "<b>" + esc(money(pay.amount)) + "</b>"));
      alt.append(row("Note / remark", "<b class='mono'>" + esc(pay.ref) + "</b>"));
      b.append(alt);
      // Share or copy the payment link (to pay from another phone).
      const shareText = "Pay " + money(pay.amount) + " to Codeskys via UPI. UPI ID: " + pay.upi_id + " · Reference: " + pay.ref + " · Payment link (open on a phone): " + pay.pay_link;
      const share = el("div", "pay-share");
      share.append(el("span", "muted small", "Pay from another phone:"));
      const sb = el("div", "pay-share-btns");
      const mkS = (label, icon, fn) => { const x = el("button", "btn sm ghost", svg(icon, "sm") + "<span>" + esc(label) + "</span>"); x.type = "button"; x.onclick = fn; return x; };
      sb.append(
        mkS("Copy link", "copy", () => navigator.clipboard.writeText(pay.pay_link).then(() => toast("Payment link copied")).catch(() => toast("Couldn’t copy the link.", "error"))),
        mkS("WhatsApp", "link", () => window.open("https://wa.me/?text=" + encodeURIComponent(shareText), "_blank", "noopener,noreferrer")),
        mkS("Email", "link", () => { location.href = "mailto:?subject=" + encodeURIComponent("Codeskys payment " + pay.ref) + "&body=" + encodeURIComponent(shareText); }));
      if (navigator.share) sb.append(mkS("More…", "link", () => navigator.share({ title: "Codeskys payment", text: shareText }).catch(() => {})));
      share.append(sb); b.append(share);
      // Step 2: transaction ID
      const s2 = el("div", "pay-step"); s2.style.marginTop = "22px";
      s2.innerHTML = "<span class='n'>2</span><div><h3>Enter your transaction ID</h3><p class='muted'>After paying, open the payment in your UPI app and copy the 12-digit <b>UPI Ref No.</b> (also shown as <b>UTR</b> or <b>Transaction ID</b>). Verification takes up to 8 hours.</p></div>";
      b.append(s2);
      const f = el("div", "field"); f.style.marginTop = "10px";
      const ti = el("input", "input mono otp-like"); ti.placeholder = "12-digit transaction ID"; ti.inputMode = "numeric"; ti.maxLength = 14; ti.autocomplete = "off";
      ti.addEventListener("input", () => { ti.value = ti.value.replace(/\D/g, "").slice(0, 12); err.textContent = ""; });
      const err = el("div", "name-msg bad");
      f.append(ti, err); b.append(f);
      const sub = el("button", "btn accent lg block", "Submit");
      sub.onclick = async () => {
        if (ti.value.length !== 12) { err.textContent = "Enter the 12-digit transaction ID from your UPI app."; ti.focus(); return; }
        spin(sub, "Submitting…");
        try {
          const r = await Api.post("/api/upi/payments/" + encodeURIComponent(pay.ref) + "/submit", { utr: ti.value });
          pay = r.payment; toast("Submitted. We’ll verify it shortly."); paint(); startPolling();
        } catch (e) { err.textContent = e.message; unspin(sub); }
      };
      ti.addEventListener("keydown", (e) => { if (e.key === "Enter") sub.click(); });
      b.append(sub);
      b.append(el("p", "muted small", "Entered it wrongly? You can submit it again. Never share your UPI PIN with anyone; Codeskys will never ask for it."));
      return c;
    }
    function paint() {
      clear(root);
      const grid = el("div", "pay-grid"); grid.append(receiptCard(), actionCard());
      root.append(steps(), grid);
    }
    async function poll(manual) {
      try {
        const r = await Api.get("/api/upi/payments/" + encodeURIComponent(pay.ref));
        if (r.payment.status !== pay.status) {
          pay = r.payment; clearInterval(payTimer); paint();
          if (pay.status === "approved") { toast("Payment approved. Your plan is active."); K.refreshNotifBadge && K.refreshNotifBadge(); }
          else if (pay.status === "rejected") toast("We couldn’t verify that payment.", "error");
        } else if (manual) toast("Still verifying. We’ll update this page as soon as it’s approved.");
      } catch (_) { /* keep the last state */ }
    }
    function startPolling() {
      clearInterval(payTimer);
      if (pay.status !== "submitted") return;
      payTimer = setInterval(() => { if (!location.hash.startsWith("#/pay/")) { clearInterval(payTimer); return; } poll(false); }, 8000);
    }
    paint(); startPolling();
  });

  // The short administrator page: payments waiting for approval, the sheet, nothing else.
  K.route("payments", async () => {
    K.setNav("payments"); skeletonPage();
    K.setCrumbs([{ label: "Payments" }]);
    let filter = "submitted", data;
    const body = el("div");
    const dl = K.btn("Download Excel sheet", { cls: "ghost", icon: "billing", href: "/api/admin/upi/export.csv" });
    setView(view(K.pageHead("Payments", "Check each payment in your bank app, then approve it. Every payment is also saved to the Excel sheet.", [dl]), body));
    K.setNav("payments");
    async function load() {
      try { data = await Api.get("/api/admin/upi?status=" + filter); } catch (e) { return showError(e); }
      paint();
    }
    function approve(p) {
      const b = el("div");
      b.innerHTML = "<p class='muted'>Open your bank or UPI app and confirm that this payment really arrived:</p><div class='link-change'><div><span class='muted small'>Amount</span><code>" + esc(money(p.amount)) + "</code></div><div><span class='muted small'>UPI transaction ID</span><code>" + esc(p.utr) + "</code></div><div><span class='muted small'>Paid by</span><code>" + esc(p.user_name) + " · " + esc(p.user_email) + "</code></div></div><p class='muted small' style='margin-top:12px'>Approving switches on " + esc(p.plan_name) + " for this person straight away.</p>";
      confirmModal({ title: "Approve this payment?", body: b, confirmLabel: "Yes, I received it. Approve", onConfirm: async () => { await Api.post("/api/admin/upi/" + encodeURIComponent(p.ref) + "/approve"); toast("Approved. " + p.user_name + " now has paid hosting."); await load(); } });
    }
    function reject(p) {
      const b = el("div"); b.innerHTML = "<p class='muted'>Tell " + esc(p.user_name) + " what to fix. They can enter the transaction ID again.</p>";
      const f = el("div", "field"); f.innerHTML = "<label>Reason</label>"; const i = el("input", "input"); i.placeholder = "e.g. Amount not received. Check the transaction ID"; i.maxLength = 200; f.append(i); b.append(f);
      confirmModal({ title: "Reject this payment?", body: b, confirmLabel: "Reject", danger: true, onConfirm: async () => { await Api.post("/api/admin/upi/" + encodeURIComponent(p.ref) + "/reject", { note: i.value }); toast("Rejected. The person has been told."); await load(); } });
    }
    function paint() {
      clear(body);
      const c = data.counts || {};
      const tabs = el("div", "tabs");
      [["submitted", "To verify", c.submitted || 0], ["approved", "Approved", c.approved || 0], ["rejected", "Rejected", c.rejected || 0], ["all", "All", null]].forEach(([id, label, n]) => {
        const t = el("a", filter === id ? "on" : "", esc(label) + (n === null ? "" : " <span class='count" + (id === "submitted" && n ? " hot" : "") + "'>" + n + "</span>"));
        t.href = "#/payments"; t.onclick = (e) => { e.preventDefault(); filter = id; load(); };
        tabs.append(t);
      });
      body.append(tabs);
      const card = el("div", "card");
      if (!data.payments.length) { card.append(emptyState("check", filter === "submitted" ? "Nothing to verify" : "No payments here", filter === "submitted" ? "New payments appear here as soon as someone submits a transaction ID." : "Payments will be listed here.")); body.append(card); return; }
      const table = el("table", "table");
      table.innerHTML = "<thead><tr><th>Submitted</th><th>Person</th><th>Plan</th><th>Amount</th><th>Transaction ID</th><th>Status</th><th></th></tr></thead>";
      const tb = el("tbody");
      data.payments.forEach((p) => {
        const tr = el("tr");
        tr.innerHTML = "<td>" + esc(dateTime(p.submitted_at || p.created_at)) + "</td><td><b>" + esc(p.user_name) + "</b><div class='muted small'>" + esc(p.user_email) + "</div></td><td>" + esc(p.plan_name) + "</td><td>" + esc(money(p.amount)) + "</td><td class='mono'>" + esc(p.utr || "—") + "</td><td><span class='badge " + (upiTone[p.status] || "") + "'>" + esc(p.status_label) + "</span>" + (p.note ? "<div class='muted small'>" + esc(p.note) + "</div>" : "") + "</td><td style='text-align:right;white-space:nowrap'></td>";
        const last = tr.lastChild;
        if (p.status === "submitted") { last.append(K.btn("Approve", { cls: "accent sm", icon: "check", onClick: () => approve(p) }), document.createTextNode(" "), K.btn("Reject", { cls: "sm", onClick: () => reject(p) })); }
        tb.append(tr);
      });
      table.append(tb); card.append(table); body.append(card);
    }
    await load();
  });


  function loadCheckoutJs() {
    if (window.Razorpay) return Promise.resolve();
    return new Promise((ok, fail) => {
      const s = document.createElement("script");
      s.src = "https://checkout.razorpay.com/v1/checkout.js";
      s.onload = ok; s.onerror = () => fail(new Error("Could not load the payment window. Check your connection."));
      document.head.append(s);
    });
  }
  async function startCheckout(plan, btn) {
    btn.disabled = true;
    try {
      await loadCheckoutJs();
      const o = await Api.post("/api/billing/orders", { plan: plan.code });
      const rzp = new window.Razorpay({
        key: o.key_id, order_id: o.order_id, amount: o.amount, currency: o.currency,
        name: o.name, description: o.description, prefill: o.prefill,
        handler: async (resp) => {
          try {
            const r = await Api.post("/api/billing/verify", resp);
            if (r.outcome === "paid") toast("Payment confirmed. Your plan is active.");
            else if (r.outcome === "pending") toast("Payment received. Confirmation is pending; this page will update shortly.");
            else toast("The payment could not be confirmed.", "error");
          } catch (err) { toast(err.message, "error"); }
          K.render();
        },
        modal: { ondismiss: () => { btn.disabled = false; } },
      });
      rzp.on("payment.failed", (r) => toast((r && r.error && r.error.description) || "Payment failed.", "error"));
      rzp.open();
    } catch (err) { toast(err.message, "error"); btn.disabled = false; }
  }

  // ---------------------------------------------------------------- PLATFORM (admin)
  K.route("platform", async (_a, params) => {
    K.setNav("platform"); skeletonPage();
    const d = await Api.get("/api/admin/overview");
    K.setNav("platform");
    K.setCrumbs([{ label: "Platform" }]);
    const tab = ["deployments", "queue", "settings", "projects"].includes(params.t) ? params.t : "overview";
    const head = K.pageHead("Platform", "Everything running on this Codeskys instance.");
    const tabs = el("div", "tabs");
    [["overview", "Overview"], ["deployments", "Deployments"], ["projects", "Projects"],
     ["queue", "Queue"], ["settings", "Settings"]].forEach(([k, label]) => {
      const a = el("a", k === tab ? "on" : "", label);
      a.href = "#/platform" + (k === "overview" ? "" : "?t=" + k);
      tabs.append(a);
    });
    const body = el("div");
    setView(viewWide(head, tabs, body));
    if (tab === "overview") platformOverview(body, d);
    else if (tab === "deployments") await platformDeployments(body);
    else if (tab === "projects") await platformProjects(body);
    else if (tab === "queue") await platformQueue(body);
    else platformSettings(body, d);
  });

  function platformOverview(body, d) {
    const s = d.stats;
    const stats = el("div", "stats");
    stats.append(stat("team", "Students", String(s.students), s.admins + " admin(s)"),
      stat("projects", "Projects", String(s.projects), s.live + " live"),
      stat("deployments", "Deploys · 30d", String(s.deploys_30d), s.in_flight + " in flight"),
      stat("alert", "Failed · 30d", String(s.failed_30d), s.expired_30d + " expired"));
    body.append(stats);

    const engineCard = el("div", "card card-pad");
    const real = d.provider === "coolify";
    engineCard.innerHTML = "<div class='row'>" + svg(real ? "shield" : "bolt") +
      "<div style='flex:1'><b>Deployment engine: " + (real ? "Coolify" : "Simulator") + "</b>" +
      "<div class='muted small'>" + (real ? "Builds run on your Coolify server." :
        "Builds are simulated — nothing is actually built or hosted. Connect Coolify in Integrations for real deployments.") +
      "</div></div><a class='link' href='#/integrations'>Configure</a></div>";
    const queueRow = el("div", "row"); queueRow.style.marginTop = "14px"; queueRow.style.gap = "8px";
    ["pending", "running", "done", "failed"].forEach((k) => {
      const n = d.queue[k] || 0;
      queueRow.append(el("span", "badge " + (k === "failed" && n ? "bad" : k === "running" && n ? "warn" : ""),
        cap(k) + ": " + n));
    });
    engineCard.append(queueRow);
    body.append(engineCard);

    const grid = el("div", "layout-2"); grid.style.marginTop = "16px";
    const left = el("div", "stack");
    const fails = el("div", "card");
    fails.append(el("div", "card-head", "<div><h3>Recent failures</h3></div><div class='actions'></div>"));
    $(".actions", fails.querySelector(".card-head")).append(K.btn("View all", { cls: "ghost sm", href: "#/platform?t=deployments" }));
    if (!d.failures.length) fails.append(el("div", "card-pad muted", "No failed deployments. "));
    else {
      const ul = el("ul", "feed");
      d.failures.forEach((f) => {
        const li = el("li");
        li.innerHTML = "<span class='ico bad'>" + svg("alert", "sm") + "</span><div class='txt'><b>" +
          esc(f.project) + "</b> <span class='muted small'>#" + f.number + " · " + esc(f.owner || "") +
          "</span><div class='muted small ellipsis'>" + esc(f.error || "") + "</div></div><span class='when'>" +
          timeAgo(f.created_at) + "</span>";
        li.style.cursor = "pointer";
        li.onclick = () => go("#/projects/" + f.slug + "/deployments/" + f.id);
        ul.append(li);
      });
      fails.append(ul);
    }
    left.append(fails);

    const usage = el("div", "card");
    usage.append(el("div", "card-head", "<div><h3>Usage by student</h3></div>"));
    const ub = el("div", "card-pad");
    if (!d.usage.length) ub.innerHTML = "<p class='muted'>No activity yet.</p>";
    else {
      const max = Math.max(1, ...d.usage.map((u) => u.deploys));
      d.usage.forEach((u) => {
        const h = el("div", "hbar");
        h.innerHTML = "<span class='ellipsis' title='" + esc(u.email) + "'>" + esc(u.name) + "</span>" +
          "<div class='track'><div class='fill' style='width:" + Math.round((u.deploys / max) * 100) + "%'></div></div>" +
          "<span class='num'>" + u.deploys + "</span>";
        ub.append(h);
      });
    }
    usage.append(ub); left.append(usage);

    const right = el("div", "stack");
    const exp = el("div", "card");
    exp.append(el("div", "card-head", "<div><h3>Expiring soonest</h3><p>Automatic teardown</p></div>"));
    if (!d.expiring.length) exp.append(el("div", "card-pad muted", "Nothing live right now."));
    else {
      const ul = el("ul", "feed");
      d.expiring.forEach((x) => {
        const li = el("li");
        li.innerHTML = "<span class='ico accent'>" + svg("clock", "sm") + "</span><div class='txt'><b>" +
          esc(x.project) + "</b> <span class='muted small'>#" + x.number + "</span><div class='muted small'>" +
          esc(expiryLabel(x.expires_at) || "—") + "</div></div>";
        li.style.cursor = "pointer";
        li.onclick = () => go("#/projects/" + x.slug + "/deployments/" + x.id);
        ul.append(li);
      });
      exp.append(ul);
    }
    right.append(exp);
    const bm = el("div", "card card-pad");
    bm.innerHTML = "<h3 style='margin:0 0 10px'>Build minutes · 30d</h3><div class='stat'><div class='value'>" +
      s.build_minutes_30d + "</div><div class='foot'>across all students</div></div>";
    right.append(bm);
    grid.append(left, right);
    body.append(grid);
  }

  async function platformDeployments(body, status) {
    const seg = el("div", "seg");
    const current = status || "";
    [["", "All"], ["building", "Building"], ["ready", "Live"], ["failed", "Failed"], ["expired", "Expired"]]
      .forEach(([k, label]) => {
        const b = el("button", k === current ? "on" : "", label);
        b.onclick = async () => { clear(body); await platformDeployments(body, k); };
        seg.append(b);
      });
    const bar = el("div", "toolbar"); bar.append(seg);
    const card = el("div", "card");
    body.append(bar, card);
    const d = await Api.get("/api/admin/deployments" + (current ? "?status=" + current : ""));
    if (!d.deployments.length) { card.append(el("div", "card-pad muted", "No deployments.")); return; }
    const table = el("table", "table");
    table.innerHTML = "<thead><tr><th>Project</th><th>Owner</th><th>Status</th><th>Engine</th><th>Lifetime</th><th></th></tr></thead>";
    const tb = el("tbody");
    d.deployments.forEach((x) => {
      const tr = el("tr");
      tr.innerHTML = "<td><b>" + esc(x.project) + "</b> <span class='muted small'>#" + x.number + "</span>" +
        "<div class='muted small'>" + esc(x.branch) + " · " + timeAgo(x.created_at) + "</div></td>" +
        "<td class='muted small'>" + esc(x.owner || "—") + "</td>" +
        "<td>" + statusChip(x.status) + "</td>" +
        "<td>" + (x.is_simulated ? "<span class='badge warn'>Simulated</span>" : "<span class='badge live'>Coolify</span>") + "</td>" +
        "<td class='muted small'>" + esc(x.status === "expired" ? "expired " + timeAgo(x.expired_at) :
          (x.expires_at ? expiryLabel(x.expires_at) : "—")) + "</td>";
      const act = el("td"); act.style.textAlign = "right";
      act.append(K.btn("Open", { cls: "ghost sm", onClick: () => go("#/projects/" + x.slug + "/deployments/" + x.id) }));
      if (x.status === "ready" || x.status === "building" || x.status === "queued") {
        act.append(K.btn(x.status === "ready" ? "Expire" : "Stop", {
          cls: "danger-ghost sm", onClick: () => expireDeployment(x) }));
      }
      tr.append(act); tb.append(tr);
    });
    table.append(tb);
    const tw = el("div", "table-wrap"); tw.append(table); card.append(tw);
  }

  function expireDeployment(x) {
    confirmModal({
      title: (x.status === "ready" ? "Expire " : "Stop ") + x.project + " #" + x.number + "?",
      desc: x.status === "ready"
        ? "The deployment is torn down now and its Coolify application is stopped and deleted. Codeskys only removes applications it created itself."
        : "The running build is cancelled.",
      confirmLabel: x.status === "ready" ? "Expire now" : "Stop build", danger: true,
      onConfirm: async () => {
        const r = await Api.post("/api/admin/deployments/" + x.id + "/expire");
        toast(r.action === "expired" ? (r.note || "Deployment expired") : "Build cancelled");
        K.render();
      },
    });
  }

  async function platformProjects(body) {
    const card = el("div", "card");
    body.append(card);
    const d = await Api.get("/api/admin/projects");
    if (!d.projects.length) { card.append(el("div", "card-pad muted", "No projects yet.")); return; }
    const table = el("table", "table");
    table.innerHTML = "<thead><tr><th>Project</th><th>Owner</th><th>Repository</th><th>Limits</th><th>TTL</th><th>Deploys</th></tr></thead>";
    const tb = el("tbody");
    d.projects.forEach((p) => {
      const tr = el("tr", "click");
      tr.innerHTML = "<td><div class='row'>" + fwLogo(p.framework) + "<div><b>" + esc(p.name) + "</b>" +
        (p.live ? " <span class='badge live'>Live</span>" : "") + (p.is_paused ? " <span class='badge warn'>Paused</span>" : "") +
        "</div></div></td><td class='muted small'>" + esc(p.owner || "—") + "</td>" +
        "<td class='muted small'>" + esc(p.repo_full_name) + (p.is_private ? " <span class='badge'>Private</span>" : "") + "</td>" +
        "<td class='muted small mono'>" + esc(p.limits_memory) + " / " + esc(p.limits_cpus) + " CPU</td>" +
        "<td class='muted small'>" + p.ttl_hours + "h</td><td>" + p.deploys + "</td>";
      tr.onclick = () => go("#/projects/" + p.slug);
      tb.append(tr);
    });
    table.append(tb);
    const tw = el("div", "table-wrap"); tw.append(table); card.append(tw);
  }

  async function platformQueue(body) {
    const card = el("div", "card");
    card.append(el("div", "card-head", "<div><h3>Job queue</h3><p>Deployments and scheduled teardowns are durable jobs — they survive a restart.</p></div>"));
    body.append(card);
    const d = await Api.get("/api/admin/queue");
    if (!d.jobs.length) { card.append(el("div", "card-pad muted", "The queue is empty.")); return; }
    const table = el("table", "table");
    table.innerHTML = "<thead><tr><th>#</th><th>Kind</th><th>State</th><th>Attempts</th><th>Worker</th><th>Error</th><th></th></tr></thead>";
    const tb = el("tbody");
    d.jobs.forEach((j) => {
      const tr = el("tr");
      const cls = j.state === "failed" ? "bad" : j.state === "running" ? "warn" : j.state === "done" ? "live" : "";
      tr.innerHTML = "<td class='mono'>" + j.id + "</td><td>" + esc(j.kind) + "</td>" +
        "<td><span class='badge " + cls + "'>" + esc(j.state) + "</span></td>" +
        "<td>" + j.attempts + " / " + j.max_attempts + "</td>" +
        "<td class='muted small ellipsis' style='max-width:160px'>" + esc(j.locked_by || "—") + "</td>" +
        "<td class='muted small ellipsis' style='max-width:260px'>" + esc(j.last_error || "") + "</td>";
      const act = el("td"); act.style.textAlign = "right";
      if (j.state === "failed") act.append(K.btn("Retry", { cls: "sm", icon: "refresh", onClick: async () => {
        try { await Api.post("/api/admin/queue/" + j.id + "/retry"); toast("Job queued again"); K.render(); }
        catch (e) { toast(e.message, "error"); } } }));
      tr.append(act); tb.append(tr);
    });
    table.append(tb);
    const tw = el("div", "table-wrap"); tw.append(table); card.append(tw);
  }

  function platformSettings(body, d) {
    const card = settingCard("Deployment defaults", "Applied to newly created projects.");
    const ttl = el("div", "field");
    ttl.innerHTML = "<label>Deployment lifetime (hours)</label><div class='hint'>Every deployment is automatically stopped and deleted once it reaches this age.</div>";
    const ttlI = el("input", "input"); ttlI.type = "number"; ttlI.min = "1"; ttlI.max = "720";
    ttlI.value = d.settings.default_ttl_hours; ttl.append(ttlI);
    card.body.append(ttl);
    card.save(async () => { await Api.post("/api/admin/settings", { default_ttl_hours: ttlI.value }); });

    const limits = el("div", "card setting");
    limits.append(el("div", "card-head", "<div><h3>Build isolation</h3><p>Applied to every Coolify application Codeskys creates.</p></div>"));
    const lb = el("div", "card-pad");
    lb.innerHTML = "<div class='grid-2'>" +
      "<div class='field'><label>Memory limit</label><input class='input mono' value='" + esc(d.settings.build_memory) + "' disabled></div>" +
      "<div class='field'><label>CPU limit</label><input class='input mono' value='" + esc(d.settings.build_cpus) + "' disabled></div></div>";
    const co = el("div", "callout info");
    co.innerHTML = svg("shield") + "<div>Set with <b>KOVA_BUILD_MEMORY</b> and <b>KOVA_BUILD_CPUS</b> in the environment. Student code is never executed by the Codeskys process — it only ever runs inside Coolify's containers on your server.</div>";
    lb.append(co);
    const worker = el("div", "callout " + (d.settings.inline_worker ? "warn" : "live"));
    worker.innerHTML = svg("info") + "<div>" + (d.settings.inline_worker
      ? "The queue worker runs <b>inside the web process</b>. Fine for one machine; for production set <b>KOVA_INLINE_WORKER=0</b> and run <b>python manage.py worker</b> separately."
      : "The queue worker runs as a <b>separate process</b>.") + "</div>";
    lb.append(worker);
    limits.append(lb);
    const stack = el("div", "stack");
    stack.append(card.card, limits);
    body.append(stack);
  }

  // ---------------------------------------------------------------- PLATFORM FEATURE CENTRE
  K.route("platform-suite", async () => {
    K.setNav("platform-suite");
    K.setCrumbs([{ label: "Platform" }, { label: "Features" }]);
    const head = K.pageHead("The Codeskys platform", "One workspace for deploying, managing and understanding your applications.", [
      K.btn("New project", { cls: "accent", icon: "plus", href: "#/new" })
    ]);
    const intro = el("div", "callout info");
    intro.innerHTML = svg("info") + "<div><b>Feature availability is shown honestly.</b> Deployment, previews, analytics and security controls use Codeskys's existing backend. Features that need extra infrastructure are marked as requiring setup; they are not simulated as working services.</div>";
    const items = [
      { id:"agents", icon:"⚙", title:"Agent runners", desc:"Queue, execute and recover deployment build jobs with worker leases, retries and build logs.", state:"Available", cls:"ready", action:"#/deployments", label:"View build activity", detail:"Codeskys's durable job worker runs deployment jobs and retries recoverable failures. It is not a general-purpose AI agent runner." },
      { id:"previews", icon:"◫", title:"Deploy previews", desc:"Create preview deployments alongside production and inspect their build status and logs.", state:"Available", cls:"ready", action:"#/projects", label:"Open projects", detail:"Preview deployments are supported by the existing project deployment workflow. Each preview is a separate deployment target." },
      { id:"ai", icon:"✧", title:"AI gateway", desc:"A central gateway for routing and observing AI model requests from your applications.", state:"Provider required", cls:"setup", action:"", label:"View requirements", detail:"Not implemented yet. A real AI gateway needs provider credentials, secure proxying, usage metering, model routing and rate limits. No AI requests are routed by Codeskys today." },
      { id:"functions", icon:"λ", title:"Functions", desc:"Run backend application code as managed functions with logs and deployment lifecycle.", state:"Runtime required", cls:"setup", action:"", label:"View requirements", detail:"Codeskys can deploy supported full-stack applications through its deployment engine, but it does not currently provide a separate serverless-functions runtime or function invocation API." },
      { id:"database", icon:"▤", title:"Database", desc:"Connect application databases and manage credentials without exposing secrets.", state:"Database provider required", cls:"setup", action:"#/integrations", label:"Open integrations", detail:"Codeskys's own metadata database is SQLite. Per-project managed PostgreSQL or other database provisioning is not implemented. This needs a database provider and isolated credentials." },
      { id:"observability", icon:"⌁", title:"Observability", desc:"Track deployment volume, success rate, build performance, activity and logs.", state:"Available", cls:"ready", action:"#/analytics", label:"Open analytics", detail:"Codeskys provides deployment analytics, build duration metrics, activity history and per-deployment build logs. Application-level traces and live infrastructure metrics are not yet included." },
      { id:"security", icon:"◇", title:"Security", desc:"Account protection, access control, encrypted provider tokens, CSRF and rate limits.", state:"Available", cls:"ready", action:"#/settings?tab=security", label:"Open security settings", detail:"Codeskys includes password hashing, session protection, CSRF checks, role-based authorization, rate limiting and encrypted Git provider tokens. Environment-variable encryption and independent security review remain outstanding." },
      { id:"edge", icon:"◎", title:"Edge network", desc:"Serve applications through a distributed CDN with edge caching and routing.", state:"CDN required", cls:"setup", action:"#/integrations", label:"View integrations", detail:"Codeskys currently deploys through its configured deployment server. It does not operate a global edge network. A CDN or edge provider and DNS configuration are required." }
    ];
    const grid = el("div", "kova-feature-grid");
    items.forEach((it) => {
      const card = el("article", "card kova-feature-card");
      card.innerHTML = "<div class='kova-feature-top'><span class='kova-feature-icon'>" + it.icon + "</span><span class='kova-feature-state " + it.cls + "'>" + esc(it.state) + "</span></div><h3>" + esc(it.title) + "</h3><p>" + esc(it.desc) + "</p>";
      const foot = el("div", "kova-feature-foot");
      const action = el("button", "btn " + (it.cls === "ready" ? "accent" : "ghost"), it.label + " ↗");
      action.onclick = () => {
        if (it.action) { go(it.action); return; }
        const body = el("div", "card-pad");
        body.append(el("p", "", esc(it.detail)));
        body.append(el("div", "callout warn", svg("info") + "<div>This feature is not live yet. Codeskys will not display demo data or claim it is operational.</div>"));
        openModal({ title: it.title, desc: "Current implementation status", body, foot: [K.btn("Close", { cls:"ghost", onClick: closeLayer })] });
      };
      foot.append(action); card.append(foot); grid.append(card);
    });
    const note = el("div", "callout warn");
    note.innerHTML = svg("info") + "<div><b>Live deployment requirement:</b> Real builds and public URLs require a reachable deployment server, a VPS, domain/DNS and provider credentials. Until connected, Codeskys's simulator is clearly labelled and does not publish real websites.</div>";
    setView(view(head, intro, grid, note));
  });

  // ---------------------------------------------------------------- BOOT
  (async function boot() {
    let me;
    try { me = await Api.init(); }
    catch (_) { location.assign("/login"); return; }
    if (!me.user) { location.assign("/login"); return; }
    State.user = me.user;
    initChrome();
    try { State.status = await Api.get("/api/platform/status"); } catch (_) { State.status = {}; }
    buildSidebar();
    // Keep regular users' screens free of internal warnings (see softenWarnings).
    let softenQueued = false;
    new MutationObserver(() => { if (softenQueued) return; softenQueued = true; requestAnimationFrame(() => { softenQueued = false; softenWarnings(document.body); }); })
      .observe(document.body, { childList: true, subtree: true });
    window.addEventListener("hashchange", () => { stopPoll(); K.render(); });
    await K.render();
    refreshNotifBadge();
    setInterval(refreshNotifBadge, 60000);
  })();
})();
