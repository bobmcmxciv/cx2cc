"use strict";

/* ------------------------------------------------------------------ utils */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, kids);
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  setAttrs(el, attrs);
  append(el, kids);
  return el;
}

function setAttrs(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "styleProps") for (const [p, pv] of Object.entries(v)) el.style.setProperty(p, pv);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
}

function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

const state = { me: null, settings: null, renderId: 0 };
let tzOff = 480;

function fmtInt(n) {
  return Math.round(Number(n || 0)).toLocaleString("en-US");
}

function fmtCompact(n) {
  n = Number(n || 0);
  const a = Math.abs(n);
  if (a >= 1e8) return trimZeros((n / 1e8).toFixed(2)) + " 亿";
  if (a >= 1e4) return trimZeros((n / 1e4).toFixed(a >= 1e6 ? 0 : 1)) + " 万";
  return fmtInt(n);
}

function trimZeros(t) {
  return t.includes(".") ? t.replace(/\.?0+$/, "") : t;
}

function fmtPct(a, b) {
  if (!b) return "—";
  return (Number(a || 0) / Number(b) * 100).toFixed(1) + "%";
}

function fmtTime(ms, seconds = true) {
  if (!ms) return "—";
  const iso = new Date(Number(ms) + tzOff * 60000).toISOString();
  return iso.slice(0, 10) + " " + iso.slice(11, seconds ? 19 : 16);
}

function fmtAgo(ms) {
  if (!ms) return "从未";
  const sec = (Date.now() - ms) / 1000;
  if (sec < 60) return "刚刚";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
  return Math.floor(sec / 86400) + " 天前";
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return ms + " ms";
  return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " s";
}

function localDay(ms) {
  return new Date(ms + tzOff * 60000).toISOString().slice(0, 10);
}

function dayRange(days, todayStr) {
  const out = [];
  const end = Date.parse(todayStr + "T00:00:00Z");
  for (let i = days - 1; i >= 0; i--) out.push(new Date(end - i * 86400000).toISOString().slice(0, 10));
  return out;
}

const ROLE_NAMES = { admin: "管理员", operator: "Key 管理员", auditor: "审计员", key: "Key 持有人" };
const STATUS_NAMES = { active: "启用", disabled: "停用", revoked: "已吊销" };
const SCOPE_NAMES = {
  chat: "对话（Messages / Chat / Responses / 搜索）",
  images: "图片生成与编辑",
  usage: "订阅额度查询",
  accounts: "账号池查看（含邮箱）",
};
const SCOPE_SHORT = { chat: "对话", images: "图片", usage: "额度", accounts: "账号池" };
const ROUTE_NAMES = {
  messages: "Messages", chat: "Chat", responses: "Responses", images: "图片", search: "搜索",
  usage: "额度", accounts: "账号池", models: "模型列表", health: "健康检查", other: "其他",
};
const ERROR_NAMES = {
  missing_key: "未携带 Key", invalid_key: "无效 Key", key_disabled: "Key 已停用", key_revoked: "Key 已吊销",
  key_expired: "Key 已过期", key_rotated: "轮换前的旧 Key", scope_denied: "权限范围不足",
  model_denied: "模型不在白名单", rpm_limit: "超出每分钟请求数", concurrency_limit: "超出并发数",
  daily_quota: "超出每日额度", weekly_quota: "超出 7 日额度", client_closed: "客户端中途断开",
  upstream_unreachable: "上游不可达", upstream_stream_error: "上游流中断", body_too_large: "请求体过大",
};
const ACTION_NAMES = {
  "login": "登录", "login.failed": "登录失败", "portal.login": "Key 自助登录",
  "portal.login_failed": "Key 自助登录失败", "key.create": "创建 Key", "key.import": "导入 Key",
  "key.update": "修改 Key", "key.disable": "停用 Key", "key.enable": "启用 Key", "key.revoke": "吊销 Key",
  "key.rotate": "轮换 Key", "user.create": "创建账号", "user.update": "修改账号",
  "user.password_changed": "修改密码", "settings.update": "修改设置", "audit.export": "导出审计记录",
  "usage.import_history": "导入历史用量",
};

function historyNotice(hist) {
  if (!hist || !hist.first_day) return null;
  const until = hist.cutoff_ms ? fmtTime(hist.cutoff_ms, false) : hist.last_day;
  return h("div", { class: "notice" },
    `${hist.first_day} 至 ${until} 的用量是从 cx2cc 与 codex-bridge 日志还原的历史数据，记在「${hist.key}」名下（当时全员共用一个 token，无法按人拆分）。`,
    "历史部分只有按日汇总，没有逐条调用记录；Codex CLI（Responses）与流式 Chat 的历史只有请求次数，不含 token。");
}
const PERM_NAMES = {
  "keys.view_all": "查看全部 Key 及用量", "keys.create": "创建 API Key", "keys.manage_all": "管理全部 Key",
  "keys.manage_own": "管理自己创建的 Key", "keys.grant_accounts": "授予账号池范围",
  "audit.view_all": "查看全部调用审计与操作日志", "users.manage": "管理控制台账号",
  "settings.manage": "修改系统设置", "upstream.view": "查看上游账号池与额度",
};

function can(perm) {
  return !!(state.me && state.me.permissions && state.me.permissions.includes(perm));
}

function errText(code) {
  if (!code) return "";
  if (ERROR_NAMES[code]) return ERROR_NAMES[code];
  const m = /^upstream_(\d{3})$/.exec(code);
  if (m) return "上游返回 " + m[1];
  return code;
}

/* -------------------------------------------------------------------- api */

class ApiFailure extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET" && state.me) opts.headers["X-CSRF-Token"] = state.me.csrf;
  const r = await fetch("api/" + path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (r.status === 401 && !path.startsWith("login")) {
    state.me = null;
    render();
    throw new ApiFailure(401, "登录已过期");
  }
  if (!r.ok) throw new ApiFailure(r.status, (data && data.error) || `HTTP ${r.status}`);
  return data;
}

