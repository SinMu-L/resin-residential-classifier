"use strict";

const state = { offset: 0, limit: 50, sort: "pool_score", desc: true, total: 0 };

function card(label, value, cls, sub) {
  return `<div class="card">
    <div class="card-label">${escapeHtml(label)}</div>
    <div class="card-value ${cls || ""}">${escapeHtml(value)}</div>
    ${sub ? `<div class="card-sub">${escapeHtml(sub)}</div>` : ""}
  </div>`;
}

function renderCards(s) {
  const st = s.by_state || {};
  const run = s.run || {};
  $("pool-cards").innerHTML = [
    card("池内节点", s.total || 0, "blue"),
    card("已入池", st.in_pool || 0, "green"),
    card("观察期", st.probation || 0, "amber"),
    card("探测中", st.probing || 0, "violet"),
    card("已淘汰", st.evicted || 0, "red"),
    card("平均风险分", s.avg_abuse_score ?? "-", ""),
    card("住宅 ASN", (s.asn_registry || {}).residential || 0, "green"),
    card("机房 ASN", (s.asn_registry || {}).cloud || 0, "amber"),
    card("运行状态", run.running ? `${run.phase || "运行中"} ${run.done || 0}/${run.total || 0}` : "空闲", run.running ? "amber" : ""),
  ].join("");
  const top = (s.top_asn_in_pool || [])
    .map((t) => `${t.asn}(${t.count})`)
    .join("  ");
  $("pool-top").textContent = top ? "Top ASN(池内): " + top : "";
}

async function loadStats() {
  try {
    const s = await api("/pool/stats");
    renderCards(s);
    if (s.run && s.run.running) setTimeout(loadStats, 1500);
  } catch (e) {
    toast("加载池统计失败: " + e.message, "err");
  }
}

function buildQuery() {
  const p = new URLSearchParams();
  const st = $("f-state").value;
  const protocol = $("f-protocol").value;
  const maxAbuse = $("f-max-abuse").value;
  const minPool = $("f-min-pool").value;
  const maxLatency = $("f-max-latency").value;
  const q = $("f-q").value.trim();
  if (st) p.set("state", st);
  if (protocol) p.set("protocol", protocol);
  if (maxAbuse) p.set("max_abuse_score", maxAbuse);
  if (minPool) p.set("min_pool_score", minPool);
  if (maxLatency) p.set("max_latency_ms", maxLatency);
  if (q) p.set("q", q);
  p.set("limit", state.limit);
  p.set("offset", state.offset);
  if (state.sort) p.set("sort", (state.desc ? "-" : "") + state.sort);
  return p;
}

function renderTable(items) {
  const body = $("pool-body");
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="11">暂无数据</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (n) => `<tr data-ip="${escapeHtml(n.ip)}">
      <td>${escapeHtml(n.ip)}${n.port ? ":" + n.port : ""}${n.port_count > 1 ? ` <span class="muted">(+${n.port_count - 1})</span>` : ""}</td>
      <td>${stateTag(n.state)}</td>
      <td>${escapeHtml(n.asn)}</td>
      <td>${n.abuse_score ?? "-"}</td>
      <td>${n.pool_score ?? "-"}</td>
      <td>${latTag(n.latency_ms)}</td>
      <td>${escapeHtml((n.egress_region || "").toUpperCase())}${n.failure_count ? " · 失败" + n.failure_count : ""}</td>
      <td>${n.consecutive_failures}</td>
      <td>${fmtTime(n.admitted_at)}</td>
      <td>${fmtTime(n.last_checked_at)}</td>
      <td>${escapeHtml(n.evict_reason || n.reason)}</td>
    </tr>`
    )
    .join("");

  body.querySelectorAll("tr[data-ip]").forEach((tr) => {
    tr.addEventListener("click", () => openDetail(tr.dataset.ip));
  });
}

async function loadPool() {
  try {
    const data = await api("/pool?" + buildQuery().toString());
    state.total = data.total;
    renderTable(data.items);
    const from = state.total === 0 ? 0 : state.offset + 1;
    const to = Math.min(state.offset + state.limit, state.total);
    const page = Math.floor(state.offset / state.limit) + 1;
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    $("table-count").textContent = `共 ${state.total} 条，当前 ${from}-${to}`;
    $("page-info").textContent = `第 ${page} / ${pages} 页`;
    $("prev-page").disabled = state.offset <= 0;
    $("next-page").disabled = state.offset + state.limit >= state.total;
  } catch (e) {
    toast("加载节点池失败: " + e.message, "err");
  }
}

