"use strict";

const RES_LABEL = {
  0: "机房",
  1: "住宅",
  2: "住宅代理",
  null: "未知",
};
const RES_CLASS = {
  0: "datacenter",
  1: "residential",
  2: "residential_proxy",
  null: "unknown",
};

const state = {
  offset: 0,
  limit: 50,
  sort: "id",
  desc: false,
  total: 0,
  items: [],
};

const $ = (id) => document.getElementById(id);

function escapeHtml(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(sec) {
  if (!sec) return "-";
  try {
    return new Date(sec * 1000).toLocaleString();
  } catch {
    return String(sec);
  }
}

async function api(path, options) {
  const resp = await fetch(path, options);
  const text = await resp.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!resp.ok) {
    const msg = body && body.error ? body.error.message : `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

function toast(message, kind) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (kind ? " " + kind : "");
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 2600);
}

async function loadHealth() {
  const badge = $("health-badge");
  try {
    const h = await api("/health");
    badge.textContent =
      (h.status === "ok" ? "健康" : "降级") +
      " · 源库" + (h.source_db.reachable ? "可达" : "不可达") +
      " · " + h.app_db.nodes + " 节点";
    badge.className = "badge " + (h.status === "ok" ? "ok" : "degraded");
    $("version").textContent = "v" + h.version;
  } catch (e) {
    badge.textContent = "连接失败";
    badge.className = "badge degraded";
  }
}

function card(label, value, cls, sub) {
  return `<div class="card">
    <div class="card-label">${escapeHtml(label)}</div>
    <div class="card-value ${cls || ""}">${escapeHtml(value)}</div>
    ${sub ? `<div class="card-sub">${escapeHtml(sub)}</div>` : ""}
  </div>`;
}

function renderCards(s) {
  const r = s.by_residential || {};
  const a = s.by_as_type || {};
  const p = s.by_protocol || {};
  $("stats-cards").innerHTML = [
    card("节点总数", s.nodes_total, "blue"),
    card("http", p.http || 0, ""),
    card("https", p.https || 0, ""),
    card("住宅", r.residential || 0, "green"),
    card("机房", r.datacenter || 0, "amber"),
    card("住宅代理", r.residential_proxy || 0, "violet"),
    card("未知", r.unknown || 0, ""),
    card("已富化", s.enriched || 0, "green", `未富化 ${s.not_enriched || 0}`),
    card("AS hosting", a.hosting || 0, ""),
    card("AS isp", a.isp || 0, ""),
    card("AS business", a.business || 0, ""),
    card("AS 未知", a.unknown || 0, ""),
  ].join("");
  $("last-sync").textContent =
    "最近同步: " + fmtTime(s.last_sync_at) + (s.last_sync_status ? ` (${s.last_sync_status})` : "");
}

async function loadStats() {
  try {
    const s = await api("/stats");
    renderCards(s);
  } catch (e) {
    toast("加载统计失败: " + e.message, "err");
  }
}

function buildQuery() {
  const params = new URLSearchParams();
  const protocol = $("f-protocol").value;
  const residential = $("f-residential").value;
  const asType = $("f-as_type").value;
  const country = $("f-country").value.trim().toUpperCase();
  const enriched = $("f-enriched").value;
  const maxLatency = $("f-max-latency").value;

  if (protocol) params.set("protocol", protocol);
  if (residential) params.set("residential", residential);
  if (asType) params.set("as_type", asType);
  if (country) params.set("country", country);
  if (enriched) params.set("enriched", enriched);
  if (maxLatency) params.set("max_latency_ms", maxLatency);
  params.set("limit", state.limit);
  params.set("offset", state.offset);
  if (state.sort) params.set("sort", (state.desc ? "-" : "") + state.sort);
  return params;
}

function resTag(v) {
  const cls = RES_CLASS[v === undefined ? null : v];
  const label = RES_LABEL[v === undefined ? null : v];
  return `<span class="tag ${cls}">${label}</span>`;
}

function latTag(ms) {
  if (ms === null || ms === undefined) return '<span class="muted">-</span>';
  const cls = ms < 100 ? "fast" : ms < 300 ? "mid" : "slow";
  return `<span class="lat ${cls}">${ms} ms</span>`;
}

function renderTable(items) {
  const body = $("nodes-body");
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="11">暂无数据</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map((n) => {
      const e = n.enrichment || {};
      return `<tr data-ip="${escapeHtml(n.ip)}" data-port="${n.port}">
        <td>${escapeHtml(n.ip)}:${n.port}</td>
        <td><span class="tag ${escapeHtml(n.protocol)}">${escapeHtml(n.protocol)}</span></td>
        <td>${escapeHtml(n.tag)}</td>
        <td>${resTag(n.is_residential)}</td>
        <td>${escapeHtml(e.as_type)}</td>
        <td>${escapeHtml(e.asn)}</td>
        <td>${escapeHtml(e.as_name)}</td>
        <td>${escapeHtml(e.country_code)}${e.city ? " / " + escapeHtml(e.city) : ""}</td>
        <td>${latTag(n.latency_ms)}</td>
        <td>${escapeHtml((n.egress_region || "").toUpperCase())}${n.failure_count ? " · 失败" + n.failure_count : ""}</td>
        <td>${n.enriched ? fmtTime(n.last_enriched_at) : "未富化"}</td>
      </tr>`;
    })
    .join("");

  body.querySelectorAll("tr[data-ip]").forEach((tr) => {
    tr.addEventListener("click", () => openDetail(tr.dataset.ip, Number(tr.dataset.port)));
  });
}

async function loadNodes() {
  try {
    const data = await api("/nodes?" + buildQuery().toString());
    state.total = data.total;
    state.items = data.items;
    renderTable(data.items);
    const from = state.total === 0 ? 0 : state.offset + 1;
    const to = Math.min(state.offset + state.limit, state.total);
    $("table-count").textContent = `共 ${state.total} 条，当前 ${from}-${to}`;
    const page = Math.floor(state.offset / state.limit) + 1;
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    $("page-info").textContent = `第 ${page} / ${pages} 页`;
    $("prev-page").disabled = state.offset <= 0;
    $("next-page").disabled = state.offset + state.limit >= state.total;
  } catch (e) {
    toast("加载节点失败: " + e.message, "err");
  }
}

async function openDetail(ip, port) {
  const modal = $("modal");
  const body = $("modal-body");
  $("modal-title").textContent = `${ip}:${port}`;
  body.innerHTML = "<p>加载中…</p>";
  modal.hidden = false;
  try {
    const node = await api(`/node/${encodeURIComponent(ip)}/${encodeURIComponent(port)}`);
    const e = node.enrichment || {};
    const rows = [
      ["ip:port", `${node.ip}:${node.port}`],
      ["协议", node.protocol],
      ["原始类型", node.raw_type],
      ["Tag", node.tag],
      ["住宅属性", RES_LABEL[node.is_residential === undefined ? null : node.is_residential]],
      ["已富化", node.enriched ? "是" : "否"],
      ["富强来源", e.source],
      ["富强档位", e.plan],
      ["ASN", e.asn],
      ["AS 名称", e.as_name],
      ["AS 类型", e.as_type],
      ["is_hosting", fmtBool(e.is_hosting)],
      ["is_anonymous", fmtBool(e.is_anonymous)],
      ["国家", e.country_code],
      ["城市", e.city],
      ["地区", e.region],
      ["经纬度", e.latitude != null && e.longitude != null ? `${e.latitude}, ${e.longitude}` : "-"],
      ["延迟", node.latency_ms != null ? node.latency_ms + " ms" : "-"],
      ["各域名延迟", node.latencies ? Object.entries(node.latencies).map(([d, v]) => `${d}=${v}ms`).join(", ") : "-"],
      ["延迟更新时间", fmtTime(node.latency_updated_at)],
      ["失败计数", node.failure_count],
      ["熔断开始", fmtTime(node.circuit_open_since)],
      ["出口 IP", node.egress_ip],
      ["出口地区", node.egress_region],
      ["出口更新时间", fmtTime(node.egress_updated_at)],
      ["富强时间", fmtTime(node.last_enriched_at)],
      ["创建时间", fmtTime(node.created_at)],
      ["更新时间", fmtTime(node.updated_at)],
    ];
    let html = `<div class="kv">${rows
      .map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v ?? "-")}</div>`)
      .join("")}</div>`;

    let enrRaw = e.raw;
    try {
      const enr = await api(`/enrichment/${encodeURIComponent(ip)}`);
      if (enr && enr.raw) enrRaw = enr.raw;
    } catch {
      /* 未富化时忽略 */
    }
    html += `<h3 class="section">富化原始数据 (ipinfo raw)</h3>`;
    html += `<pre>${escapeHtml(enrRaw ? JSON.stringify(enrRaw, null, 2) : "无")}</pre>`;
    html += `<h3 class="section">节点原始配置 (raw_options_json)</h3>`;
    html += `<pre>${escapeHtml(
      node.raw_options_json ? JSON.stringify(node.raw_options_json, null, 2) : "无"
    )}</pre>`;
    body.innerHTML = html;
  } catch (err) {
    body.innerHTML = `<p class="tag unknown">加载失败: ${escapeHtml(err.message)}</p>`;
  }
}

