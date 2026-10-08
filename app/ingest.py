"""Ingest 同步管线（FR-1 ~ FR-4 编排）。

流程：读取数据源 -> 过滤 http/https -> 按 IP 去重批量富化（带缓存与回退链）
      -> Upsert nodes / ip_enrichments -> 记录 ingest_runs 统计。
"""
import json
import threading
import time
from typing import Optional

from .config import settings


def _i(v) -> Optional[int]:
    """bool/None -> 1/0/None（SQLite 整型存储）。"""
    if v is None:
        return None
    return 1 if v else 0
from . import db, progress
from .source import read_source_nodes
from .enricher import build_chain
from .enricher.ipinfo import extract_fields

_run_lock = threading.Lock()
_enricher_chain = None


def get_enricher_chain():
    """懒加载并缓存富化器回退链（单次构建，进程内复用）。"""
    global _enricher_chain
    if _enricher_chain is None:
        _enricher_chain = build_chain(settings)
    return _enricher_chain


def source_reachable() -> bool:
    from .source import source_reachable as _sr
    return _sr()


def schedule_ingest() -> Optional[int]:
    """触发一次同步（后台线程异步执行）。已在运行时返回 None。"""
    if not _run_lock.acquire(blocking=False):
        return None
    run_id = db.start_ingest_run()
    t = threading.Thread(target=_worker, args=(run_id,), daemon=True)
    t.start()
    return run_id


def _worker(run_id: int) -> None:
    try:
        _do_ingest(run_id)
    except Exception as e:  # noqa: BLE001 - 单轮失败不应终止服务
        progress.get("ingest").finish(message="同步失败", error=str(e))
    finally:
        _run_lock.release()


def _do_ingest(run_id: int) -> None:
    started = int(time.time())
    prog = progress.get("ingest")
    prog.start(phase="读取数据源")
    stats = {
        "nodes_seen": 0, "nodes_new": 0, "nodes_updated": 0,
        "nodes_enriched": 0, "nodes_failed": 0, "status": "ok",
    }

    candidates, err = read_source_nodes()
    if err:
        prog.finish(message="数据源不可达", error=err)
        stats["status"] = "error"
        stats["finished_at"] = int(time.time())
        db.finish_ingest_run(run_id, stats)
        return

    stats["nodes_seen"] = len(candidates)

    # 解析既有 hash，用于区分新增/更新
    existing = db.get_existing_hashes([c.hash for c in candidates])

    # 按 IP 去重富化
    unique_ips = []
    seen = set()
    for c in candidates:
        if c.ip not in seen:
            seen.add(c.ip)
            unique_ips.append(c.ip)

    prog.update(phase="富化", total=len(unique_ips), done=0)
    enrich_status = _resolve_enrichments(
        unique_ips, on_progress=lambda done: prog.update(done=done)
    )

    prog.update(phase="落库", total=len(candidates), done=0)
    now = int(time.time())
    total_nodes = len(candidates)
    for i, c in enumerate(candidates):
        st = enrich_status.get(c.ip)
        ok = bool(st and st.get("ok"))
        fields = st.get("fields") if ok else None

        node = {
            "hash": c.hash,
            "ip": c.ip,
            "port": c.port,
            "protocol": c.protocol,
            "raw_type": c.raw_type,
            "tag": c.tag,
            "is_residential": fields["is_residential"] if fields else None,
            "enriched": 1 if ok else 0,
            "enrich_source": st.get("source") if ok else None,
            "enrich_plan": st.get("plan") if ok else None,
            "asn": fields["asn"] if fields else None,
            "as_name": fields["as_name"] if fields else None,
            "as_type": fields["as_type"] if fields else None,
            "is_hosting": _i(fields["is_hosting"]) if fields else None,
            "is_anonymous": _i(fields["is_anonymous"]) if fields else None,
            "country_code": fields["country_code"] if fields else None,
            "city": fields["city"] if fields else None,
            "region": fields["region"] if fields else None,
            "latitude": fields["latitude"] if fields else None,
            "longitude": fields["longitude"] if fields else None,
            "raw_options_json": json.dumps(c.raw_options, ensure_ascii=False),
            "latency_ms": c.latency_ms,
            "latencies_json": json.dumps(c.latencies, ensure_ascii=False) if c.latencies else None,
            "latency_updated_at": c.latency_updated_at,
            "failure_count": c.failure_count,
            "circuit_open_since": c.circuit_open_since,
            "egress_ip": c.egress_ip,
            "egress_region": c.egress_region,
            "egress_updated_at": c.egress_updated_at,
            "last_enriched_at": now if ok else None,
            "created_at": c.created_at,
            "updated_at": now,
        }
        db.upsert_node(node)
        if c.hash in existing:
            stats["nodes_updated"] += 1
        else:
            stats["nodes_new"] += 1
        if ok:
            stats["nodes_enriched"] += 1
        if (i + 1) % 25 == 0 or (i + 1) == total_nodes:
            prog.update(done=i + 1)

    stats["nodes_failed"] = sum(1 for ip in unique_ips if not enrich_status.get(ip, {}).get("ok"))
    # 清理已从数据源移除的节点，避免陈旧 hash 拖累延迟等运行时指标的关联
    stats["nodes_pruned"] = db.delete_nodes_not_in([c.hash for c in candidates])
    stats["finished_at"] = int(time.time())
    prog.finish(
        message=f"新增 {stats['nodes_new']} / 更新 {stats['nodes_updated']} / "
                f"富化 {stats['nodes_enriched']} / 失败 {stats['nodes_failed']} / "
                f"清理 {stats['nodes_pruned']}"
    )
    db.finish_ingest_run(run_id, stats)

    # 同步完成后自动刷新节点池，保证代表节点/延迟等指标与最新数据一致
    try:
        if settings.pool_enabled:
            from .pool import schedule_pool_sync
            schedule_pool_sync()
    except Exception:  # noqa: BLE001 - 池刷新失败不影响同步结果
        pass