function toast(text) {
  const t = h("div", { class: "toast", role: "status" }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (_) {
    toast("复制失败，请手动选择复制");
  }
}

/* ----------------------------------------------------------------- shell */

const app = document.getElementById("app");

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  const parts = path.split("/");
  return { name: parts[0] || "", arg: parts[1], params: new URLSearchParams(qs || "") };
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function navItems() {
  if (!state.me) return [];
  if (state.me.kind === "key") {
    return [[`key/${state.me.id}`, "我的用量"], ["requests", "调用记录"], ["events", "操作日志"]];
  }
  const items = [["overview", "概览"], ["keys", "API Key"], ["requests", "调用审计"], ["events", "操作日志"]];
  if (can("users.manage")) items.push(["users", "控制台账号"]);
  if (can("upstream.view")) items.push(["upstream", "上游与模型"]);
  items.push(["settings", "设置"]);
  return items;
}

function renderShell(current) {
  const nav = h("nav", { class: "nav" }, navItems().map(([hash, label]) => {
    const active = hash === current || hash.split("/")[0] === current;
    return h("a", { href: "#/" + hash, class: active ? "active" : null }, label);
  }));
  const me = state.me;
  const who = h("div", { class: "who" },
    h("span", { class: "badge plain" }, ROLE_NAMES[me.role] || me.role),
    h("span", null, me.name),
    me.kind === "user" ? h("button", { class: "link", onclick: openPasswordModal }, "改密码") : null,
    h("button", { class: "link", onclick: logout }, "退出"),
  );
  const top = h("header", { class: "topbar" },
    h("div", { class: "brand" }, "cx2cc", h("span", null, "控制台")), nav, who);
  const main = h("main");
  app.replaceChildren(top, main);
  return main;
}

async function logout() {
  try { await api("POST", "logout", {}); } catch (_) { /* already gone */ }
  state.me = null;
  location.hash = "";
  render();
}

const VIEWS = {};

async function render() {
  hideTooltip();
  document.querySelectorAll(".backdrop").forEach((b) => b.remove());
  const id = ++state.renderId;
  if (!state.me) {
    renderLogin();
    return;
  }
  const route = parseHash();
  if (!route.name || route.name === "key-login") {
    location.replace(state.me.kind === "key" ? `#/key/${state.me.id}` : "#/overview");
    return;
  }
  const main = renderShell(route.name);
  main.append(h("div", { class: "muted" }, "加载中…"));
  const view = VIEWS[route.name];
  const ctx = { alive: () => id === state.renderId };
  try {
    if (!view) throw new Error("页面不存在");
    await view(main, route, ctx);
  } catch (e) {
    if (ctx.alive() && !(e instanceof ApiFailure && e.status === 401)) {
      main.replaceChildren(h("div", { class: "notice err" }, e.message || String(e)));
    }
  }
}

window.addEventListener("hashchange", render);

async function loadSettings() {
  const data = await api("GET", "settings");
  state.settings = data;
  tzOff = data.settings.tz_offset_minutes;
}

async function boot() {
  try {
    const data = await api("GET", "me");
    state.me = data.me;
    if (state.me) await loadSettings();
  } catch (_) {
    state.me = null;
  }
  render();
}

/* ----------------------------------------------------------------- login */

function renderLogin() {
  let mode = "user";
  const error = h("div", { class: "notice err", hidden: true });
  const userForm = h("form", null,
    h("label", { class: "field" }, "用户名", h("input", { name: "username", autocomplete: "username", required: true })),
    h("label", { class: "field" }, "密码",
      h("input", { name: "password", type: "password", autocomplete: "current-password", required: true })),
    h("button", { class: "primary", type: "submit" }, "登录"),
  );
  const keyForm = h("form", { hidden: true },
    h("label", { class: "field" }, "API Key",
      h("input", { name: "key", type: "password", autocomplete: "off", placeholder: "sk-cx2cc-…", required: true })),
    h("p", { class: "hint" }, "只能查看这把 Key 自己的用量与调用记录，不能做任何修改。"),
    h("button", { class: "primary", type: "submit" }, "查看我的用量"),
  );
  const tabs = h("div", { class: "seg", role: "tablist" });
  const tabUser = h("button", { type: "button", class: "on", onclick: () => switchTo("user") }, "控制台账号");
  const tabKey = h("button", { type: "button", onclick: () => switchTo("key") }, "用 API Key 登录");
  tabs.append(tabUser, tabKey);

  function switchTo(m) {
    mode = m;
    tabUser.className = m === "user" ? "on" : "";
    tabKey.className = m === "key" ? "on" : "";
    userForm.hidden = m !== "user";
    keyForm.hidden = m !== "key";
    error.hidden = true;
  }

  async function submit(ev) {
    ev.preventDefault();
    error.hidden = true;
    const form = ev.currentTarget;
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const data = mode === "user"
        ? await api("POST", "login", { username: form.username.value.trim(), password: form.password.value })
        : await api("POST", "login-key", { key: form.key.value.trim() });
      state.me = data.me;
      await loadSettings();
      location.hash = "";
      render();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }
  userForm.addEventListener("submit", submit);
  keyForm.addEventListener("submit", submit);
  const wantKey = parseHash().name === "key-login";

  app.replaceChildren(h("div", { class: "login-wrap" },
    h("div", { class: "card login" },
      h("h1", null, "cx2cc 控制台"),
      h("div", { class: "sub" }, "API Key、用量与审计"),
      tabs, error, userForm, keyForm),
  ));
  if (wantKey) {
    switchTo("key");
    keyForm.key.focus();
  } else {
    userForm.username.focus();
  }
}

/* ---------------------------------------------------------------- charts */

const tooltip = document.getElementById("tooltip");

function showTooltip(x, y, head, rows, foot) {
  tooltip.replaceChildren(h("div", { class: "t-head" }, head), rows.map((r) =>
    h("div", { class: "t-row" },
      r.color ? h("span", { class: "key", styleProps: { background: r.color } }) : null,
      h("span", { class: "v" }, r.value), h("span", { class: "n" }, r.name))),
  foot ? h("div", { class: "t-head" }, foot) : null);
  tooltip.hidden = false;
  const pad = 14;
  const w = tooltip.offsetWidth;
  const hgt = tooltip.offsetHeight;
  let left = x + pad;
  let top = y + pad;
  if (left + w > window.innerWidth - 8) left = x - w - pad;
  if (top + hgt > window.innerHeight - 8) top = y - hgt - pad;
  tooltip.style.left = Math.max(8, left) + "px";
  tooltip.style.top = Math.max(8, top) + "px";
}

function hideTooltip() {
  tooltip.hidden = true;
}

function niceStep(max, count) {
  if (max <= 0) return 1;
  const raw = max / count;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  const step = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
  return step * p;
}

function roundTop(x, y, w, hgt, r) {
  r = Math.max(0, Math.min(r, w / 2, hgt));
  return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
}

/**
 * Column chart, stacked when there is more than one series.
 * labels: x categories; series: [{name, color, values}]; fmt: value formatter.
 */
function columnChart({ labels, series, fmt, height = 220, shortLabel, ariaLabel }) {
  const wrap = h("div", { class: "chart" });
  const draw = () => {
    const W = Math.max(320, wrap.clientWidth || 720);
    const H = height;
    const padL = 56, padR = 8, padT = 10, padB = 26;
    const pw = W - padL - padR;
    const ph = H - padT - padB;
    const n = labels.length;
    const totals = labels.map((_, i) => series.reduce((acc, se) => acc + (Number(se.values[i]) || 0), 0));
    const step = niceStep(Math.max(0, ...totals), 4);
    const max = Math.max(step, Math.ceil(Math.max(0, ...totals) / step) * step);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": ariaLabel || "" });
    for (let v = 0; v <= max + step / 2; v += step) {
      const y = padT + ph - (v / max) * ph;
      svg.append(s("line", { x1: padL, x2: W - padR, y1: y, y2: y, class: v === 0 ? "base-line" : "grid-line" }));
      svg.append(s("text", { x: padL - 8, y: y + 4, "text-anchor": "end" }, fmt(v)));
    }
    const band = pw / Math.max(1, n);
    const bw = Math.min(24, Math.max(2, band * 0.64));
    const cols = [];
    for (let i = 0; i < n; i++) {
      const x = padL + band * i + (band - bw) / 2;
      let y0 = padT + ph;
      const g = s("g", { class: "col" });
      const segs = series.map((se) => ({ se, v: Number(se.values[i]) || 0 })).filter((p) => p.v > 0);
      segs.forEach((p, idx) => {
        const hgt = (p.v / max) * ph;
        const gap = idx > 0 ? 2 : 0;
        const top = y0 - hgt;
        const drawH = Math.max(0, hgt - gap);
        const isTop = idx === segs.length - 1;
        const el = isTop
          ? s("path", { d: roundTop(x, top, bw, drawH, 4) })
          : s("rect", { x, y: top, width: bw, height: drawH });
        el.style.fill = p.se.color;
        g.append(el);
        y0 = top;
      });
      svg.append(g);
      cols.push(g);
      const hit = s("rect", {
        x: padL + band * i, y: padT, width: band, height: ph, class: "hit", tabindex: 0,
        "aria-label": `${labels[i]}：${fmt(totals[i])}`,
      });
      const show = (cx, cy) => {
        cols.forEach((c, j) => c.classList.toggle("dim", j !== i));
        const rows = series.slice().reverse().map((se) => ({
          color: se.color, value: fmt(Number(se.values[i]) || 0), name: se.name,
        }));
        showTooltip(cx, cy, labels[i], series.length > 1 ? rows : [{ value: fmt(totals[i]), name: series[0].name }],
          series.length > 1 ? `合计 ${fmt(totals[i])}` : null);
      };
      const clear = () => { cols.forEach((c) => c.classList.remove("dim")); hideTooltip(); };
      hit.addEventListener("pointermove", (ev) => show(ev.clientX, ev.clientY));
      hit.addEventListener("pointerleave", clear);
      hit.addEventListener("focus", () => {
        const r = hit.getBoundingClientRect();
        show(r.left + r.width / 2, r.top + 20);
      });
      hit.addEventListener("blur", clear);
      svg.append(hit);
    }
    const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 62))));
    for (let i = 0; i < n; i += every) {
      svg.append(s("text", { x: padL + band * i + band / 2, y: H - 8, "text-anchor": "middle" },
        shortLabel ? shortLabel(labels[i]) : labels[i]));
    }
    wrap.replaceChildren(svg);
  };
  requestAnimationFrame(draw);
  let last = 0;
  new ResizeObserver(() => {
    const w = wrap.clientWidth;
    if (Math.abs(w - last) > 4) { last = w; draw(); }
  }).observe(wrap);
  return wrap;
}

