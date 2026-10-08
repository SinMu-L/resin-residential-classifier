"""REST API 路由（PRD 第 5 节）。"""
import csv
import io
import ipaddress
import json
import random
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import asn_store, db
from .config import settings
from .models import (
    build_asn_response,
    build_enrichment_response,
    build_node_response,
    build_pool_check_response,
    build_pool_response,
    error_envelope,
)
from .recent_buffer import recent_buffer

router = APIRouter()

RES_MAP = {"residential": 1, "datacenter": 0, "residential_proxy": 2, "business": 3}
SORT_WHITELIST = {
    "as_type", "last_enriched_at", "country_code", "is_residential",
    "protocol", "created_at", "updated_at", "ip", "port", "id",
    "latency_ms", "failure_count",
}

POOL_STATES = ("probing", "in_pool", "probation", "evicted")
POOL_SORT_WHITELIST = {
    "ip", "state", "asn", "abuse_score", "pool_score",
    "admitted_at", "last_checked_at", "next_check_at", "created_at", "updated_at",
}
ASN_SORT_WHITELIST = {"asn", "category", "org", "country", "created_at", "updated_at"}


class AsnCreate(BaseModel):
    asn: str
    category: str
    org: Optional[str] = None
    country: Optional[str] = None
    note: Optional[str] = None
    enabled: bool = True


class AsnUpdate(BaseModel):
    category: Optional[str] = None
    org: Optional[str] = None
    country: Optional[str] = None
    note: Optional[str] = None
    enabled: Optional[bool] = None


class AsnImport(BaseModel):
    category: str
    text: str
    source: str = "import"



def _require_auth(authorization: Optional[str] = Header(default=None)):
    if not settings.auth_enabled:
        return
    if authorization != f"Bearer {settings.refresh_token}":
        raise HTTPException(status_code=401, detail=error_envelope("UNAUTHORIZED", "需要提供正确的 Bearer 令牌"))


def _build_where(
    protocol: Optional[str] = None,
    residential: Optional[str] = None,
    as_type: Optional[str] = None,
    country: Optional[str] = None,
    enriched: Optional[bool] = None,
    exclude_ips: Optional[list] = None,
    max_latency_ms: Optional[float] = None,
) -> tuple[str, list]:
    clauses = []
    params: list = []
    if protocol:
        clauses.append("n.protocol = ?")
        params.append(protocol)
    if residential:
        if residential == "unknown":
            clauses.append("n.is_residential IS NULL")
        elif residential in RES_MAP:
            clauses.append("n.is_residential = ?")
            params.append(RES_MAP[residential])
        else:
            raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "residential 取值非法"))
    if as_type:
        clauses.append("n.as_type = ?")
        params.append(as_type)
    if country:
        clauses.append("n.country_code = ?")
        params.append(country)
    if enriched is not None:
        clauses.append("n.enriched = ?")
        params.append(1 if enriched else 0)
    if max_latency_ms is not None:
        clauses.append("n.latency_ms IS NOT NULL AND n.latency_ms <= ?")
        params.append(max_latency_ms)
    if exclude_ips:
        ph = ",".join("?" for _ in exclude_ips)
        clauses.append(f"n.ip NOT IN ({ph})")
        params.extend(exclude_ips)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _parse_sort(sort: Optional[str]) -> str:
    if not sort:
        return "n.id ASC"
    desc = sort.startswith("-")
    col = sort[1:] if desc else sort
    if col not in SORT_WHITELIST:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"不允许的排序字段: {col}"))
    safe_col = f"n.{col}"
    return f"{safe_col} DESC" if desc else f"{safe_col} ASC"


@router.get("/health")
def health():
    src_ok = False
    try:
        from .source import source_reachable
        src_ok = source_reachable()
    except Exception:  # noqa: BLE001
        src_ok = False
    app_ok = True
    nodes_count = 0
    try:
        nodes_count = db.count_nodes()
    except Exception:  # noqa: BLE001
        app_ok = False
    last = db.get_last_ingest_run()
    try:
        from .mmdb import mmdb_status
        mmdb = mmdb_status()
    except Exception:  # noqa: BLE001
        mmdb = None
    status = "ok" if (src_ok and app_ok) else "degraded"
    return {
        "status": status,
        "version": settings.version,
        "source_db": {"reachable": src_ok, "path": settings.source_db_path},
        "app_db": {"reachable": app_ok, "nodes": nodes_count},
        "mmdb": mmdb,
        "last_sync_at": last["finished_at"] if last else None,
        "last_sync_status": last["status"] if last else None,
    }


