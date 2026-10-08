"use strict";

const state = { offset: 0, limit: 100, sort: "asn", desc: false, total: 0, editing: null };

function buildQuery() {
  const p = new URLSearchParams();
  const category = $("f-category").value;
  const enabled = $("f-enabled").value;
  const q = $("f-q").value.trim();
  if (category) p.set("category", category);
  if (enabled) p.set("enabled", enabled);
  if (q) p.set("q", q);
  p.set("limit", state.limit);
  p.set("offset", state.offset);
  if (state.sort) p.set("sort", (state.desc ? "-" : "") + state.sort);
  return p;
}

function renderTable(items) {
  const body = $("asn-body");
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="8">暂无数据</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (a) => `<tr data-asn="${escapeHtml(a.asn)}">
      <td>${escapeHtml(a.asn)}</td>
      <td><span class="tag ${a.category === "cloud" ? "datacenter" : "residential"}">${escapeHtml(a.category)}</span></td>
      <td>${escapeHtml(a.org)}</td>
      <td>${escapeHtml(a.country)}</td>
      <td>${a.enabled ? "✅" : "—"}</td>
      <td>${escapeHtml(a.source)}</td>
      <td>${fmtTime(a.updated_at)}</td>
      <td class="actions">
        <button class="btn tiny act-edit">编辑</button>
        <button class="btn tiny act-toggle">${a.enabled ? "禁用" : "启用"}</button>
        <button class="btn tiny danger act-del">删除</button>
      </td>
    </tr>`
    )
    .join("");

  body.querySelectorAll("tr[data-asn]").forEach((tr) => {
    const asn = tr.dataset.asn;
    const item = items.find((x) => x.asn === asn);
    tr.querySelector(".act-edit").addEventListener("click", () => startEdit(item));
    tr.querySelector(".act-toggle").addEventListener("click", () => toggleAsn(item));
    tr.querySelector(".act-del").addEventListener("click", () => deleteAsn(item));
  });
}

async function loadCounts() {
  try {
    const data = await api("/asn?limit=1");
    const c = data.counts || {};
    $("asn-counts").textContent = `cloud ${c.cloud || 0} · residential ${c.residential || 0} · 合计 ${(c.cloud || 0) + (c.residential || 0)}`;
  } catch {
    /* ignore */
  }
}

async function loadAsn() {
  try {
    const data = await api("/asn?" + buildQuery().toString());
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
    toast("加载 ASN 失败: " + e.message, "err");
  }
}

function startEdit(item) {
  state.editing = item.asn;
  $("a-asn").value = item.asn;
  $("a-asn").disabled = true;
  $("a-category").value = item.category;
  $("a-org").value = item.org || "";
  $("a-country").value = item.country || "";
  $("a-note").value = item.note || "";
  $("a-enabled").value = item.enabled ? "1" : "0";
  $("add-asn").textContent = "更新 " + item.asn;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function resetForm() {
  state.editing = null;
  $("a-asn").value = "";
  $("a-asn").disabled = false;
  $("a-org").value = "";
  $("a-country").value = "";
  $("a-note").value = "";
  $("a-enabled").value = "1";
  $("add-asn").textContent = "保存";
}

async function saveAsn() {
  const asn = $("a-asn").value.trim();
  if (!asn) return toast("请填写 ASN", "err");
  const payload = {
    asn,
    category: $("a-category").value,
    org: $("a-org").value.trim() || null,
    country: $("a-country").value.trim().toUpperCase() || null,
    note: $("a-note").value.trim() || null,
    enabled: $("a-enabled").value === "1",
  };
  try {
    if (state.editing) {
      const { asn: _drop, ...update } = payload;
      await api(`/asn/${encodeURIComponent(state.editing)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(update),
      });
      toast("已更新 " + state.editing, "ok");
    } else {
      await api("/asn", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(payload),
      });
      toast("已保存 " + asn, "ok");
    }
    resetForm();
    await Promise.all([loadCounts(), loadAsn()]);
  } catch (e) {
    toast("保存失败: " + e.message, "err");
  }
}

async function toggleAsn(item) {
  try {
    await api(`/asn/${encodeURIComponent(item.asn)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ enabled: !item.enabled }),
    });
    toast(`${item.asn} 已${item.enabled ? "禁用" : "启用"}`, "ok");
    await loadAsn();
  } catch (e) {
    toast("操作失败: " + e.message, "err");
  }
}

async function deleteAsn(item) {
  if (!confirm(`确认删除 ${item.asn}？`)) return;
  try {
    await api(`/asn/${encodeURIComponent(item.asn)}`, { method: "DELETE", headers: authHeaders() });
    toast("已删除 " + item.asn, "ok");
    await Promise.all([loadCounts(), loadAsn()]);
  } catch (e) {
    toast("删除失败: " + e.message, "err");
  }
}

async function importAsn() {
  const text = $("i-text").value.trim();
  if (!text) return toast("请粘贴要导入的 ASN", "err");
  try {
    const r = await api("/asn/import", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ category: $("i-category").value, text }),
    });
    toast(`已导入 ${r.count} 条`, "ok");
    $("i-text").value = "";
    await Promise.all([loadCounts(), loadAsn()]);
  } catch (e) {
    toast("导入失败: " + e.message, "err");
  }
}

function bindSorting() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (state.sort === col) state.desc = !state.desc;
      else {
        state.sort = col;
        state.desc = false;
      }
      document.querySelectorAll("th[data-sort]").forEach((x) => x.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(state.desc ? "sort-desc" : "sort-asc");
      state.offset = 0;
      loadAsn();
    });
  });
}

function bindEvents() {
  $("add-asn").addEventListener("click", saveAsn);
  $("import-asn").addEventListener("click", importAsn);
  $("reload").addEventListener("click", () => Promise.all([loadCounts(), loadAsn()]));
  $("export-data").addEventListener("click", () => {
    const p = buildQuery();
    p.set("format", $("export-format").value);
    window.location.href = "/export/asn?" + p.toString();
  });
  $("apply-filters").addEventListener("click", () => {
    state.offset = 0;
    loadAsn();
  });
  $("reset-filters").addEventListener("click", () => {
    ["f-category", "f-enabled", "f-q"].forEach((id) => ($(id).value = ""));
    state.offset = 0;
    loadAsn();
  });
  $("f-limit").addEventListener("change", (e) => {
    state.limit = Number(e.target.value);
    state.offset = 0;
    loadAsn();
  });
  $("prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadAsn();
  });
  $("next-page").addEventListener("click", () => {
    if (state.offset + state.limit < state.total) {
      state.offset += state.limit;
      loadAsn();
    }
  });
}

async function init() {
  bindEvents();
  bindSorting();
  bindModal();
  state.limit = Number($("f-limit").value);
  await Promise.all([loadCounts(), loadAsn()]);
}

document.addEventListener("DOMContentLoaded", init);