function legend(series) {
  return h("div", { class: "legend" }, series.map((se) =>
    h("span", null, h("span", { class: "sw", styleProps: { background: se.color } }), se.name)));
}

function chartTable(labels, series, fmt) {
  return h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, h("th", null, "日期 / 时间"), series.map((se) => h("th", { class: "num" }, se.name)),
      series.length > 1 ? h("th", { class: "num" }, "合计") : null)),
    h("tbody", null, labels.map((l, i) => h("tr", null, h("td", { class: "nowrap" }, l),
      series.map((se) => h("td", { class: "num" }, fmt(Number(se.values[i]) || 0))),
      series.length > 1
        ? h("td", { class: "num" }, fmt(series.reduce((a, se) => a + (Number(se.values[i]) || 0), 0)))
        : null)))));
}

function chartCard(title, sub, chartSpec) {
  const chart = columnChart(chartSpec);
  const table = chartTable(chartSpec.labels, chartSpec.series, chartSpec.fmt);
  table.hidden = true;
  const toggle = h("button", {
    onclick: () => {
      table.hidden = !table.hidden;
      chart.hidden = !table.hidden;
      toggle.textContent = table.hidden ? "表格" : "图表";
    },
  }, "表格");
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", null, h("h2", null, title), sub ? h("div", { class: "muted small" }, sub) : null), toggle),
    chart, chartSpec.series.length > 1 ? legend(chartSpec.series) : null, table);
}

const SERIES_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"];

/* ----------------------------------------------------------- small parts */

function tile(label, value, foot, hero) {
  return h("div", { class: "tile" + (hero ? " hero" : "") },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    foot ? h("div", { class: "foot" }, foot) : null);
}

function statusBadge(status) {
  return h("span", { class: "badge " + status }, STATUS_NAMES[status] || status);
}

function table(columns, rows, opts = {}) {
  const tbody = h("tbody");
  const fill = (items) => {
    for (const row of items) {
      const tr = h("tr", { class: opts.onRow ? "clickable" : null },
        columns.map((c) => h("td", { class: c.cls || null }, c.render(row))));
      if (opts.onRow) tr.addEventListener("click", (ev) => {
        if (ev.target.closest("a,button")) return;
        opts.onRow(row);
      });
      tbody.append(tr);
    }
  };
  if (!rows.length) {
    tbody.append(h("tr", null, h("td", { class: "empty", colspan: columns.length }, opts.empty || "暂无数据")));
  } else {
    fill(rows);
  }
  const el = h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, columns.map((c) => h("th", { class: c.cls || null }, c.title)))), tbody));
  el.appendRows = (items) => {
    const empty = tbody.querySelector("td.empty");
    if (empty) empty.parentElement.remove();
    fill(items);
  };
  return el;
}

function modelName(m) {
  return !m || m === "-" ? h("span", { class: "muted" }, "（未到模型）") : h("span", { class: "mono" }, m);
}

function keyLink(id, alias) {
  if (id === null || id === undefined) return h("span", { class: "muted" }, alias || "（无 Key）");
  return h("a", { href: `#/key/${id}` }, alias || `#${id}`);
}

function limitsText(k) {
  const parts = [];
  if (k.rpm_limit) parts.push(`${k.rpm_limit}/分钟`);
  if (k.concurrency_limit) parts.push(`并发 ${k.concurrency_limit}`);
  if (k.daily_limit) parts.push(`日 ${fmtCompact(k.daily_limit)}`);
  if (k.weekly_limit) parts.push(`7 日 ${fmtCompact(k.weekly_limit)}`);
  return parts.length ? parts.join(" · ") : "不限";
}

/* ------------------------------------------------------------- overview */

VIEWS.overview = async (main, route, ctx) => {
  const days = [7, 30, 90].includes(Number(route.params.get("days"))) ? Number(route.params.get("days")) : 30;
  const d = await api("GET", `overview?days=${days}`);
  if (!ctx.alive()) return;
  const t = d.totals;
  const kc = d.key_counts || {};

  const head = h("div", { class: "page-head" },
    h("div", null, h("h1", null, "概览"),
      h("div", { class: "sub" }, can("keys.view_all") ? "全部 API Key 的用量" : "你创建的 API Key 的用量")),
    h("div", { class: "seg" }, [7, 30, 90].map((n) =>
      h("button", { class: n === days ? "on" : null, onclick: () => go(`#/overview?days=${n}`) }, `近 ${n} 天`))));

  const tiles = h("div", { class: "tiles" },
    tile("今日加权用量", fmtCompact(t.today.weighted), `${fmtInt(t.today.requests)} 次请求`, true),
    tile("近 7 日加权用量", fmtCompact(t["7d"].weighted), `${fmtInt(t["7d"].requests)} 次请求`),
    tile("近 30 日加权用量", fmtCompact(t["30d"].weighted), `${fmtInt(t["30d"].requests)} 次请求`),
    tile("缓存命中率（7 日）", fmtPct(t["7d"].cached_tokens, t["7d"].input_tokens),
      `${fmtCompact(t["7d"].cached_tokens)} / ${fmtCompact(t["7d"].input_tokens)} 输入`),
    tile("错误率（7 日）", fmtPct(t["7d"].errors, t["7d"].requests), `${fmtInt(t["7d"].errors)} 次失败`),
    tile("24 小时内活跃 Key", fmtInt(d.active_24h),
      `启用 ${kc.active || 0} · 停用 ${kc.disabled || 0} · 吊销 ${kc.revoked || 0}`),
  );

  // Stacked columns: the five heaviest keys of the last 30 days keep their
  // colours whatever range is selected; the rest fold into 其他.
  const labels = dayRange(days, d.today);
  const top = d.top_keys_30d.slice(0, 5);
  const alias = {};
  d.per_day_key.forEach((r) => { alias[r.key_id] = r.alias || `#${r.key_id}`; });
  d.by_key.forEach((r) => { alias[r.key_id] = r.alias || `#${r.key_id}`; });
  const idx = Object.fromEntries(labels.map((l, i) => [l, i]));
  const series = top.map((kid, i) => ({ id: kid, name: alias[kid] || `#${kid}`, color: SERIES_COLORS[i], values: labels.map(() => 0) }));
  const other = { id: "other", name: "其他", color: "var(--s-other)", values: labels.map(() => 0) };
  for (const r of d.per_day_key) {
    const i = idx[r.day];
    if (i === undefined) continue;
    const se = series.find((x) => x.id === r.key_id) || other;
    se.values[i] += Number(r.weighted) || 0;
  }
  const plotted = series.concat(other.values.some((v) => v > 0) ? [other] : []);
  const chart = plotted.length
    ? chartCard("每日加权用量（按 Key）", "加权 = 未缓存输入 × " + state.settings.settings.weight_uncached +
        " + 缓存输入 × " + state.settings.settings.weight_cached + " + 输出 × " + state.settings.settings.weight_output,
      { labels, series: plotted, fmt: fmtCompact, shortLabel: (l) => l.slice(5), ariaLabel: "每日加权用量" })
    : h("div", { class: "card" }, h("h2", null, "每日加权用量"), h("p", { class: "muted" }, "所选范围内还没有调用记录。"));

  const byKey = h("div", { class: "card" }, h("h2", null, "近 7 天按 Key"), table([
    { title: "别名", render: (r) => keyLink(r.key_id, r.alias) },
    { title: "使用人", render: (r) => r.owner || "—" },
    { title: "状态", render: (r) => (r.status ? statusBadge(r.status) : "—") },
    { title: "请求", cls: "num", render: (r) => fmtInt(r.requests) },
    { title: "输入", cls: "num", render: (r) => fmtCompact(r.input_tokens) },
    { title: "缓存命中", cls: "num", render: (r) => fmtPct(r.cached_tokens, r.input_tokens) },
    { title: "输出", cls: "num", render: (r) => fmtCompact(r.output_tokens) },
    { title: "加权", cls: "num", render: (r) => fmtCompact(r.weighted) },
    { title: "失败", cls: "num", render: (r) => fmtInt(r.errors) },
    { title: "最后使用", cls: "nowrap", render: (r) => fmtAgo(r.last_used_at) },
  ], d.by_key, { onRow: (r) => go(`#/key/${r.key_id}`) }));

  const byOwner = h("div", { class: "card" }, h("h2", null, "近 7 天按使用人"), table([
    { title: "使用人", render: (r) => r.owner },
    { title: "Key 数", cls: "num", render: (r) => fmtInt(r.keys) },
    { title: "请求", cls: "num", render: (r) => fmtInt(r.requests) },
    { title: "加权", cls: "num", render: (r) => fmtCompact(r.weighted) },
  ], d.by_owner));

  const byModel = h("div", { class: "card" }, h("h2", null, "近 7 天按模型"), table([
    { title: "实际模型", render: (r) => modelName(r.model) },
    { title: "请求", cls: "num", render: (r) => fmtInt(r.requests) },
    { title: "加权", cls: "num", render: (r) => fmtCompact(r.weighted) },
  ], d.by_model));

  const errors = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", null, "最近失败的调用"),
      h("a", { href: "#/requests?status=error" }, "全部失败记录 →")),
    table([
      { title: "时间", cls: "nowrap", render: (r) => fmtTime(r.ts) },
      { title: "Key", render: (r) => r.alias || h("span", { class: "muted" }, "（无）") },
      { title: "路由", render: (r) => ROUTE_NAMES[r.route] || r.route },
      { title: "状态", cls: "num", render: (r) => r.status },
      { title: "原因", render: (r) => h("span", { class: "err-text" }, errText(r.error) || "—") },
      { title: "来源 IP", cls: "mono", render: (r) => r.client_ip || "—" },
    ], d.recent_errors, { empty: "近 7 天没有失败的调用" }));

  const upstream = d.upstream_ok;
  const health = upstream && upstream.status === "ok"
    ? h("span", { class: "badge ok" }, "上游 cx2cc 正常")
    : h("span", { class: "badge err" }, upstream ? "上游 cx2cc 不可达" : "上游状态未知");

  main.replaceChildren(head, h("div", { class: "filters" }, health), historyNotice(d.history), tiles, chart,
    byKey, h("div", { class: "grid2 gap-below" }, byOwner, byModel), errors);
};