def _resolve_enrichments(ips: list[str], on_progress=None) -> dict:
    """返回 {ip: {ok, source, plan, fields, raw, error}}。

    - 优先复用应用库中未过期且无错误的富化结果（缓存，FR-3.5）。
    - 否则调用回退链（默认 ipinfo）批量富化并落库。
    - on_progress(done) 用于上报进度（done = 已得出结果的 IP 数）。
    """
    now = int(time.time())
    ttl = settings.enrich_cache_ttl
    result: dict = {}
    to_fetch: list[str] = []

    for ip in ips:
        rec = db.get_enrichment_by_ip(ip)
        if rec and not rec["error"] and (rec["checked_at"] + ttl) > now:
            raw = _safe_json(rec["raw"])
            fields = extract_fields(raw) if raw is not None else None
            result[ip] = {
                "ok": fields is not None,
                "source": rec["source"],
                "plan": rec["plan"],
                "fields": fields,
                "raw": raw,
                "error": None,
            }
        else:
            to_fetch.append(ip)

    if on_progress and result:
        on_progress(len(result))

    if to_fetch:
        chain = get_enricher_chain()
        fetched = _fetch_via_chain(chain, to_fetch)
        for ip in to_fetch:
            e = fetched.get(ip)
            if e is None or e.error:
                result[ip] = {"ok": False, "source": (e.source if e else None),
                              "plan": (e.plan if e else None), "fields": None, "raw": None,
                              "error": (e.error if e else "无可用富化器")}
            else:
                fields = extract_fields(e.raw)
                result[ip] = {
                    "ok": fields is not None,
                    "source": e.source,
                    "plan": e.plan,
                    "fields": fields,
                    "raw": e.raw,
                    "error": None,
                }
                # 落库（全量 raw + 派生字段）
                db.upsert_enrichment({
                    "ip": ip,
                    "source": e.source,
                    "plan": e.plan,
                    "raw": json.dumps(e.raw, ensure_ascii=False),
                    "is_residential": fields["is_residential"] if fields else None,
                    "is_hosting": _i(fields["is_hosting"]) if fields else None,
                    "as_type": fields["as_type"] if fields else None,
                    "checked_at": e.enriched_at,
                    "expires_at": e.enriched_at + ttl,
                    "error": None if fields else "无法解析返回",
                })
            if on_progress:
                on_progress(len(result))

    return result


def _fetch_via_chain(chain, ips: list[str]) -> dict:
    """依次尝试回退链中的富化器（FR-3.4）。"""
    from .enricher.base import IpEnrichment
    out: dict[str, IpEnrichment] = {}
    remaining = list(ips)
    for enricher in chain:
        if not remaining:
            break
        try:
            batch = enricher.enrich_batch(remaining)
        except Exception:  # noqa: BLE001 - 单个源异常不应中断整轮
            batch = {}
        for ip in remaining:
            e = batch.get(ip)
            if e is not None and not e.error:
                out[ip] = e
        remaining = [ip for ip in remaining if ip not in out]
    # 未成功的补一个占位（带 error）
    for ip in remaining:
        out[ip] = IpEnrichment(ip, chain[0].name if chain else "none", "unknown", {}, int(time.time()),
                               error="所有富化器均失败")
    return out


def _safe_json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None