@router.get("/node/{ip}/{port}")
def get_node(ip: str, port: int):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"非法 IP: {ip}"))
    if not (1 <= port <= 65535):
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "端口需在 1-65535 之间"))
    row = db.get_node_by_ip_port(ip, port)
    if row is None:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"节点 {ip}:{port} 不存在"))
    return build_node_response(row)


@router.get("/nodes")
def list_nodes(
    protocol: Optional[str] = Query(default=None),
    residential: Optional[str] = Query(default=None),
    as_type: Optional[str] = Query(default=None),
    country: Optional[str] = Query(default=None),
    enriched: Optional[bool] = Query(default=None),
    max_latency_ms: Optional[float] = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort: Optional[str] = Query(default=None),
):
    where, params = _build_where(protocol, residential, as_type, country, enriched,
                                 max_latency_ms=max_latency_ms)
    order_by = _parse_sort(sort)
    total, rows = db.get_nodes_page(where, params, limit, offset, order_by)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [build_node_response(r) for r in rows],
    }


@router.get("/nodes/random")
def random_node(
    protocol: Optional[str] = Query(default=None),
    residential: Optional[str] = Query(default=None),
    as_type: Optional[str] = Query(default=None),
    country: Optional[str] = Query(default=None),
    enriched: Optional[bool] = Query(default=None),
    max_latency_ms: Optional[float] = Query(default=None, ge=0),
    distinct_ip: bool = Query(default=False),
    avoid_recent: bool = Query(default=False),
):
    exclude = recent_buffer.keys() if avoid_recent else None
    if exclude == []:
        exclude = None

    where, params = _build_where(protocol, residential, as_type, country, enriched,
                                 exclude_ips=exclude, max_latency_ms=max_latency_ms)
    candidates = db.get_candidate_rows(where, params)
    if not candidates:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", "无满足条件的可用节点"))

    if distinct_ip:
        seen = {}
        for r in candidates:
            key = (r["ip"], r["port"])
            if key not in seen:
                seen[key] = r
        pool = list(seen.values())
    else:
        pool = list(candidates)

    chosen = random.choice(pool)
    row = db.get_node_by_id(chosen["id"])
    if row is None:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", "无满足条件的可用节点"))

    if avoid_recent:
        recent_buffer.add(row["ip"])

    return build_node_response(row)


@router.get("/enrichment/{ip}")
def get_enrichment(ip: str):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"非法 IP: {ip}"))
    row = db.get_enrichment_by_ip(ip)
    if row is None or row["error"]:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"IP {ip} 未富化或富化失败"))
    return build_enrichment_response(row)