/* ---------------------------------------------------------------- keys */

VIEWS.keys = async (main, route, ctx) => {
  const q = route.params.get("q") || "";
  const status = route.params.get("status") || "";
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  const d = await api("GET", "keys?" + params);
  if (!ctx.alive()) return;

  const search = h("input", { type: "search", placeholder: "搜索别名、使用人、备注或前缀", value: q, size: 28 });
  const statusSel = h("select", null,
    [["", "全部状态"], ["active", "启用"], ["disabled", "停用"], ["revoked", "已吊销"]]
      .map(([v, l]) => h("option", { value: v, selected: v === status ? true : null }, l)));
  const apply = () => {
    const p = new URLSearchParams();
    if (search.value.trim()) p.set("q", search.value.trim());
    if (statusSel.value) p.set("status", statusSel.value);
    go("#/keys" + (p.toString() ? "?" + p : ""));
  };
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
  statusSel.addEventListener("change", apply);

  const head = h("div", { class: "page-head" },
    h("div", null, h("h1", null, "API Key"),
      h("div", { class: "sub" }, "每人（或每台机器）一把 Key，按别名区分；用量、限额与审计都按 Key 记录。")),
    can("keys.create") ? h("button", { class: "primary", onclick: () => openKeyModal(null) }, "新建 API Key") : null);

  const list = table([
    { title: "别名", render: (k) => h("div", null, keyLink(k.id, k.alias), k.note ? h("div", { class: "muted small" }, k.note) : null) },
    { title: "使用人", render: (k) => k.owner || "—" },
    { title: "Key 前缀", cls: "mono nowrap", render: (k) => k.key_prefix + "…" },
    { title: "权限范围", render: (k) => k.scopes.map((sc) => h("span", { class: "tag" }, SCOPE_SHORT[sc] || sc)) },
    { title: "限额", render: (k) => h("span", { class: "small" }, limitsText(k)) },
    { title: "状态", render: (k) => h("div", null, statusBadge(k.status),
      k.expires_at ? h("div", { class: "muted small" }, (k.expires_at < Date.now() ? "已过期 " : "到期 ") + fmtTime(k.expires_at, false)) : null) },
    { title: "今日加权", cls: "num", render: (k) => fmtCompact(k.usage.weighted_today) },
    { title: "7 日加权", cls: "num", render: (k) => fmtCompact(k.usage.weighted_7d) },
    { title: "7 日请求", cls: "num", render: (k) => fmtInt(k.usage.requests_7d) },
    { title: "最后使用", cls: "nowrap", render: (k) => h("div", null, fmtAgo(k.last_used_at),
      k.last_used_ip ? h("div", { class: "muted small mono" }, k.last_used_ip) : null) },
  ], d.keys, { onRow: (k) => go(`#/key/${k.id}`), empty: q || status ? "没有符合条件的 Key" : "还没有 API Key" });

  main.replaceChildren(head, h("div", { class: "filters" }, search, statusSel, h("button", { onclick: apply }, "筛选")),
    h("div", { class: "card" }, list));
};

/* ------------------------------------------------------------ key detail */

