"use strict";

// 后台任务进度条：轮询 /tasks，渲染到指定容器。
const TaskProgress = (function () {
  const watchers = {};

  async function fetchTasks() {
    const r = await fetch("/tasks");
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function render(el, task) {
    if (!el) return;
    if (!task || (!task.running && !task.finished_at)) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    const pct = task.running ? Math.max(2, task.percent || 0) : 100;
    const cls = task.error ? "err" : task.running ? "running" : "done";
    const label = task.error ? "错误：" + task.error : task.running ? task.phase || "运行中" : "完成";
    const detail = task.message ? " · " + task.message : "";
    const count = task.total ? ` (${task.done || 0}/${task.total})` : "";
    el.innerHTML =
      `<div class="pb-head"><span class="pb-label">${esc(label)}${esc(detail)}${esc(count)}</span>` +
      `<span class="pb-pct">${task.running ? pct + "%" : "100%"}</span></div>` +
      `<div class="pb-track"><div class="pb-fill ${cls}" style="width:${pct}%"></div></div>`;
  }

  // 开始观察某个任务并渲染到 containerId；任务完成后停留 3 秒再隐藏。
  function watch(name, containerId, intervalMs) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (watchers[name]) clearTimeout(watchers[name]);
    const iv = intervalMs || 1000;

    async function tick() {
      let task = null;
      try {
        task = (await fetchTasks())[name] || null;
      } catch (e) {
        /* 网络抖动，稍后重试 */
      }
      render(el, task);
      if (task && task.running) {
        watchers[name] = setTimeout(tick, iv);
      } else if (task && task.finished_at) {
        watchers[name] = setTimeout(() => {
          el.hidden = true;
          delete watchers[name];
        }, 3000);
      } else {
        delete watchers[name];
      }
    }
    tick();
  }

  return { watch };
})();
