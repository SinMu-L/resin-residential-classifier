"use strict";

// 池/ASN 页面共用的轻量工具（index.html 用 app.js，不加载本文件）
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

function fmtBool(v) {
  if (v === null || v === undefined) return "-";
  return v ? "true" : "false";
}

function latTag(ms) {
  if (ms === null || ms === undefined) return '<span class="muted">-</span>';
  const cls = ms < 100 ? "fast" : ms < 300 ? "mid" : "slow";
  return `<span class="lat ${cls}">${ms} ms</span>`;
}

function token() {
  const el = $("refresh-token");
  return el ? el.value.trim() : "";
}

function authHeaders() {
  const t = token();
  return t ? { Authorization: "Bearer " + t } : {};
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

let _toastTimer = null;
function toast(message, kind) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (kind ? " " + kind : "");
  el.hidden = false;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => (el.hidden = true), 2800);
}

function bindModal() {
  const modal = $("modal");
  if (!modal) return;
  $("modal-close").addEventListener("click", () => (modal.hidden = true));
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") modal.hidden = true;
  });
}

function openModal(title, html) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = html;
  $("modal").hidden = false;
}

const STATE_LABEL = {
  in_pool: "已入池",
  probation: "观察期",
  probing: "探测中",
  evicted: "已淘汰",
};

function stateTag(v) {
  return `<span class="tag st-${escapeHtml(v)}">${escapeHtml(STATE_LABEL[v] || v || "-")}</span>`;
}