VIEWS.key = async (main, route, ctx) => {
  const id = Number(route.arg);
  const days = Number(route.params.get("days")) === 90 ? 90 : 30;
  const d = await api("GET", `keys/${id}?days=${days}`);
  if (!ctx.alive()) return;
  const k = d.key;
  const u = k.usage || {};

  const actions = [];
  if (k.can_manage && k.status !== "revoked") {
    actions.push(h("button", { onclick: () => openKeyModal(k) }, "编辑"));
    if (k.status === "active") actions.push(h("button", { onclick: () => keyAction(k, "disable") }, "停用"));
    if (k.status === "disabled") actions.push(h("button", { onclick: () => keyAction(k, "enable") }, "启用"));
    actions.push(h("button", { onclick: () => openRotateModal(k) }, "轮换"));
    actions.push(h("button", { class: "danger", onclick: () => openRevokeModal(k) }, "吊销"));
  }

  const head = h("div", { class: "page-head" },
    h("div", null,
      h("h1", null, k.alias, " ", statusBadge(k.status)),
      h("div", { class: "sub" }, (k.owner ? `使用人 ${k.owner} · ` : "") + `前缀 ${k.key_prefix}…`)),
    h("div", { class: "head-actions" },
      h("div", { class: "seg" }, [30, 90].map((n) =>
        h("button", { class: n === days ? "on" : null, onclick: () => go(`#/key/${id}?days=${n}`) }, `近 ${n} 天`))),
      actions));

  const notices = [];
  if (k.rotation_grace_until) {
    notices.push(h("div", { class: "notice warn" }, `这把 Key 刚轮换过，旧值在 ${fmtTime(k.rotation_grace_until)} 之前仍然有效。`));
  }
  if (k.expires_at && k.expires_at < Date.now()) notices.push(h("div", { class: "notice err" }, "这把 Key 已过期，请求会被拒绝。"));
  if (d.history && d.history.key === k.alias) notices.push(historyNotice(d.history));

  const meters = [];
  const meter = (label, used, limit) => {
    const ratio = limit ? Math.min(1, used / limit) : 0;
    const bar = h("div");
    bar.style.width = (ratio * 100).toFixed(1) + "%";
    return h("div", null, h("div", { class: "small ink2" }, `${label}：${fmtCompact(used)} / ${fmtCompact(limit)}（${fmtPct(used, limit)}）`),
      h("div", { class: "meter" + (ratio >= 1 ? " crit" : ratio >= 0.8 ? " warn" : "") }, bar));
  };
  if (k.daily_limit) meters.push(meter("今日额度", u.weighted_today || 0, k.daily_limit));
  if (k.weekly_limit) meters.push(meter("近 7 日额度", u.weighted_7d || 0, k.weekly_limit));

  const tiles = h("div", { class: "tiles" },
    tile("今日加权用量", fmtCompact(u.weighted_today), `${fmtInt(u.requests_today)} 次请求`, true),
    tile("近 7 日加权", fmtCompact(u.weighted_7d), `${fmtInt(u.requests_7d)} 次请求 · 失败 ${fmtInt(u.errors_7d)}`),
    tile("近 30 日加权", fmtCompact(u.weighted_30d), `${fmtInt(u.requests_30d)} 次请求`),
    tile("缓存命中率（30 日）", fmtPct(u.cached_30d, u.input_30d), `输出 ${fmtCompact(u.output_30d)}`),
    tile("首字节耗时（7 日）", fmtMs(d.ttfb_p50), `p95 ${fmtMs(d.ttfb_p95)}`),
    tile("进行中的请求", fmtInt(d.inflight), k.concurrency_limit ? `并发上限 ${k.concurrency_limit}` : "不限并发"),
  );

  const info = h("div", { class: "card" }, h("h2", null, "Key 信息"), h("dl", { class: "kv" },
    h("dt", null, "权限范围"), h("dd", null, k.scopes.map((sc) => h("div", null, SCOPE_NAMES[sc] || sc))),
    h("dt", null, "模型白名单"), h("dd", null, k.allowed_models.length ? k.allowed_models.map((m) => h("span", { class: "tag mono" }, m)) : "不限（含别名解析）"),
    h("dt", null, "限额"), h("dd", null, limitsText(k)),
    h("dt", null, "有效期"), h("dd", null, k.expires_at ? fmtTime(k.expires_at) : "长期"),
    h("dt", null, "备注"), h("dd", null, k.note || "—"),
    h("dt", null, "创建"), h("dd", null, fmtTime(k.created_at) + (k.created_by_name ? ` · ${k.created_by_name}` : "")),
    h("dt", null, "最后使用"), h("dd", null, k.last_used_at ? `${fmtTime(k.last_used_at)} · ${k.last_used_ip || ""}` : "从未"),
    k.revoked_at ? [h("dt", null, "吊销时间"), h("dd", null, fmtTime(k.revoked_at))] : null,
  ), meters.length ? h("div", { class: "stack" }, h("div"), meters) : null);

  const labelsN = dayRange(days, localDay(Date.now()));
  const byDay = Object.fromEntries(d.daily.map((r) => [r.day, r]));
  const dailyChart = chartCard(`每日加权用量（${days} 天）`, null, {
    labels: labelsN,
    series: [{ name: "加权用量", color: "var(--s1)", values: labelsN.map((l) => (byDay[l] ? byDay[l].weighted : 0)) }],
    fmt: fmtCompact, shortLabel: (l) => l.slice(5), ariaLabel: "每日加权用量",
  });

  const hours = [];
  const nowHour = Math.floor((Date.now() + tzOff * 60000) / 3600000) * 3600000 - tzOff * 60000;
  for (let i = 47; i >= 0; i--) hours.push(nowHour - i * 3600000);
  const byHour = Object.fromEntries(d.hourly.map((r) => [r.hour, r]));
  const hourlyChart = chartCard("每小时请求数（48 小时）", null, {
    labels: hours.map((ms) => fmtTime(ms, false)),
    series: [{ name: "请求数", color: "var(--s1)", values: hours.map((ms) => (byHour[ms] ? byHour[ms].requests : 0)) }],
    fmt: fmtInt, shortLabel: (l) => l.slice(11), ariaLabel: "每小时请求数",
  });

  const models = h("div", { class: "card" }, h("h2", null, `模型分布（${days} 天）`), table([
    { title: "实际模型", render: (r) => modelName(r.model) },
    { title: "请求", cls: "num", render: (r) => fmtInt(r.requests) },
    { title: "输入", cls: "num", render: (r) => fmtCompact(r.input_tokens) },
    { title: "缓存命中", cls: "num", render: (r) => fmtPct(r.cached_tokens, r.input_tokens) },
    { title: "输出", cls: "num", render: (r) => fmtCompact(r.output_tokens) },
    { title: "加权", cls: "num", render: (r) => fmtCompact(r.weighted) },
  ], d.models));

  const ips = h("div", { class: "card" }, h("h2", null, "来源 IP（7 天）"), table([
    { title: "IP", cls: "mono", render: (r) => r.client_ip || "—" },
    { title: "请求", cls: "num", render: (r) => fmtInt(r.requests) },
    { title: "最近", cls: "nowrap", render: (r) => fmtAgo(r.last_ts) },
    { title: "客户端", render: (r) => h("span", { class: "small muted" }, r.user_agent || "—") },
  ], d.ips));

  const recent = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", null, "最近调用"), h("a", { href: `#/requests?key_id=${id}` }, "查看全部 →")));
  const events = h("div", { class: "card" }, h("h2", null, "操作记录"));

  main.replaceChildren(head, ...notices, tiles, h("div", { class: "grid2" }, info, models),
    dailyChart, hourlyChart, ips, recent, events);

  const reqs = await api("GET", `requests?key_id=${id}&limit=20`);
  if (!ctx.alive()) return;
  recent.append(requestsTable(reqs.requests, { showKey: false }));
  const evs = await api("GET", `events?target_type=key&target_id=${id}&limit=20`);
  if (!ctx.alive()) return;
  events.append(eventsTable(evs.events));
};

async function keyAction(k, action, body) {
  try {
    const d = await api("POST", `keys/${k.id}/${action}`, body || {});
    toast({ disable: "已停用", enable: "已启用", revoke: "已吊销", rotate: "已轮换" }[action]);
    if (d.key) showSecret(d.record, d.key, "新的 Key（只显示这一次）");
    else render();
  } catch (e) {
    toast(e.message);
  }
}

/* -------------------------------------------------------------- requests */

function requestsTable(rows, { showKey = true } = {}) {
  const cols = [
    { title: "时间", cls: "nowrap", render: (r) => fmtTime(r.ts) },
    showKey ? { title: "Key", render: (r) => (r.key_id ? keyLink(r.key_id, r.alias)
      : h("span", { class: "muted", title: r.key_fingerprint ? "指纹 " + r.key_fingerprint : null }, "（无效 Key）")) } : null,
    { title: "路由", render: (r) => h("span", { title: `${r.method} ${r.path}` }, ROUTE_NAMES[r.route] || r.route, r.stream ? h("span", { class: "muted small" }, " 流式") : null) },
    { title: "模型", render: (r) => h("div", { class: "mono small" }, r.model_served || r.model_requested || "—",
      r.model_requested && r.model_served && r.model_requested.toLowerCase() !== r.model_served.toLowerCase()
        ? h("div", { class: "muted" }, "请求 " + r.model_requested) : null) },
    { title: "状态", cls: "num", render: (r) => h("span", { class: r.status >= 400 || r.error ? "err-text" : null }, r.status) },
    { title: "输入", cls: "num", render: (r) => fmtInt(r.input_tokens) },
    { title: "缓存", cls: "num", render: (r) => fmtInt(r.cached_tokens) },
    { title: "输出", cls: "num", render: (r) => fmtInt(r.output_tokens) },
    { title: "加权", cls: "num", render: (r) => fmtInt(r.weighted) },
    { title: "首字节 / 总耗时", cls: "num", render: (r) => `${fmtMs(r.ttfb_ms)} / ${fmtMs(r.duration_ms)}` },
    { title: "来源", render: (r) => h("div", { class: "small" }, h("div", { class: "mono" }, r.client_ip || "—"),
      r.user_agent ? h("div", { class: "muted" }, r.user_agent.slice(0, 40)) : null) },
    { title: "会话", render: (r) => (r.session_id
      ? h("a", { class: "mono small", href: `#/requests?session=${encodeURIComponent(r.session_id)}`, title: r.session_id }, r.session_id.slice(0, 8))
      : "—") },
    { title: "错误", render: (r) => (r.error ? h("span", { class: "err-text small", title: r.error }, errText(r.error)) : "") },
  ].filter(Boolean);
  return table(cols, rows, { empty: "没有符合条件的调用" });
}

const RANGES = { "24h": 86400000, "7d": 7 * 86400000, "30d": 30 * 86400000, all: 0 };

