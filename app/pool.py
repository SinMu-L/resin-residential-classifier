"""高质量节点池漏斗编排（L1 ASN 初筛 → L2 AbuseIPDB → L4 复检淘汰）。

- 池按 **IP 去重**：同一 IP 多端口只保留一个代表节点（端口最小者）。
- 状态机：``probing``（L2 查询失败待重试）→ ``in_pool`` ⇄ ``probation`` → ``evicted``。
- L4 复检内联在整轮漏斗中：按 ``next_check_at`` 决定是否重新查询风险分，带滞回
  （连续 ``POOL_EVICT_FAILURES`` 次超标才淘汰）与冷却（``POOL_COOLDOWN`` 后可重入）。

风险检查器通过模块级 ``_risk_client`` 懒加载，测试可替换为 Fake。
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from . import asn_store, db, progress
from .config import settings
from .risk import make_client

logger = logging.getLogger("node-enricher.pool")

_run_lock = threading.Lock()
_risk_client = None
_prog = progress.get("pool")


def get_progress() -> dict:
    return _prog.snapshot()


def get_risk_client():
    """懒加载并缓存风险检查器（进程内复用）。"""
    global _risk_client
    if _risk_client is None:
        _risk_client = make_client(settings)
    return _risk_client


def schedule_pool_sync(force: bool = False) -> bool:
    """触发一次池漏斗（后台线程）。已在运行时返回 False。

    force=True 时忽略各项 ``next_check_at``，对所有已达 L1 的 IP 重新查询风险。
    """
    if not _run_lock.acquire(blocking=False):
        return False
    threading.Thread(target=_worker, args=(force,), daemon=True).start()
    return True


def _worker(force: bool = False) -> None:
    try:
        _do_pool_sync(force=force)
    except Exception as e:  # noqa: BLE001 - 单轮失败不应终止服务
        logger.warning("池漏斗执行异常: %s", e)
        _prog.finish(message="池漏斗失败", error=str(e))
    finally:
        _run_lock.release()


def rebuild_sync(force: bool = True) -> dict:
    """同步执行一轮漏斗（供测试/单次调用）。"""
    if not _run_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        stats = _do_pool_sync(force=force)
        return {"status": "ok", "stats": stats}
    finally:
        _run_lock.release()


def recheck_ip(ip: str) -> dict:
    """强制对单个 IP 重新走一遍 L1/L2 并更新池状态（忽略缓存与 next_check_at）。"""
    if not any(r["ip"] == ip for r in db.get_nodes_for_pool()):
        return {"status": "not_found"}
    if not _run_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        _do_pool_sync(force=True, only_ips={ip})
        return {"status": "ok"}
    finally:
        _run_lock.release()


def _i(v) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


def _do_pool_sync(force: bool = False, only_ips: set = None) -> dict:
    now = int(time.time())
    _prog.start(phase="加载名单")
    asn_store.load(force=True)

    nodes = db.get_nodes_for_pool()
    by_ip: dict[str, list] = {}
    for n in nodes:
        by_ip.setdefault(n["ip"], []).append(n)
    if only_ips is not None:
        by_ip = {ip: rows for ip, rows in by_ip.items() if ip in only_ips}

    existing = {r["ip"]: r for r in db.get_all_pool_nodes()}

    def rep(rows):
        return min(rows, key=lambda x: (x["port"], x["id"]))

    # -- L1：机房/ASN 分类，并决定哪些 IP 需要（重新）查询风险 ---------- #
    l1: dict[str, tuple] = {}          # ip -> (verdict, reason, asn)
    need_risk: list[str] = []
    for ip, rows in by_ip.items():
        r = rep(rows)
        asn = asn_store.normalize_asn(r["asn"])
        # 机房判定：富化（本地库 is_hosting 等）已标记为托管/机房 -> 直接拒
        if r["is_hosting"] == 1:
            l1[ip] = ("reject", "hosting", asn)
            continue
        cat = asn_store.classify(asn)
        if cat == "cloud":
            l1[ip] = ("reject", "cloud_asn", asn)
            continue
        if cat is None and settings.pool_l1_strict:
            l1[ip] = ("reject", "unknown_asn", asn)
            continue
        l1[ip] = ("pass", None, asn)
        ex = existing.get(ip)
        if ex is None or force:
            need_risk.append(ip)
        elif ex["state"] == "probing":
            # 上次风险查询失败（网络/配额/配置），不应被 next_check_at 锁住，总是重试
            need_risk.append(ip)
        elif ex["state"] == "evicted":
            if now - (ex["evicted_at"] or 0) >= settings.pool_cooldown:
                need_risk.append(ip)
        elif (ex["next_check_at"] or 0) <= now:
            need_risk.append(ip)

    risks = _resolve_risks(need_risk, force=force) if need_risk else {}
    _prog.update(phase="决策入池")

    stats = {
        "ips_seen": len(by_ip), "admitted": 0, "kept": 0, "probation": 0,
        "evicted": 0, "rejected_l1": 0, "rejected_cloud": 0, "rejected_unknown": 0,
        "rejected_hosting": 0, "error": 0, "removed": 0, "queried": len(need_risk),
    }

    # -- L1/L2 决策与状态迁移 ------------------------------------------ #
    for ip, rows in by_ip.items():
        r = rep(rows)
        verdict, reason, asn = l1[ip]
        ex = existing.get(ip)
        created = ex["created_at"] if ex else now

        if verdict == "reject":
            stats["rejected_l1"] += 1
            if reason == "cloud_asn":
                stats["rejected_cloud"] += 1
            elif reason == "unknown_asn":
                stats["rejected_unknown"] += 1
            elif reason == "hosting":
                stats["rejected_hosting"] += 1
            # 首次出现的拒绝 IP 也要落一条 evicted，保证“全部进池”可见
            if ex is None or ex["state"] != "evicted":
                db.upsert_pool_node(_row(
                    ip, r["hash"], len(rows), asn, "evicted",
                    abuse_score=ex["abuse_score"] if ex else None,
                    failures=ex["consecutive_failures"] if ex else 0,
                    reason=reason,
                    admitted_at=ex["admitted_at"] if ex else None,
                    last_checked_at=ex["last_checked_at"] if ex else now,
                    next_check_at=now + settings.pool_cooldown,
                    evicted_at=now, evict_reason=reason, created=created, updated=now,
                ))
                db.insert_pool_check(_check(ip, r["hash"], "L1", 0, None, reason, now))
                stats["evicted"] += 1
            continue

        if ip not in risks:
            # 复用既有状态，仅刷新代表节点/端口数
            if ex is not None:
                db.upsert_pool_node(_row(
                    ip, r["hash"], len(rows), asn, ex["state"],
                    abuse_score=ex["abuse_score"], failures=ex["consecutive_failures"],
                    reason=ex["reason"], admitted_at=ex["admitted_at"],
                    last_checked_at=ex["last_checked_at"], next_check_at=ex["next_check_at"],
                    evicted_at=ex["evicted_at"], evict_reason=ex["evict_reason"],
                    created=created, updated=now,
                ))
                stats["kept"] += 1
            continue

        risk = risks[ip]
        score = risk.get("abuse_confidence")
        err = risk.get("error")
        failures = ex["consecutive_failures"] if ex else 0

        if err:
            db.upsert_pool_node(_row(
                ip, r["hash"], len(rows), asn, "probing",
                abuse_score=score, failures=failures, reason=err,
                admitted_at=ex["admitted_at"] if ex else None,
                last_checked_at=now, next_check_at=now,
                evicted_at=None, evict_reason=None, created=created, updated=now,
            ))
            db.insert_pool_check(_check(ip, r["hash"], "L2", 0, score, err, now))
            stats["error"] += 1
        elif score is not None and score >= settings.pool_abuse_max_score:
            failures += 1
            if failures >= settings.pool_evict_failures:
                db.upsert_pool_node(_row(
                    ip, r["hash"], len(rows), asn, "evicted",
                    abuse_score=score, failures=failures, reason="high_abuse",
                    admitted_at=ex["admitted_at"] if ex else None,
                    last_checked_at=now, next_check_at=now + settings.pool_cooldown,
                    evicted_at=now, evict_reason="high_abuse", created=created, updated=now,
                ))
                db.insert_pool_check(_check(ip, r["hash"], "L2", 0, score, "high_abuse", now))
                stats["evicted"] += 1
            else:
                db.upsert_pool_node(_row(
                    ip, r["hash"], len(rows), asn, "probation",
                    abuse_score=score, failures=failures, reason="high_abuse",
                    admitted_at=ex["admitted_at"] if ex else None,
                    last_checked_at=now, next_check_at=now + settings.pool_recheck_probation,
                    evicted_at=None, evict_reason=None, created=created, updated=now,
                ))
                db.insert_pool_check(_check(ip, r["hash"], "L2", 0, score, "high_abuse", now))
                stats["probation"] += 1
        else:
            # 通过：in_pool（清空失败计数）
            db.upsert_pool_node(_row(
                ip, r["hash"], len(rows), asn, "in_pool",
                abuse_score=score, failures=0, reason=None,
                admitted_at=(ex["admitted_at"] if ex and ex["admitted_at"] else now),
                last_checked_at=now, next_check_at=now + settings.pool_recheck_in_pool,
                evicted_at=None, evict_reason=None, created=created, updated=now,
            ))
            db.insert_pool_check(_check(ip, r["hash"], "L2", 1, score, None, now))
            stats["admitted"] += 1

    # -- 源已移除的池内节点：淘汰（单 IP 重检时跳过） ------------------- #
    if only_ips is None:
        current = set(by_ip)
        for ip, ex in existing.items():
            if ip not in current and ex["state"] != "evicted":
                db.upsert_pool_node(_row(
                    ip, ex["representative_hash"], ex["port_count"], ex["asn"], "evicted",
                    abuse_score=ex["abuse_score"], failures=ex["consecutive_failures"],
                    reason="source_removed", admitted_at=ex["admitted_at"],
                    last_checked_at=ex["last_checked_at"],
                    next_check_at=now + settings.pool_cooldown,
                    evicted_at=now, evict_reason="source_removed",
                    created=ex["created_at"], updated=now,
                ))
                db.insert_pool_check(_check(ip, ex["representative_hash"], "L1", 0, None, "source_removed", now))
                stats["removed"] += 1

    logger.info(
        "池漏斗完成: ips=%s 查询=%s in_pool+=%s 淘汰=%s L1拒=%s(云=%s,机房=%s,未知=%s) 错误=%s 移除=%s 耗时=%ss",
        stats["ips_seen"], stats["queried"], stats["admitted"], stats["evicted"],
        stats["rejected_l1"], stats["rejected_cloud"], stats["rejected_hosting"],
        stats["rejected_unknown"], stats["error"], stats["removed"], int(time.time()) - now,
    )
    _prog.finish(
        message=f"IP {stats['ips_seen']} · 查询 {stats['queried']} · 入池 {stats['admitted']} · "
                f"淘汰 {stats['evicted']} · L1拒 {stats['rejected_l1']}"
                f"（云 {stats['rejected_cloud']}/机房 {stats['rejected_hosting']}/未知 {stats['rejected_unknown']}）"
    )
    return stats


def _row(ip, rep_hash, port_count, asn, state, *, abuse_score, failures, reason,
         admitted_at, last_checked_at, next_check_at, evicted_at, evict_reason,
         created, updated) -> dict:
    return {
        "ip": ip,
        "representative_hash": rep_hash,
        "port_count": port_count,
        "state": state,
        "asn": asn,
        "abuse_score": abuse_score,
        "pool_score": _purity(abuse_score, state),
        "consecutive_failures": failures,
        "reason": reason,
        "admitted_at": admitted_at,
        "last_checked_at": last_checked_at,
        "next_check_at": next_check_at,
        "evicted_at": evicted_at,
        "evict_reason": evict_reason,
        "created_at": created,
        "updated_at": updated,
    }


def _purity(abuse_score, state) -> Optional[int]:
    """由风险分派生的纯净度分（0-100），未知为 None。"""
    if state == "evicted":
        return 0
    if abuse_score is None:
        return None
    return max(0, min(100, 100 - int(abuse_score)))


def _check(ip, rep_hash, stage, passed, score, reason, now) -> dict:
    return {
        "ip": ip,
        "representative_hash": rep_hash,
        "stage": stage,
        "passed": 1 if passed else 0,
        "score": score,
        "reason": reason,
        "raw": None,
        "checked_at": now,
    }


def _risk_from_row(rec) -> dict:
    return {
        "ok": True,
        "abuse_confidence": rec["abuse_confidence"],
        "total_reports": rec["total_reports"],
        "distinct_users": rec["distinct_users"],
        "last_reported_at": rec["last_reported_at"],
        "is_whitelisted": (bool(rec["is_whitelisted"]) if rec["is_whitelisted"] is not None else None),
        "usage_type": rec["usage_type"],
        "isp": rec["isp"],
        "country": rec["country"],
        "raw": json.loads(rec["raw"]) if rec["raw"] else None,
        "error": None,
    }


def _err_result(message: str) -> dict:
    return {
        "ok": False, "abuse_confidence": None, "total_reports": None,
        "distinct_users": None, "last_reported_at": None, "is_whitelisted": None,
        "usage_type": None, "isp": None, "country": None, "raw": None,
        "error": message,
    }


def _resolve_risks(ips: list[str], force: bool = False) -> dict:
    """带 TTL 缓存的风险查询：命中未过期缓存则复用，否则调用检查器并落库。

    force=True 时忽略缓存，对所有 IP 重新查询（用于手动强制重建）。
    未命中的 IP 通过线程池并发查询（``ABUSEIPDB_CONCURRENCY``），显著缩短整轮耗时。
    """
    now = int(time.time())
    ttl = settings.abuse_cache_ttl
    client = get_risk_client()
    out: dict[str, dict] = {}
    to_fetch: list[str] = []

    for ip in ips:
        rec = db.get_ip_risk(ip)
        if not force and rec is not None and not rec["error"] and (rec["checked_at"] + ttl) > now:
            out[ip] = _risk_from_row(rec)
        else:
            to_fetch.append(ip)

    if not to_fetch:
        return out

    workers = max(1, int(getattr(settings, "abuseipdb_concurrency", 8)))
    _prog.update(phase="L2 风险查询", total=len(to_fetch), done=0)
    logger.info("风险查询开始: %d 个 IP，并发=%d", len(to_fetch), workers)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(client.check, ip): ip for ip in to_fetch}
        for fut in as_completed(future_map):
            ip = future_map[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001 - 单 IP 异常不影响整轮
                r = _err_result(f"检查器异常: {e}")
            db.upsert_ip_risk({
                "ip": ip,
                "source": getattr(client, "name", "risk"),
                "abuse_confidence": r.get("abuse_confidence"),
                "total_reports": r.get("total_reports"),
                "distinct_users": r.get("distinct_users"),
                "last_reported_at": r.get("last_reported_at"),
                "is_whitelisted": _i(r.get("is_whitelisted")),
                "usage_type": r.get("usage_type"),
                "isp": r.get("isp"),
                "country": r.get("country"),
                "raw": json.dumps(r.get("raw"), ensure_ascii=False) if r.get("raw") is not None else None,
                "checked_at": now,
                "expires_at": now + ttl,
                "error": r.get("error"),
            })
            out[ip] = r
            _prog.update(inc=1)
    return out