function fmtBool(v) {
  if (v === null || v === undefined) return "-";
  return v ? "true" : "false";
}

async function triggerRefresh() {
  const token = $("refresh-token").value.trim();
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  try {
    const resp = await fetch("/refresh", { method: "POST", headers });
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 202) {
      toast(body.status === "skipped" ? "已有同步任务在运行" : "已触发同步 (run " + body.run_id + ")", "ok");
      if (typeof TaskProgress !== "undefined") TaskProgress.watch("ingest", "ingest-progress");
    } else if (resp.status === 401) {
      toast("鉴权失败：REFRESH_TOKEN 不正确", "err");
    } else {
      toast("触发失败: HTTP " + resp.status, "err");
    }
  } catch (e) {
    toast("触发失败: " + e.message, "err");
  }
}

function bindSorting() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (state.sort === col) {
        state.desc = !state.desc;
      } else {
        state.sort = col;
        state.desc = false;
      }
      document.querySelectorAll("th[data-sort]").forEach((x) => x.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(state.desc ? "sort-desc" : "sort-asc");
      state.offset = 0;
      loadNodes();
    });
  });
}

function bindEvents() {
  $("apply-filters").addEventListener("click", () => {
    state.offset = 0;
    loadNodes();
  });
  $("reset-filters").addEventListener("click", () => {
    $("f-protocol").value = "";
    $("f-residential").value = "";
    $("f-as_type").value = "";
    $("f-country").value = "";
    $("f-enriched").value = "";
    $("f-max-latency").value = "";
    state.offset = 0;
    loadNodes();
  });
  $("f-limit").addEventListener("change", (e) => {
    state.limit = Number(e.target.value);
    state.offset = 0;
    loadNodes();
  });
  $("prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadNodes();
  });
  $("next-page").addEventListener("click", () => {
    if (state.offset + state.limit < state.total) {
      state.offset += state.limit;
      loadNodes();
    }
  });
  $("refresh-stats").addEventListener("click", () => {
    loadHealth();
    loadStats();
    loadNodes();
  });
  $("trigger-refresh").addEventListener("click", triggerRefresh);
  $("export-data").addEventListener("click", () => {
    const p = buildQuery();
    p.set("format", $("export-format").value);
    window.location.href = "/export/nodes?" + p.toString();
  });
  $("modal-close").addEventListener("click", () => ($("modal").hidden = true));
  $("modal").addEventListener("click", (e) => {
    if (e.target === $("modal")) $("modal").hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("modal").hidden = true;
  });
}

async function init() {
  bindEvents();
  bindSorting();
  state.limit = Number($("f-limit").value);
  await Promise.all([loadHealth(), loadStats()]);
  await loadNodes();
  if (typeof TaskProgress !== "undefined") TaskProgress.watch("ingest", "ingest-progress");
}

document.addEventListener("DOMContentLoaded", init);