VIEWS.requests = async (main, route, ctx) => {
  const p = route.params;
  const range = RANGES[p.get("range")] !== undefined ? p.get("range") : "7d";
  const query = new URLSearchParams();
  for (const k of ["key_id", "alias", "route", "status", "model", "ip", "session", "error"]) {
    if (p.get(k)) query.set(k, p.get(k));
  }
  if (RANGES[range]) query.set("from", String(Date.now() - RANGES[range]));
  const d = await api("GET", "requests?limit=100&" + query);
  if (!ctx.alive()) return;

  const field = (name, placeholder, size) => h("input", { name, placeholder, value: p.get(name) || "", size });
  const aliasIn = state.me.kind === "key" ? null : field("alias", "Key 别名", 12);
  const routeSel = h("select", { name: "route" }, h("option", { value: "" }, "全部路由"),
    Object.entries(ROUTE_NAMES).map(([v, l]) => h("option", { value: v, selected: p.get("route") === v ? true : null }, l)));
  const statusSel = h("select", { name: "status" },
    [["", "全部状态"], ["ok", "成功"], ["error", "失败"]].map(([v, l]) =>
      h("option", { value: v, selected: (p.get("status") || "") === v ? true : null }, l)));
  const rangeSel = h("select", { name: "range" },
    [["24h", "近 24 小时"], ["7d", "近 7 天"], ["30d", "近 30 天"], ["all", "全部"]].map(([v, l]) =>
      h("option", { value: v, selected: v === range ? true : null }, l)));
  const modelIn = field("model", "模型", 12);
  const ipIn = field("ip", "来源 IP", 13);
  const sessionIn = field("session", "会话 ID", 12);
  const form = h("form", { class: "filters" }, rangeSel, aliasIn, routeSel, statusSel, modelIn, ipIn, sessionIn,
    h("button", { type: "submit", class: "primary" }, "查询"),
    h("a", { class: "btn", href: "api/requests.csv?" + query, download: "" }, "导出 CSV"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = new URLSearchParams();
    if (p.get("key_id")) q.set("key_id", p.get("key_id"));
    for (const el of form.elements) {
      if (el.name && el.value) q.set(el.name, el.value);
    }
    go("#/requests?" + q);
  });

  const head = h("div", { class: "page-head" }, h("div", null, h("h1", null, state.me.kind === "key" ? "调用记录" : "调用审计"),
    h("div", { class: "sub" }, "每次经网关的 API 调用一行：谁、何时、从哪里、用了哪个模型、多少 token、结果如何。不记录提示词内容。")));
  const note = p.get("key_id") ? h("div", { class: "notice" }, `只看 Key #${p.get("key_id")} 的调用。`,
    h("a", { href: "#/requests" }, " 看全部")) : null;
  const list = requestsTable(d.requests);
  const card = h("div", { class: "card" }, list);
  let next = d.next_before_id;
  const more = h("div", { class: "more" }, h("button", {
    onclick: async (ev) => {
      ev.currentTarget.disabled = true;
      const nd = await api("GET", `requests?limit=100&before_id=${next}&` + query);
      list.appendRows(nd.requests);
      next = nd.next_before_id;
      ev.currentTarget.disabled = false;
      more.hidden = !next;
    },
  }, "加载更多"));
  more.hidden = !next;
  card.append(more);
  main.replaceChildren(head, form, note, card);
};

/* ---------------------------------------------------------------- events */

function eventDetail(e) {
  const d = e.detail;
  if (!d) return "";
  if (typeof d !== "object") return String(d);
  return Object.entries(d).map(([k, v]) => {
    if (v && typeof v === "object" && "from" in v && "to" in v) return `${k}: ${fmtVal(v.from)} → ${fmtVal(v.to)}`;
    return `${k}: ${fmtVal(v)}`;
  }).join("；");
}

function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "空";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function eventsTable(rows) {
  return table([
    { title: "时间", cls: "nowrap", render: (e) => fmtTime(e.ts) },
    { title: "操作人", render: (e) => h("span", null, e.actor_name || "—", h("span", { class: "muted small" },
      ` ${({ user: "账号", key: "Key", cli: "命令行", anonymous: "匿名" })[e.actor_type] || e.actor_type}`)) },
    { title: "动作", render: (e) => ACTION_NAMES[e.action] || e.action },
    { title: "对象", render: (e) => (e.target_type === "key" && e.target_id
      ? keyLink(e.target_id, e.target_name) : e.target_name || "—") },
    { title: "详情", render: (e) => h("span", { class: "small ink2" }, eventDetail(e)) },
    { title: "IP", cls: "mono small", render: (e) => e.ip || "—" },
  ], rows, { empty: "暂无操作记录" });
}

VIEWS.events = async (main, route, ctx) => {
  const action = route.params.get("action") || "";
  const q = new URLSearchParams({ limit: "100" });
  if (action) q.set("action", action);
  const d = await api("GET", "events?" + q);
  if (!ctx.alive()) return;
  const sel = h("select", null, h("option", { value: "" }, "全部动作"),
    [["key.", "Key 相关"], ["login", "登录"], ["portal.", "Key 自助登录"], ["user.", "账号相关"], ["settings.", "设置"], ["audit.", "审计导出"], ["usage.", "历史导入"]]
      .map(([v, l]) => h("option", { value: v, selected: v === action ? true : null }, l)));
  sel.addEventListener("change", () => go("#/events" + (sel.value ? "?action=" + encodeURIComponent(sel.value) : "")));
  const list = eventsTable(d.events);
  const card = h("div", { class: "card" }, list);
  let next = d.next_before_id;
  const more = h("div", { class: "more" }, h("button", {
    onclick: async () => {
      const nq = new URLSearchParams(q);
      nq.set("before_id", next);
      const nd = await api("GET", "events?" + nq);
      list.appendRows(nd.events);
      next = nd.next_before_id;
      more.hidden = !next;
    },
  }, "加载更多"));
  more.hidden = !next;
  card.append(more);
  main.replaceChildren(h("div", { class: "page-head" }, h("div", null, h("h1", null, "操作日志"),
    h("div", { class: "sub" }, "登录、创建 / 修改 / 停用 / 轮换 / 吊销 Key、账号与设置变更，全部留痕。"))),
  h("div", { class: "filters" }, sel), card);
};

/* ----------------------------------------------------------------- users */

VIEWS.users = async (main, route, ctx) => {
  const d = await api("GET", "users");
  if (!ctx.alive()) return;
  const roles = state.settings.roles;
  const list = table([
    { title: "用户名", render: (u) => h("span", { class: "mono" }, u.username) },
    { title: "显示名", render: (u) => u.display_name || "—" },
    { title: "角色", render: (u) => ROLE_NAMES[u.role] || u.role },
    { title: "状态", render: (u) => (u.disabled ? h("span", { class: "badge disabled" }, "停用") : h("span", { class: "badge active" }, "启用")) },
    { title: "最近登录", cls: "nowrap", render: (u) => (u.last_login_at ? fmtTime(u.last_login_at) : "从未") },
    { title: "", render: (u) => h("button", { class: "link", onclick: () => openUserModal(u) }, "编辑") },
  ], d.users);
  const matrix = h("div", { class: "card" }, h("h2", null, "角色与权限"), table([
    { title: "权限", render: (r) => PERM_NAMES[r] || r },
    ...["admin", "operator", "auditor"].map((role) => ({
      title: ROLE_NAMES[role], cls: "num", render: (r) => (roles[role].includes(r) ? "✓" : "—"),
    })),
  ], Object.keys(PERM_NAMES)),
  h("p", { class: "hint" }, "Key 管理员只能看到和管理自己创建的 Key；任何人都可以用自己的 API Key 登录查看这把 Key 的用量。"));
  main.replaceChildren(h("div", { class: "page-head" }, h("div", null, h("h1", null, "控制台账号"),
    h("div", { class: "sub" }, "谁能登录控制台、能不能创建 API Key，由账号的角色决定。")),
  h("button", { class: "primary", onclick: () => openUserModal(null) }, "新建账号")),
  h("div", { class: "card" }, list), matrix);
};

/* -------------------------------------------------------------- settings */

