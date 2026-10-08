"""Resin 订阅内容生成（只读发布，不写 Resin）。

把高质量节点池（默认 ``state='in_pool'``）渲染成 Resin 可解析的 sing-box
``{"outbounds": [...]}`` 订阅内容，由 Resin 作为**远程订阅**主动拉取。

设计要点：
- 直接回填节点原始 ``raw_options``，保证与 Resin 侧的 ``HashFromRawOptions``
  一致：命中已有节点、只加引用与标签、共享健康状态，不产生重复节点。
- ``tag`` 不参与哈希，这里统一覆盖为稳定标签，便于 Resin 中按标签过滤。
- 空池返回 ``{"outbounds": []}``（Resin 订阅更新成功，但平台无可路由节点）。
"""
import hashlib
import json
import logging
from typing import Optional

from . import db
from .config import settings

logger = logging.getLogger("node-enricher.resin_feed")


def _render_opts(row) -> Optional[dict]:
    """解析单个池代表节点的原始配置，并覆盖为稳定 tag。"""
    try:
        opts = json.loads(row["raw_options_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(opts, dict) or not opts.get("type"):
        return None
    opts = dict(opts)
    opts["tag"] = f"pool-{row['ip']}"
    return opts


def build_subscription(states: Optional[list[str]] = None) -> dict:
    """生成 sing-box 订阅字典 ``{"outbounds": [...]}``（按 hash 去重）。"""
    states = states if states is not None else settings.resin_feed_states
    rows = db.get_feed_nodes(states)
    outbounds = []
    seen: set[str] = set()
    skipped = 0
    for r in rows:
        h = r["hash"]
        if h in seen:
            continue
        seen.add(h)
        opts = _render_opts(r)
        if opts is None:
            skipped += 1
            continue
        outbounds.append(opts)
    if skipped:
        logger.warning("订阅生成跳过 %d 个无法解析的节点配置", skipped)
    return {"outbounds": outbounds}


def render(content: dict) -> str:
    return json.dumps(content, ensure_ascii=False)


def build_feed(states: Optional[list[str]] = None) -> dict:
    """返回可供端点直接使用的订阅结果。"""
    content = build_subscription(states)
    text = render(content)
    return {
        "content": content,
        "text": text,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "node_count": len(content["outbounds"]),
    }


def contains_credentials(content: dict) -> bool:
    """检测订阅内容是否疑似包含代理凭据（用于公开暴露时的安全告警）。"""
    for ob in content.get("outbounds", []):
        if not isinstance(ob, dict):
            continue
        if ob.get("username") or ob.get("password") or ob.get("uuid"):
            return True
        if ob.get("users"):  # sing-box 多用户形式
            return True
    return False