@router.get("/stats")
def stats():
    conn = db.get_app_conn()
    try:
        rows = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN is_residential=1 THEN 1 ELSE 0 END) AS residential,
                SUM(CASE WHEN is_residential=0 THEN 1 ELSE 0 END) AS datacenter,
                SUM(CASE WHEN is_residential=2 THEN 1 ELSE 0 END) AS residential_proxy,
                SUM(CASE WHEN is_residential=3 THEN 1 ELSE 0 END) AS business,
                SUM(CASE WHEN is_residential IS NULL THEN 1 ELSE 0 END) AS unknown_res,
                SUM(CASE WHEN enriched=1 THEN 1 ELSE 0 END) AS enriched,
                SUM(CASE WHEN enriched=0 THEN 1 ELSE 0 END) AS not_enriched,
                SUM(CASE WHEN as_type='hosting' THEN 1 ELSE 0 END) AS as_hosting,
                SUM(CASE WHEN as_type='isp' THEN 1 ELSE 0 END) AS as_isp,
                SUM(CASE WHEN as_type='business' THEN 1 ELSE 0 END) AS as_business,
                SUM(CASE WHEN as_type IS NULL OR as_type NOT IN ('hosting','isp','business') THEN 1 ELSE 0 END) AS as_other
            FROM nodes
            """
        ).fetchone()
        proto_rows = conn.execute(
            "SELECT protocol AS p, COUNT(*) AS c FROM nodes GROUP BY protocol ORDER BY c DESC"
        ).fetchall()
    finally:
        conn.close()

    last = db.get_last_ingest_run()
    return {
        "nodes_total": rows["total"],
        "by_protocol": {r["p"]: r["c"] for r in proto_rows},
        "by_residential": {
            "residential": rows["residential"] or 0,
            "datacenter": rows["datacenter"] or 0,
            "residential_proxy": rows["residential_proxy"] or 0,
            "business": rows["business"] or 0,
            "unknown": rows["unknown_res"] or 0,
        },
        "enriched": rows["enriched"] or 0,
        "not_enriched": rows["not_enriched"] or 0,
        "by_as_type": {
            "hosting": rows["as_hosting"] or 0,
            "isp": rows["as_isp"] or 0,
            "business": rows["as_business"] or 0,
            "unknown": rows["as_other"] or 0,
        },
        "last_sync_at": last["finished_at"] if last else None,
        "last_sync_status": last["status"] if last else None,
    }


@router.post("/refresh", dependencies=[Depends(_require_auth)])
def refresh():
    from .ingest import schedule_ingest
    run_id = schedule_ingest()
    if run_id is None:
        return JSONResponse(
            status_code=202,
            content={"status": "skipped", "message": "已有同步任务在运行中"},
        )
    return JSONResponse(status_code=202, content={"status": "accepted", "run_id": run_id})


# --------------------------------------------------------------------------- #
# ASN 名单管理（L1）                                                           #
# --------------------------------------------------------------------------- #

def _parse_sort_generic(sort: Optional[str], whitelist: set, prefix: str,
                        default: str) -> str:
    if not sort:
        return default
    desc = sort.startswith("-")
    col = sort[1:] if desc else sort
    if col not in whitelist:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"不允许的排序字段: {col}"))
    return f"{prefix}{col} DESC" if desc else f"{prefix}{col} ASC"


@router.get("/asn")
def list_asn(
    category: Optional[str] = Query(default=None),
    enabled: Optional[bool] = Query(default=None),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort: Optional[str] = Query(default=None),
):
    if category and not asn_store.is_valid_category(category):
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "category 取值非法"))
    order_by = _parse_sort_generic(sort, ASN_SORT_WHITELIST, "", "asn ASC")
    total, rows = db.list_asn(category, enabled, q, limit, offset, order_by)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts": asn_store.counts(),
        "items": [build_asn_response(r) for r in rows],
    }


@router.post("/asn", dependencies=[Depends(_require_auth)])
def create_asn(payload: AsnCreate):
    try:
        asn = asn_store.upsert(
            payload.asn, payload.category, org=payload.org, country=payload.country,
            note=payload.note, source="manual", enabled=payload.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", str(e)))
    return JSONResponse(status_code=201, content=build_asn_response(db.get_asn(asn)))


@router.put("/asn/{asn}", dependencies=[Depends(_require_auth)])
def update_asn(asn: str, payload: AsnUpdate):
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "无可更新字段"))
    try:
        ok = asn_store.update(asn, fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", str(e)))
    if not ok:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"ASN {asn} 不存在"))
    return build_asn_response(db.get_asn(asn_store.normalize_asn(asn)))


@router.delete("/asn/{asn}", dependencies=[Depends(_require_auth)])
def delete_asn(asn: str):
    try:
        ok = asn_store.delete(asn)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", str(e)))
    if not ok:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"ASN {asn} 不存在"))
    return {"status": "deleted", "asn": asn_store.normalize_asn(asn)}


@router.post("/asn/import", dependencies=[Depends(_require_auth)])
def import_asn(payload: AsnImport):
    try:
        n = asn_store.import_lines(payload.category, payload.text, source=payload.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", str(e)))
    return {"status": "imported", "count": n, "category": payload.category}


# --------------------------------------------------------------------------- #
# 高质量节点池（L1 + L2 + L4）                                                 #
# --------------------------------------------------------------------------- #

def _build_pool_where(
    state: Optional[str] = None,
    protocol: Optional[str] = None,
    max_abuse_score: Optional[int] = None,
    min_pool_score: Optional[int] = None,
    max_latency_ms: Optional[float] = None,
    q: Optional[str] = None,
) -> tuple[str, list]:
    clauses, params = [], []
    if state:
        if state not in POOL_STATES:
            raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "state 取值非法"))
        clauses.append("p.state = ?")
        params.append(state)
    if protocol:
        clauses.append("n.protocol = ?")
        params.append(protocol)
    if max_abuse_score is not None:
        clauses.append("p.abuse_score IS NOT NULL AND p.abuse_score <= ?")
        params.append(max_abuse_score)
    if min_pool_score is not None:
        clauses.append("p.pool_score IS NOT NULL AND p.pool_score >= ?")
        params.append(min_pool_score)
    if max_latency_ms is not None:
        clauses.append("n.latency_ms IS NOT NULL AND n.latency_ms <= ?")
        params.append(max_latency_ms)
    if q:
        clauses.append("(p.ip LIKE ? OR p.asn LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _parse_pool_sort(sort: Optional[str]) -> str:
    if not sort:
        return "p.pool_score DESC, p.ip ASC"
    desc = sort.startswith("-")
    col = sort[1:] if desc else sort
    if col not in POOL_SORT_WHITELIST and col != "latency_ms":
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"不允许的排序字段: {col}"))
    expr = "n.latency_ms" if col == "latency_ms" else f"p.{col}"
    return f"{expr} DESC" if desc else f"{expr} ASC"


@router.get("/pool")
def list_pool(
    state: Optional[str] = Query(default=None),
    protocol: Optional[str] = Query(default=None),
    max_abuse_score: Optional[int] = Query(default=None, ge=0, le=100),
    min_pool_score: Optional[int] = Query(default=None, ge=0, le=100),
    max_latency_ms: Optional[float] = Query(default=None, ge=0),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort: Optional[str] = Query(default=None),
):
    where, params = _build_pool_where(state, protocol, max_abuse_score, min_pool_score,
                                      max_latency_ms, q)
    order_by = _parse_pool_sort(sort)
    total, rows = db.get_pool_page(where, params, limit, offset, order_by)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [build_pool_response(r) for r in rows],
    }


@router.get("/pool/random")
def random_pool_node(
    protocol: Optional[str] = Query(default=None),
    max_abuse_score: Optional[int] = Query(default=None, ge=0, le=100),
    max_latency_ms: Optional[float] = Query(default=None, ge=0),
    avoid_recent: bool = Query(default=False),
):
    exclude = recent_buffer.keys() if avoid_recent else None
    if exclude == []:
        exclude = None
    where, params = _build_pool_where(state="in_pool", protocol=protocol,
                                      max_abuse_score=max_abuse_score,
                                      max_latency_ms=max_latency_ms)
    if exclude:
        ph = ",".join("?" for _ in exclude)
        where += (" AND " if where else "WHERE ") + f"p.ip NOT IN ({ph})"
        params.extend(exclude)
    candidates = db.get_pool_candidates(where, params)
    if not candidates:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", "池内无满足条件的可用节点"))
    chosen = random.choice(candidates)
    row = db.get_pool_node(chosen["ip"])
    if avoid_recent:
        recent_buffer.add(row["ip"])
    return build_pool_response(row)


@router.get("/pool/stats")
def pool_stats():
    from . import pool as pool_mod
    conn = db.get_app_conn()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN p.state='in_pool' THEN 1 ELSE 0 END) AS in_pool,
                SUM(CASE WHEN p.state='probation' THEN 1 ELSE 0 END) AS probation,
                SUM(CASE WHEN p.state='probing' THEN 1 ELSE 0 END) AS probing,
                SUM(CASE WHEN p.state='evicted' THEN 1 ELSE 0 END) AS evicted,
                AVG(CASE WHEN p.abuse_score IS NOT NULL THEN p.abuse_score END) AS avg_abuse,
                AVG(CASE WHEN p.state='in_pool' AND n.latency_ms IS NOT NULL THEN n.latency_ms END) AS avg_latency
            FROM node_pool p
            LEFT JOIN nodes n ON n.hash = p.representative_hash
            """
        ).fetchone()
        top = conn.execute(
            """
            SELECT asn, COUNT(*) AS c FROM node_pool
            WHERE state='in_pool' AND asn IS NOT NULL
            GROUP BY asn ORDER BY c DESC LIMIT 10
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": row["total"] or 0,
        "by_state": {
            "in_pool": row["in_pool"] or 0,
            "probation": row["probation"] or 0,
            "probing": row["probing"] or 0,
            "evicted": row["evicted"] or 0,
        },
        "avg_abuse_score": round(row["avg_abuse"], 1) if row["avg_abuse"] is not None else None,
        "avg_latency_ms": round(row["avg_latency"], 1) if row["avg_latency"] is not None else None,
        "top_asn_in_pool": [{"asn": t["asn"], "count": t["c"]} for t in top],
        "asn_registry": asn_store.counts(),
        "run": pool_mod.get_progress(),
    }


@router.get("/pool/{ip}/{port}")
def get_pool_node(ip: str, port: int):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"非法 IP: {ip}"))
    row = db.get_pool_node(ip)
    if row is None:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"节点 {ip} 不在池中"))
    result = build_pool_response(row)
    result["checks"] = [build_pool_check_response(c) for c in db.get_pool_checks(ip, limit=20)]
    return result


@router.post("/pool/{ip}/{port}/recheck", dependencies=[Depends(_require_auth)])
def recheck_pool_node(ip: str, port: int):
    """强制对单个 IP 重新检测（忽略缓存与 next_check_at）。"""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", f"非法 IP: {ip}"))
    from .pool import recheck_ip
    res = recheck_ip(ip)
    if res["status"] == "busy":
        return JSONResponse(
            status_code=202,
            content={"status": "skipped", "message": "已有池任务在运行，请稍后再试"},
        )
    if res["status"] == "not_found":
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"IP {ip} 不在数据源中"))
    row = db.get_pool_node(ip)
    if row is None:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", f"节点 {ip} 不在池中"))
    result = build_pool_response(row)
    result["checks"] = [build_pool_check_response(c) for c in db.get_pool_checks(ip, limit=20)]
    return result


@router.post("/pool/rebuild", dependencies=[Depends(_require_auth)])
def rebuild_pool(force: bool = Query(default=False)):
    from .pool import get_progress, schedule_pool_sync
    started = schedule_pool_sync(force=force)
    if not started:
        return JSONResponse(
            status_code=202,
            content={
                "status": "skipped",
                "message": "已有池任务在运行",
                "run": get_progress(),
            },
        )
    return JSONResponse(status_code=202, content={"status": "accepted", "force": force})


@router.get("/tasks")
def tasks():
    """返回所有后台任务的实时进度（供给前端进度条轮询）。"""
    from . import progress
    return progress.snapshot_all()


# --------------------------------------------------------------------------- #
# 导出（CSV / JSON，遵循列表接口的过滤/排序，但不分页）                          #
# --------------------------------------------------------------------------- #

_NODE_EXPORT_COLUMNS = [
    "ip", "port", "protocol", "tag", "is_residential", "asn", "as_name", "as_type",
    "is_hosting", "is_anonymous", "country_code", "city", "region",
    "latency_ms", "egress_ip", "egress_region", "failure_count",
    "enriched", "created_at", "updated_at",
]
_POOL_EXPORT_COLUMNS = [
    "ip", "port", "port_count", "protocol", "state", "asn", "abuse_score", "pool_score",
    "latency_ms", "egress_ip", "egress_region", "failure_count",
    "consecutive_failures", "reason", "admitted_at", "last_checked_at",
    "next_check_at", "evicted_at", "evict_reason",
]
_ASN_EXPORT_COLUMNS = [
    "asn", "category", "org", "country", "enabled", "source", "note", "updated_at",
]


def _export_response(items: list, columns: list, filename: str, fmt: str) -> Response:
    fmt = (fmt or "csv").lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "format 仅支持 csv/json"))
    if fmt == "json":
        return Response(
            content=json.dumps(items, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for it in items:
        writer.writerow({c: it.get(c) for c in columns})
    # 前置 BOM，便于 Excel 正确识别 UTF-8
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


def _node_export_row(r) -> dict:
    return {
        "ip": r["ip"], "port": r["port"], "protocol": r["protocol"], "tag": r["tag"],
        "is_residential": r["is_residential"], "asn": r["asn"], "as_name": r["as_name"],
        "as_type": r["as_type"], "is_hosting": r["is_hosting"], "is_anonymous": r["is_anonymous"],
        "country_code": r["country_code"], "city": r["city"], "region": r["region"],
        "latency_ms": r["latency_ms"], "egress_ip": r["egress_ip"],
        "egress_region": r["egress_region"], "failure_count": r["failure_count"],
        "enriched": r["enriched"], "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


@router.get("/export/nodes")
def export_nodes(
    protocol: Optional[str] = Query(default=None),
    residential: Optional[str] = Query(default=None),
    as_type: Optional[str] = Query(default=None),
    country: Optional[str] = Query(default=None),
    enriched: Optional[bool] = Query(default=None),
    max_latency_ms: Optional[float] = Query(default=None, ge=0),
    sort: Optional[str] = Query(default=None),
    format: str = Query(default="csv"),
):
    where, params = _build_where(protocol, residential, as_type, country, enriched,
                                 max_latency_ms=max_latency_ms)
    rows = db.get_nodes_for_export(where, params, _parse_sort(sort))
    return _export_response([_node_export_row(r) for r in rows], _NODE_EXPORT_COLUMNS, "nodes", format)


@router.get("/export/pool")
def export_pool(
    state: Optional[str] = Query(default=None),
    protocol: Optional[str] = Query(default=None),
    max_abuse_score: Optional[int] = Query(default=None, ge=0, le=100),
    min_pool_score: Optional[int] = Query(default=None, ge=0, le=100),
    max_latency_ms: Optional[float] = Query(default=None, ge=0),
    q: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    format: str = Query(default="csv"),
):
    where, params = _build_pool_where(state, protocol, max_abuse_score, min_pool_score,
                                      max_latency_ms, q)
    rows = db.get_pool_for_export(where, params, _parse_pool_sort(sort))
    return _export_response([build_pool_response(r) for r in rows], _POOL_EXPORT_COLUMNS, "pool", format)


@router.get("/export/asn")
def export_asn(
    category: Optional[str] = Query(default=None),
    enabled: Optional[bool] = Query(default=None),
    q: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    format: str = Query(default="csv"),
):
    if category and not asn_store.is_valid_category(category):
        raise HTTPException(status_code=400, detail=error_envelope("BAD_PARAM", "category 取值非法"))
    order_by = _parse_sort_generic(sort, ASN_SORT_WHITELIST, "", "asn ASC")
    _, rows = db.list_asn(category, enabled, q, 100000, 0, order_by)
    return _export_response([build_asn_response(r) for r in rows], _ASN_EXPORT_COLUMNS, "asn_registry", format)


# --------------------------------------------------------------------------- #
# Resin 订阅发布（只读：Resin 主动拉取本端点作为远程订阅）                      #
# --------------------------------------------------------------------------- #

def _check_feed_token(token: Optional[str], require_enabled: bool = True) -> None:
    """校验订阅端点访问 token。

    - token 未配置时不鉴权（仅建议用于内网/同机场景）。
    - Resin 拉取时不发送自定义请求头，故 token 只能通过 query 传入。
    """
    if require_enabled and not settings.resin_feed_enabled:
        raise HTTPException(status_code=404, detail=error_envelope("NOT_FOUND", "Resin 订阅端点未启用"))
    if settings.resin_feed_token and token != settings.resin_feed_token:
        raise HTTPException(status_code=401, detail=error_envelope("UNAUTHORIZED", "无效的订阅 token"))


@router.get("/resin/subscription")
def resin_subscription(token: Optional[str] = Query(default=None)):
    """返回 sing-box 订阅内容，供 Resin 作为远程订阅 URL 拉取。"""
    _check_feed_token(token)
    from .resin_feed import build_feed
    feed = build_feed()
    return Response(
        content=feed["text"],
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/resin/status")
def resin_status(token: Optional[str] = Query(default=None)):
    """订阅端点状态：启用情况、节点数、内容哈希（不返回节点内容）。"""
    _check_feed_token(token)
    from .resin_feed import build_feed, contains_credentials
    feed = build_feed()
    return {
        "enabled": settings.resin_feed_enabled,
        "states": settings.resin_feed_states,
        "subscription_name": settings.resin_feed_subscription_name,
        "node_count": feed["node_count"],
        "content_hash": feed["hash"],
        "token_required": bool(settings.resin_feed_token),
        "contains_credentials": contains_credentials(feed["content"]),
    }


@router.get("/export/resin-subscription")
def export_resin_subscription(token: Optional[str] = Query(default=None)):
    """手动下载订阅内容（便于先做 dry-run 校验；不要求端点已启用）。"""
    _check_feed_token(token, require_enabled=False)
    from .resin_feed import build_feed
    feed = build_feed()
    return Response(
        content=feed["text"],
        media_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{settings.resin_feed_subscription_name}.json"',
        },
    )