VIEWS.settings = async (main, route, ctx) => {
  await loadSettings();
  if (!ctx.alive()) return;
  const st = state.settings.settings;
  const editable = state.settings.editable;
  const input = (name, value, step) => h("input", { name, type: "number", step, value, disabled: editable ? null : true });
  const form = h("form", { class: "form-grid" },
    h("label", { class: "field" }, "未缓存输入权重", input("weight_uncached", st.weight_uncached, "0.01")),
    h("label", { class: "field" }, "缓存输入权重", input("weight_cached", st.weight_cached, "0.01")),
    h("label", { class: "field" }, "输出权重", input("weight_output", st.weight_output, "0.01")),
    h("label", { class: "field" }, "调用明细保留天数", input("retention_days", st.retention_days, "1")),
    h("label", { class: "field" }, "显示时区（相对 UTC 的分钟数）", input("tz_offset_minutes", st.tz_offset_minutes, "15")),
    h("p", { class: "hint full" }, "加权用量是每把 Key 限额和排行的统一口径：未缓存输入 × 权重 + 缓存输入 × 权重 + 输出 × 权重，默认 1 / 0.1 / 8，与订阅额度的计量方式一致。改权重只影响之后的记录。按日汇总永久保留，逐条调用明细按保留天数清理。"),
    editable ? h("div", { class: "full" }, h("button", { type: "submit", class: "primary" }, "保存")) : null,
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {};
    for (const el of form.elements) if (el.name) body[el.name] = Number(el.value);
    try {
      await api("PATCH", "settings", body);
      toast("已保存");
      render();
    } catch (e) {
      toast(e.message);
    }
  });
  const base = location.origin + (state.settings.api_prefix || "");
  const connect = h("div", { class: "card" }, h("h2", null, "客户端接入地址"),
    h("pre", { class: "snippet" },
      `# Claude Code（Anthropic 协议）\nANTHROPIC_BASE_URL=${base}\nANTHROPIC_AUTH_TOKEN=<你的 API Key>\n\n` +
      `# OpenAI 协议客户端 / Codex CLI（Responses）\nbase_url = ${base}/v1\napi_key  = <你的 API Key>`));
  main.replaceChildren(h("div", { class: "page-head" }, h("div", null, h("h1", null, "设置"),
    h("div", { class: "sub" }, editable ? "只有管理员可以修改。" : "只读；只有管理员可以修改。"))),
  h("div", { class: "card" }, h("h2", null, "计量与保留"), form), connect);
};

/* -------------------------------------------------------------- upstream */

VIEWS.upstream = async (main, route, ctx) => {
  const d = await api("GET", "upstream");
  if (!ctx.alive()) return;
  const cat = d.catalog || {};
  const health = d.health && d.health.status === "ok"
    ? h("span", { class: "badge ok" }, "cx2cc 正常") : h("span", { class: "badge err" }, "cx2cc 不可达");
  const aliases = Object.entries(cat.aliases || {});
  const models = h("div", { class: "card" }, h("h2", null, "可用模型"),
    h("p", { class: "muted small" }, `默认模型 ${cat.default_model || "—"} · 未知模型策略 ${cat.unknown_model_policy || "—"} · 目录拉取于 ${d.catalog_fetched_at ? fmtTime(d.catalog_fetched_at * 1000) : "—"}`),
    table([
      { title: "模型", render: (m) => h("span", { class: "mono" }, m.id) },
      { title: "显示名", render: (m) => m.display_name || "—" },
      { title: "实际服务", render: (m) => h("span", { class: "mono" }, m.served_as || m.id) },
      { title: "上下文", cls: "num", render: (m) => (m.context_window ? fmtCompact(m.context_window) : "—") },
      { title: "推理强度", render: (m) => (m.reasoning_efforts || []).join(" / ") || "—" },
    ], cat.data || []));
  const aliasCard = h("div", { class: "card" }, h("h2", null, "模型别名"),
    h("p", { class: "muted small" }, "客户端请求左边的名字时，cx2cc 实际调用右边的模型。Key 的模型白名单按两边任一名字匹配。"),
    table([
      { title: "请求的模型名", render: (a) => h("span", { class: "mono" }, a[0]) },
      { title: "实际模型", render: (a) => h("span", { class: "mono" }, a[1]) },
    ], aliases, { empty: "没有配置别名" }));
  const acc = d.accounts && d.accounts.accounts ? d.accounts.accounts : [];
  const pool = h("div", { class: "card" }, h("h2", null, "订阅账号池"), d.accounts && d.accounts.error
    ? h("p", { class: "err-text" }, d.accounts.error)
    : table([
      { title: "账号", render: (a) => h("div", null, h("span", { class: "mono" }, a.id), a.id === d.accounts.active ? h("span", { class: "tag" }, "当前") : null) },
      { title: "套餐", render: (a) => a.plan || "—" },
      { title: "邮箱", cls: "small", render: (a) => a.email || "—" },
      { title: "窗口已用", cls: "num", render: (a) => (a.used_percent !== undefined ? a.used_percent + "%" : "—") },
      { title: "窗口重置", cls: "nowrap", render: (a) => (a.window_reset_at ? fmtTime(a.window_reset_at * 1000) : "—") },
      { title: "状态", render: (a) => (a.available ? h("span", { class: "badge ok" }, "可用")
        : h("span", { class: "badge warn" }, a.skipped || a.cooldown_reason || "冷却中",
          a.cooldown_until ? ` 至 ${fmtTime(a.cooldown_until * 1000, false)}` : "")) },
    ], acc));
  const usage = d.usage || {};
  const rl = usage.rate_limit || {};
  const win = rl.primary_window || {};
  const quota = h("div", { class: "card" }, h("h2", null, "当前账号的订阅额度"), usage.error
    ? h("p", { class: "err-text" }, usage.error)
    : h("dl", { class: "kv" },
      h("dt", null, "套餐"), h("dd", null, usage.plan_type || "—"),
      h("dt", null, "主窗口已用"), h("dd", null, win.used_percent !== undefined ? win.used_percent + "%" : "—"),
      h("dt", null, "窗口长度"), h("dd", null, win.limit_window_seconds ? Math.round(win.limit_window_seconds / 86400) + " 天" : "—"),
      h("dt", null, "重置时间"), h("dd", null, win.reset_at ? fmtTime(win.reset_at * 1000) : "—"),
      h("dt", null, "已触顶"), h("dd", null, rl.limit_reached ? "是" : "否")));
  main.replaceChildren(h("div", { class: "page-head" }, h("div", null, h("h1", null, "上游与模型"),
    h("div", { class: "sub" }, "网关后面的 cx2cc 与 codex-bridge：模型目录、别名、订阅账号池（只读）。"))),
  h("div", { class: "filters" }, health), h("div", { class: "grid2" }, aliasCard, quota), models, pool);
};

/* ---------------------------------------------------------------- modals */

function openModal(title, body, { submitLabel = "确定", danger = false, onSubmit, cancelLabel = "取消" } = {}) {
  const error = h("div", { class: "notice err error", hidden: true });
  const submitBtn = onSubmit ? h("button", { type: "submit", class: danger ? "danger solid" : "primary" }, submitLabel) : null;
  const form = h("form", { class: "modal", role: "dialog", "aria-modal": "true" }, h("h2", null, title), body, error,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close() }, onSubmit ? cancelLabel : "关闭"), submitBtn));
  const backdrop = h("div", { class: "backdrop" }, form);
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!onSubmit) return close();
    error.hidden = true;
    submitBtn.disabled = true;
    try {
      await onSubmit(form, close);
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      submitBtn.disabled = false;
    }
  });
  document.body.append(backdrop);
  const first = form.querySelector("input:not([disabled]),select,textarea");
  if (first) first.focus();
  return { close, form };
}