async function openDetail(ip) {
  openModal(ip + " (池)", "<p>加载中…</p>");
  try {
    const n = await api(`/pool/${encodeURIComponent(ip)}/0`);
    const rows = [
      ["IP", n.ip],
      ["代表端口", n.port],
      ["端口数", n.port_count],
      ["状态", STATE_LABEL[n.state] || n.state],
      ["ASN", n.asn],
      ["风险分", n.abuse_score],
      ["纯净分", n.pool_score],
      ["延迟", n.latency_ms != null ? n.latency_ms + " ms" : "-"],
      ["出口 IP", n.egress_ip],
      ["出口地区", n.egress_region],
      ["失败计数", n.failure_count],
      ["连续失败", n.consecutive_failures],
      ["原因", n.reason],
      ["入池时间", fmtTime(n.admitted_at)],
      ["最近检测", fmtTime(n.last_checked_at)],
      ["下次检测", fmtTime(n.next_check_at)],
      ["淘汰时间", fmtTime(n.evicted_at)],
      ["淘汰原因", n.evict_reason],
    ];
    let html = `<div class="detail-actions"><button id="recheck-node" class="btn primary">重新检测此节点</button></div>`;
    html += `<div class="kv">${rows
      .map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v ?? "-")}</div>`)
      .join("")}</div>`;
    if (n.checks && n.checks.length) {
      html += `<h3 class="section">检测历史</h3>`;
      html += `<table><thead><tr><th>时间</th><th>阶段</th><th>通过</th><th>分数</th><th>原因</th></tr></thead><tbody>`;
      html += n.checks
        .map(
          (c) => `<tr><td>${fmtTime(c.checked_at)}</td><td>${escapeHtml(c.stage)}</td>
          <td>${c.passed ? "是" : "否"}</td><td>${c.score ?? "-"}</td><td>${escapeHtml(c.reason || "")}</td></tr>`
        )
        .join("");
      html += `</tbody></table>`;
    }
    openModal(n.ip + " (池)", html);
    const reBtn = $("recheck-node");
    if (reBtn) reBtn.addEventListener("click", () => recheckNode(n.ip));
  } catch (err) {
    openModal(ip + " (池)", `<p class="tag unknown">加载失败: ${escapeHtml(err.message)}</p>`);
  }
}

function bindSorting() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (state.sort === col) state.desc = !state.desc;
      else {
        state.sort = col;
        state.desc = true;
      }
      document.querySelectorAll("th[data-sort]").forEach((x) => x.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(state.desc ? "sort-desc" : "sort-asc");
      state.offset = 0;
      loadPool();
    });
  });
}

async function rebuild(force) {
  try {
    const url = force ? "/pool/rebuild?force=true" : "/pool/rebuild";
    const resp = await fetch(url, { method: "POST", headers: authHeaders() });
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 202) {
      toast(body.status === "skipped" ? "已有池任务在运行" : (force ? "已触发强制重检" : "已触发池重建"), "ok");
      if (typeof TaskProgress !== "undefined") TaskProgress.watch("pool", "pool-progress");
    } else if (resp.status === 401) {
      toast("鉴权失败：REFRESH_TOKEN 不正确", "err");
    } else {
      toast("触发失败: HTTP " + resp.status, "err");
    }
    setTimeout(() => {
      loadStats();
      loadPool();
    }, 1200);
  } catch (e) {
    toast("触发失败: " + e.message, "err");
  }
}

async function recheckNode(ip) {
  const btn = $("recheck-node");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "检测中…";
  }
  try {
    const resp = await fetch(`/pool/${encodeURIComponent(ip)}/0/recheck`, {
      method: "POST",
      headers: authHeaders(),
    });
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 200) {
      toast(`已重新检测 ${ip}`, "ok");
      await loadStats();
      await loadPool();
      openDetail(ip);
    } else if (resp.status === 202) {
      toast(body.message || "已有池任务在运行", "err");
      if (btn) { btn.disabled = false; btn.textContent = "重新检测此节点"; }
    } else if (resp.status === 401) {
      toast("鉴权失败：REFRESH_TOKEN 不正确", "err");
      if (btn) { btn.disabled = false; btn.textContent = "重新检测此节点"; }
    } else {
      toast("检测失败: " + (body && body.error ? body.error.message : "HTTP " + resp.status), "err");
      if (btn) { btn.disabled = false; btn.textContent = "重新检测此节点"; }
    }
  } catch (e) {
    toast("检测失败: " + e.message, "err");
    if (btn) { btn.disabled = false; btn.textContent = "重新检测此节点"; }
  }
}

function bindEvents() {
  $("apply-filters").addEventListener("click", () => {
    state.offset = 0;
    loadPool();
  });
  $("reset-filters").addEventListener("click", () => {
    ["f-state", "f-protocol", "f-max-abuse", "f-min-pool", "f-max-latency", "f-q"].forEach((id) => ($(id).value = ""));
    state.offset = 0;
    loadPool();
  });
  $("f-limit").addEventListener("change", (e) => {
    state.limit = Number(e.target.value);
    state.offset = 0;
    loadPool();
  });
  $("prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadPool();
  });
  $("next-page").addEventListener("click", () => {
    if (state.offset + state.limit < state.total) {
      state.offset += state.limit;
      loadPool();
    }
  });
  $("reload").addEventListener("click", () => {
    loadStats();
    loadPool();
  });
  $("rebuild-pool").addEventListener("click", () => rebuild(false));
  $("force-rebuild").addEventListener("click", () => rebuild(true));
  $("export-data").addEventListener("click", () => {
    const p = buildQuery();
    p.set("format", $("export-format").value);
    window.location.href = "/export/pool?" + p.toString();
  });
}

async function init() {
  bindEvents();
  bindSorting();
  bindModal();
  state.limit = Number($("f-limit").value);
  await loadStats();
  await loadPool();
  if (typeof TaskProgress !== "undefined") TaskProgress.watch("pool", "pool-progress");
}

document.addEventListener("DOMContentLoaded", init);