function openKeyModal(k) {
  const editing = !!k;
  const scopes = state.settings.scopes;
  const current = editing ? k.scopes : state.settings.default_scopes;
  const num = (name, label, value, hint) => h("label", { class: "field" }, label,
    h("input", { name, type: "number", min: 0, step: 1, value: value || "", placeholder: "不限" }),
    hint ? h("span", { class: "hint" }, hint) : null);
  const body = h("div", { class: "form-grid" },
    h("label", { class: "field" }, "别名 *", h("input", { name: "alias", required: true, maxlength: 64, value: editing ? k.alias : "", placeholder: "如 bob-macbook" })),
    h("label", { class: "field" }, "使用人", h("input", { name: "owner", maxlength: 64, value: editing ? k.owner : "", placeholder: "谁在用这把 Key" })),
    h("label", { class: "field full" }, "备注", h("input", { name: "note", maxlength: 500, value: editing ? k.note : "" })),
    h("div", { class: "field full" }, h("span", { class: "ink2 small" }, "权限范围"), h("div", { class: "checks" },
      scopes.map((sc) => {
        const locked = sc === "accounts" && !can("keys.grant_accounts");
        return h("label", { title: locked ? "只有管理员可以授予" : null },
          h("input", { type: "checkbox", name: "scope", value: sc, checked: current.includes(sc), disabled: locked ? true : null }),
          SCOPE_NAMES[sc] || sc);
      }))),
    h("label", { class: "field full" }, "模型白名单",
      h("input", { name: "allowed_models", value: editing ? k.allowed_models.join(", ") : "", placeholder: "留空 = 不限，例如 gpt-6.1-sol, gpt-6-luna" }),
      h("span", { class: "hint" }, "逗号分隔；请求名或别名解析后的实际模型任一命中即放行。")),
    num("rpm_limit", "每分钟请求数上限", editing ? k.rpm_limit : ""),
    num("concurrency_limit", "并发请求上限", editing ? k.concurrency_limit : ""),
    num("daily_limit", "每日加权额度", editing ? k.daily_limit : "", "按显示时区的自然日计"),
    num("weekly_limit", "近 7 日加权额度", editing ? k.weekly_limit : "", "含今天在内的 7 个自然日"),
    h("label", { class: "field" }, "有效期（天）", h("input", { name: "expires_in_days", type: "number", min: 0, step: 1, placeholder: editing ? (k.expires_at ? "当前到期 " + fmtTime(k.expires_at, false) : "当前长期有效") : "留空 = 长期" }),
      editing ? h("span", { class: "hint" }, "填写则从现在起重新计算；留空不改。") : null),
  );
  openModal(editing ? `编辑 ${k.alias}` : "新建 API Key", body, {
    submitLabel: editing ? "保存" : "创建",
    onSubmit: async (form, close) => {
      const f = form.elements;
      const payload = {
        alias: f.alias.value.trim(),
        owner: f.owner.value.trim(),
        note: f.note.value.trim(),
        scopes: [...form.querySelectorAll("input[name=scope]")].filter((x) => x.checked).map((x) => x.value),
        allowed_models: f.allowed_models.value,
        rpm_limit: f.rpm_limit.value || null,
        concurrency_limit: f.concurrency_limit.value || null,
        daily_limit: f.daily_limit.value || null,
        weekly_limit: f.weekly_limit.value || null,
      };
      if (f.expires_in_days.value) payload.expires_in_days = f.expires_in_days.value;
      if (editing) {
        await api("PATCH", `keys/${k.id}`, payload);
        close();
        toast("已保存");
        render();
      } else {
        const d = await api("POST", "keys", payload);
        close();
        showSecret(d.record, d.key, "API Key 已创建（只显示这一次）");
      }
    },
  });
}

function showSecret(record, secret, title) {
  const base = location.origin + (state.settings.api_prefix || "");
  const snippet = `# Claude Code\nANTHROPIC_BASE_URL=${base}\nANTHROPIC_AUTH_TOKEN=${secret}\n\n` +
    `# OpenAI 协议客户端 / Codex CLI\nbase_url = ${base}/v1\napi_key  = ${secret}`;
  const body = h("div", null,
    h("p", null, `别名 ${record.alias}。这是这把 Key 唯一一次以明文出现，关闭后只能轮换出新值。`),
    h("div", { class: "secret" }, h("code", null, secret), h("button", { type: "button", onclick: () => copy(secret) }, "复制")),
    h("h3", { class: "small" }, "客户端配置"),
    h("pre", { class: "snippet" }, snippet),
    h("div", { class: "more" }, h("button", { type: "button", onclick: () => copy(snippet) }, "复制配置")));
  const m = openModal(title, body, {});
  const done = m.close;
  m.form.querySelector(".actions button").addEventListener("click", () => {
    done();
    go(`#/key/${record.id}`);
  });
}

function openRotateModal(k) {
  const body = h("div", null,
    h("p", null, "轮换会生成新的 Key 值。旧值可以保留一段宽限期，让使用人有时间换上新值；宽限期为 0 则立即失效。"),
    h("label", { class: "field" }, "旧 Key 宽限小时数（0–168）", h("input", { name: "grace", type: "number", min: 0, max: 168, value: 24 })));
  openModal(`轮换 ${k.alias}`, body, {
    submitLabel: "轮换",
    onSubmit: async (form, close) => {
      const d = await api("POST", `keys/${k.id}/rotate`, { grace_hours: Number(form.elements.grace.value || 0) });
      close();
      showSecret(d.record, d.key, "新的 Key（只显示这一次）");
    },
  });
}

function openRevokeModal(k) {
  const body = h("div", null,
    h("p", null, `吊销后「${k.alias}」立即失效且无法恢复，调用记录和操作日志保留。只是暂时不让用，请选「停用」。`),
    h("label", { class: "field" }, `输入别名 ${k.alias} 确认`, h("input", { name: "confirm", autocomplete: "off" })));
  openModal(`吊销 ${k.alias}`, body, {
    submitLabel: "吊销", danger: true,
    onSubmit: async (form, close) => {
      if (form.elements.confirm.value.trim() !== k.alias) throw new Error("别名不一致");
      await api("POST", `keys/${k.id}/revoke`, {});
      close();
      toast("已吊销");
      render();
    },
  });
}

function openUserModal(u) {
  const editing = !!u;
  const body = h("div", { class: "form-grid" },
    h("label", { class: "field" }, "用户名", h("input", { name: "username", required: true, value: editing ? u.username : "", disabled: editing ? true : null, autocomplete: "off" })),
    h("label", { class: "field" }, "显示名", h("input", { name: "display_name", value: editing ? u.display_name : "" })),
    h("label", { class: "field" }, "角色", h("select", { name: "role" }, ["admin", "operator", "auditor"].map((r) =>
      h("option", { value: r, selected: (editing ? u.role : "operator") === r ? true : null }, ROLE_NAMES[r])))),
    h("label", { class: "field" }, editing ? "重置密码（留空不改）" : "初始密码", h("input", { name: "password", type: "password", autocomplete: "new-password", minlength: 10, required: editing ? null : true })),
    editing ? h("label", { class: "field full" }, h("span", { class: "checks" }, h("label", null,
      h("input", { type: "checkbox", name: "disabled", checked: !!u.disabled }), "停用这个账号（立即登出）"))) : null,
    h("p", { class: "hint full" }, "管理员：全部权限；Key 管理员：可以创建 API Key，只能看到和管理自己创建的 Key；审计员：只读查看全部 Key、调用审计和操作日志。"));
  openModal(editing ? `编辑账号 ${u.username}` : "新建控制台账号", body, {
    submitLabel: editing ? "保存" : "创建",
    onSubmit: async (form, close) => {
      const f = form.elements;
      if (editing) {
        const payload = { display_name: f.display_name.value.trim(), role: f.role.value, disabled: f.disabled.checked };
        if (f.password.value) payload.password = f.password.value;
        await api("PATCH", `users/${u.id}`, payload);
      } else {
        await api("POST", "users", {
          username: f.username.value.trim(), display_name: f.display_name.value.trim(),
          role: f.role.value, password: f.password.value,
        });
      }
      close();
      toast("已保存");
      render();
    },
  });
}

function openPasswordModal() {
  const body = h("div", { class: "form-grid" },
    h("label", { class: "field full" }, "原密码", h("input", { name: "old", type: "password", autocomplete: "current-password", required: true })),
    h("label", { class: "field full" }, "新密码（至少 10 位）", h("input", { name: "new", type: "password", autocomplete: "new-password", minlength: 10, required: true })));
  openModal("修改密码", body, {
    submitLabel: "修改",
    onSubmit: async (form, close) => {
      await api("POST", "password", { old_password: form.elements.old.value, new_password: form.elements.new.value });
      close();
      toast("密码已修改，其他设备上的登录已退出");
    },
  });
}

boot();
