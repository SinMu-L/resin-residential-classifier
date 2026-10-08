"""SQLite 持久化层：应用库（新建、独立文件）的建表、读写与索引。

数据源 cache.db 全程只读，相关读取见 source.py。
"""
import os
import sqlite3
import threading
from typing import Optional

from .config import settings

# 应用库建表 DDL（PRD 第 6 节）
SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hash             TEXT UNIQUE NOT NULL,
    ip               TEXT NOT NULL,
    port             INTEGER NOT NULL,
    protocol         TEXT NOT NULL,
    raw_type         TEXT NOT NULL,
    tag              TEXT,
    is_residential   INTEGER,
    enriched         INTEGER NOT NULL DEFAULT 0,
    enrich_source    TEXT,
    enrich_plan      TEXT,
    asn              TEXT,
    as_name          TEXT,
    as_type          TEXT,
    is_hosting       INTEGER,
    is_anonymous     INTEGER,
    country_code     TEXT,
    city             TEXT,
    region           TEXT,
    latitude         REAL,
    longitude        REAL,
    raw_options_json TEXT,
    latency_ms       REAL,
    latencies_json   TEXT,
    latency_updated_at INTEGER,
    failure_count    INTEGER,
    circuit_open_since INTEGER,
    egress_ip        TEXT,
    egress_region    TEXT,
    egress_updated_at INTEGER,
    last_enriched_at INTEGER,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_ip_port ON nodes(ip, port);
CREATE INDEX IF NOT EXISTS idx_nodes_proto_res ON nodes(protocol, is_residential);
CREATE INDEX IF NOT EXISTS idx_nodes_as_type ON nodes(as_type);

CREATE TABLE IF NOT EXISTS ip_enrichments (
    ip              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    plan            TEXT,
    raw             TEXT NOT NULL,
    is_residential  INTEGER,
    is_hosting      INTEGER,
    as_type         TEXT,
    checked_at      INTEGER NOT NULL,
    expires_at      INTEGER,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    nodes_seen      INTEGER,
    nodes_new       INTEGER,
    nodes_updated   INTEGER,
    nodes_enriched  INTEGER,
    nodes_failed    INTEGER,
    status          TEXT
);

-- ASN 名单（L1 初筛）：category = 'cloud'(机房/云) | 'residential'(住宅 ISP)
CREATE TABLE IF NOT EXISTS asn_registry (
    asn        TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    org        TEXT,
    country    TEXT,
    source     TEXT,
    note       TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asn_category ON asn_registry(category, enabled);

-- IP 级风险情报（L2）：AbuseIPDB 等，独立于 fallback 富化链
CREATE TABLE IF NOT EXISTS ip_risk (
    ip                TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    abuse_confidence  INTEGER,
    total_reports     INTEGER,
    distinct_users    INTEGER,
    last_reported_at  TEXT,
    is_whitelisted    INTEGER,
    usage_type        TEXT,
    isp               TEXT,
    country           TEXT,
    raw               TEXT,
    checked_at        INTEGER NOT NULL,
    expires_at        INTEGER,
    error             TEXT
);

-- 节点池（按 IP 去重）：漏斗状态机
CREATE TABLE IF NOT EXISTS node_pool (
    ip                   TEXT PRIMARY KEY,
    representative_hash  TEXT,
    port_count           INTEGER,
    state                TEXT NOT NULL,
    asn                  TEXT,
    abuse_score          INTEGER,
    pool_score           INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    reason               TEXT,
    admitted_at          INTEGER,
    last_checked_at      INTEGER,
    next_check_at        INTEGER,
    evicted_at           INTEGER,
    evict_reason         TEXT,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_state ON node_pool(state);
CREATE INDEX IF NOT EXISTS idx_pool_next_check ON node_pool(state, next_check_at);

-- 漏斗决策历史（追加式，用于审计/趋势）
CREATE TABLE IF NOT EXISTS pool_checks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ip                   TEXT NOT NULL,
    representative_hash  TEXT,
    stage                TEXT NOT NULL,
    passed               INTEGER NOT NULL,
    score                INTEGER,
    reason               TEXT,
    raw                  TEXT,
    checked_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_checks_ip ON pool_checks(ip, id);
"""

_write_lock = threading.Lock()

# 增量列迁移：老库缺列时自动 ALTER TABLE 补齐（CREATE TABLE IF NOT EXISTS 不会加列）
_MIGRATIONS = {
    "nodes": {
        "latency_ms": "REAL",
        "latencies_json": "TEXT",
        "latency_updated_at": "INTEGER",
        "failure_count": "INTEGER",
        "circuit_open_since": "INTEGER",
        "egress_ip": "TEXT",
        "egress_region": "TEXT",
        "egress_updated_at": "INTEGER",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def get_app_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.app_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_app_db() -> None:
    """新建/补齐应用库表结构，并开启 WAL 以提升并发读写能力。"""
    path = settings.app_db_path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 节点（nodes）                                                               #
# --------------------------------------------------------------------------- #

def upsert_node(n: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                INSERT INTO nodes (
                    hash, ip, port, protocol, raw_type, tag,
                    is_residential, enriched, enrich_source, enrich_plan,
                    asn, as_name, as_type, is_hosting, is_anonymous,
                    country_code, city, region, latitude, longitude,
                    raw_options_json, latency_ms, latencies_json, latency_updated_at,
                    failure_count, circuit_open_since, egress_ip, egress_region, egress_updated_at,
                    last_enriched_at, created_at, updated_at
                ) VALUES (
                    :hash, :ip, :port, :protocol, :raw_type, :tag,
                    :is_residential, :enriched, :enrich_source, :enrich_plan,
                    :asn, :as_name, :as_type, :is_hosting, :is_anonymous,
                    :country_code, :city, :region, :latitude, :longitude,
                    :raw_options_json, :latency_ms, :latencies_json, :latency_updated_at,
                    :failure_count, :circuit_open_since, :egress_ip, :egress_region, :egress_updated_at,
                    :last_enriched_at, :created_at, :updated_at
                )
                ON CONFLICT(hash) DO UPDATE SET
                    ip=excluded.ip, port=excluded.port, protocol=excluded.protocol,
                    raw_type=excluded.raw_type, tag=excluded.tag,
                    is_residential=excluded.is_residential, enriched=excluded.enriched,
                    enrich_source=excluded.enrich_source, enrich_plan=excluded.enrich_plan,
                    asn=excluded.asn, as_name=excluded.as_name, as_type=excluded.as_type,
                    is_hosting=excluded.is_hosting, is_anonymous=excluded.is_anonymous,
                    country_code=excluded.country_code, city=excluded.city, region=excluded.region,
                    latitude=excluded.latitude, longitude=excluded.longitude,
                    raw_options_json=excluded.raw_options_json,
                    latency_ms=excluded.latency_ms, latencies_json=excluded.latencies_json,
                    latency_updated_at=excluded.latency_updated_at,
                    failure_count=excluded.failure_count,
                    circuit_open_since=excluded.circuit_open_since,
                    egress_ip=excluded.egress_ip, egress_region=excluded.egress_region,
                    egress_updated_at=excluded.egress_updated_at,
                    last_enriched_at=excluded.last_enriched_at, updated_at=excluded.updated_at
                """,
                n,
            )
            conn.commit()
        finally:
            conn.close()


def get_existing_hashes(hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    with _write_lock:
        conn = get_app_conn()
        try:
            placeholders = ",".join("?" for _ in hashes)
            rows = conn.execute(
                f"SELECT hash FROM nodes WHERE hash IN ({placeholders})", hashes
            ).fetchall()
            return {r["hash"] for r in rows}
        finally:
            conn.close()


def delete_nodes_not_in(hashes: list[str]) -> int:
    """删除已不在数据源中的节点（hash 不在给定集合内），返回删除条数。"""
    keep = set(hashes)
    with _write_lock:
        conn = get_app_conn()
        try:
            stale = [r["hash"] for r in conn.execute("SELECT hash FROM nodes").fetchall()
                     if r["hash"] not in keep]
            for i in range(0, len(stale), 500):
                chunk = stale[i:i + 500]
                ph = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM nodes WHERE hash IN ({ph})", chunk)
            conn.commit()
            return len(stale)
        finally:
            conn.close()


def get_node_by_ip_port(ip: str, port: int) -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            """
            SELECT n.*, e.raw AS enrichment_raw
            FROM nodes n
            LEFT JOIN ip_enrichments e ON n.ip = e.ip
            WHERE n.ip = ? AND n.port = ?
            LIMIT 1
            """,
            (ip, port),
        ).fetchone()
    finally:
        conn.close()


def get_node_by_id(node_id: int) -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            """
            SELECT n.*, e.raw AS enrichment_raw
            FROM nodes n
            LEFT JOIN ip_enrichments e ON n.ip = e.ip
            WHERE n.id = ?
            LIMIT 1
            """,
            (node_id,),
        ).fetchone()
    finally:
        conn.close()


def count_nodes() -> int:
    conn = get_app_conn()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    finally:
        conn.close()


def get_candidate_rows(where: str, params: list) -> list[sqlite3.Row]:
    """返回满足过滤条件的节点最小字段集合（用于随机选取的候选集）。"""
    conn = get_app_conn()
    try:
        return conn.execute(
            f"SELECT id, ip, port, hash, enriched FROM nodes n {where}", params
        ).fetchall()
    finally:
        conn.close()


def get_nodes_page(where: str, params: list, limit: int, offset: int,
                   order_by: str) -> tuple[int, list[sqlite3.Row]]:
    conn = get_app_conn()
    try:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM nodes n {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT n.*, e.raw AS enrichment_raw
            FROM nodes n
            LEFT JOIN ip_enrichments e ON n.ip = e.ip
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return total, rows
    finally:
        conn.close()


def get_nodes_for_export(where: str, params: list, order_by: str) -> list[sqlite3.Row]:
    """返回满足过滤条件的全部节点（不分页），用于导出。"""
    conn = get_app_conn()
    try:
        return conn.execute(
            f"""
            SELECT n.*, e.raw AS enrichment_raw
            FROM nodes n
            LEFT JOIN ip_enrichments e ON n.ip = e.ip
            {where}
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# IP 富化（ip_enrichments）                                                    #
# --------------------------------------------------------------------------- #

def upsert_enrichment(e: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                INSERT INTO ip_enrichments (
                    ip, source, plan, raw, is_residential, is_hosting,
                    as_type, checked_at, expires_at, error
                ) VALUES (
                    :ip, :source, :plan, :raw, :is_residential, :is_hosting,
                    :as_type, :checked_at, :expires_at, :error
                )
                ON CONFLICT(ip) DO UPDATE SET
                    source=excluded.source, plan=excluded.plan, raw=excluded.raw,
                    is_residential=excluded.is_residential, is_hosting=excluded.is_hosting,
                    as_type=excluded.as_type, checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at, error=excluded.error
                """,
                e,
            )
            conn.commit()
        finally:
            conn.close()


def get_enrichment_by_ip(ip: str) -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            "SELECT * FROM ip_enrichments WHERE ip = ?", (ip,)
        ).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 同步运行记录（ingest_runs）                                                 #
# --------------------------------------------------------------------------- #

def start_ingest_run() -> int:
    with _write_lock:
        conn = get_app_conn()
        try:
            cur = conn.execute(
                "INSERT INTO ingest_runs (started_at, status) VALUES (?, 'running')",
                (int(__import__("time").time()),),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def finish_ingest_run(run_id: int, stats: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                UPDATE ingest_runs SET
                    finished_at=?, nodes_seen=?, nodes_new=?, nodes_updated=?,
                    nodes_enriched=?, nodes_failed=?, status=?
                WHERE id=?
                """,
                (
                    stats.get("finished_at"),
                    stats.get("nodes_seen"),
                    stats.get("nodes_new"),
                    stats.get("nodes_updated"),
                    stats.get("nodes_enriched"),
                    stats.get("nodes_failed"),
                    stats.get("status"),
                    run_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_last_ingest_run() -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# ASN 名单（asn_registry）                                                     #
# --------------------------------------------------------------------------- #

def list_asn(category: Optional[str] = None, enabled: Optional[bool] = None,
             q: Optional[str] = None, limit: int = 100, offset: int = 0,
             order_by: str = "asn ASC") -> tuple[int, list[sqlite3.Row]]:
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if enabled is not None:
        clauses.append("enabled = ?")
        params.append(1 if enabled else 0)
    if q:
        clauses.append("(asn LIKE ? OR org LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = get_app_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM asn_registry {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM asn_registry {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return total, rows
    finally:
        conn.close()


def get_enabled_asn(category: str) -> list[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            "SELECT asn FROM asn_registry WHERE category = ? AND enabled = 1",
            (category,),
        ).fetchall()
    finally:
        conn.close()


def get_asn(asn: str) -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            "SELECT * FROM asn_registry WHERE asn = ?", (asn,)
        ).fetchone()
    finally:
        conn.close()


def upsert_asn(a: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                INSERT INTO asn_registry (
                    asn, category, org, country, source, note, enabled,
                    created_at, updated_at
                ) VALUES (
                    :asn, :category, :org, :country, :source, :note, :enabled,
                    :created_at, :updated_at
                )
                ON CONFLICT(asn) DO UPDATE SET
                    category=excluded.category,
                    org=COALESCE(excluded.org, asn_registry.org),
                    country=COALESCE(excluded.country, asn_registry.country),
                    note=COALESCE(excluded.note, asn_registry.note),
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                a,
            )
            conn.commit()
        finally:
            conn.close()


def update_asn(asn: str, fields: dict) -> bool:
    if not fields:
        return False
    cols = ", ".join(f"{k}=:{k}" for k in fields)
    params = dict(fields)
    params["asn"] = asn
    with _write_lock:
        conn = get_app_conn()
        try:
            cur = conn.execute(
                f"UPDATE asn_registry SET {cols} WHERE asn=:asn", params
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_asn(asn: str) -> bool:
    with _write_lock:
        conn = get_app_conn()
        try:
            cur = conn.execute("DELETE FROM asn_registry WHERE asn = ?", (asn,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# IP 风险（ip_risk）                                                           #
# --------------------------------------------------------------------------- #

def upsert_ip_risk(r: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                INSERT INTO ip_risk (
                    ip, source, abuse_confidence, total_reports, distinct_users,
                    last_reported_at, is_whitelisted, usage_type, isp, country,
                    raw, checked_at, expires_at, error
                ) VALUES (
                    :ip, :source, :abuse_confidence, :total_reports, :distinct_users,
                    :last_reported_at, :is_whitelisted, :usage_type, :isp, :country,
                    :raw, :checked_at, :expires_at, :error
                )
                ON CONFLICT(ip) DO UPDATE SET
                    source=excluded.source,
                    abuse_confidence=excluded.abuse_confidence,
                    total_reports=excluded.total_reports,
                    distinct_users=excluded.distinct_users,
                    last_reported_at=excluded.last_reported_at,
                    is_whitelisted=excluded.is_whitelisted,
                    usage_type=excluded.usage_type,
                    isp=excluded.isp,
                    country=excluded.country,
                    raw=excluded.raw,
                    checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at,
                    error=excluded.error
                """,
                r,
            )
            conn.commit()
        finally:
            conn.close()


def get_ip_risk(ip: str) -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute("SELECT * FROM ip_risk WHERE ip = ?", (ip,)).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 节点池（node_pool / pool_checks）                                            #
# --------------------------------------------------------------------------- #

def get_nodes_for_pool() -> list[sqlite3.Row]:
    """返回参与漏斗的节点最小字段集合（含 id 以便选取代表节点）。"""
    conn = get_app_conn()
    try:
        return conn.execute(
            "SELECT id, hash, ip, port, asn, as_type, is_hosting FROM nodes"
        ).fetchall()
    finally:
        conn.close()


def get_all_pool_nodes() -> list[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute("SELECT * FROM node_pool").fetchall()
    finally:
        conn.close()


def upsert_pool_node(p: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                INSERT INTO node_pool (
                    ip, representative_hash, port_count, state, asn, abuse_score,
                    pool_score, consecutive_failures, reason, admitted_at,
                    last_checked_at, next_check_at, evicted_at, evict_reason,
                    created_at, updated_at
                ) VALUES (
                    :ip, :representative_hash, :port_count, :state, :asn, :abuse_score,
                    :pool_score, :consecutive_failures, :reason, :admitted_at,
                    :last_checked_at, :next_check_at, :evicted_at, :evict_reason,
                    :created_at, :updated_at
                )
                ON CONFLICT(ip) DO UPDATE SET
                    representative_hash=excluded.representative_hash,
                    port_count=excluded.port_count,
                    state=excluded.state,
                    asn=excluded.asn,
                    abuse_score=excluded.abuse_score,
                    pool_score=excluded.pool_score,
                    consecutive_failures=excluded.consecutive_failures,
                    reason=excluded.reason,
                    admitted_at=excluded.admitted_at,
                    last_checked_at=excluded.last_checked_at,
                    next_check_at=excluded.next_check_at,
                    evicted_at=excluded.evicted_at,
                    evict_reason=excluded.evict_reason,
                    updated_at=excluded.updated_at
                """,
                p,
            )
            conn.commit()
        finally:
            conn.close()


def insert_pool_check(c: dict) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute(
                """
                INSERT INTO pool_checks (
                    ip, representative_hash, stage, passed, score, reason, raw, checked_at
                ) VALUES (
                    :ip, :representative_hash, :stage, :passed, :score, :reason, :raw, :checked_at
                )
                """,
                c,
            )
            conn.commit()
        finally:
            conn.close()


def get_pool_page(where: str, params: list, limit: int, offset: int,
                  order_by: str) -> tuple[int, list[sqlite3.Row]]:
    conn = get_app_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM node_pool p {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT p.*, n.protocol AS node_protocol, n.port AS node_port,
                   n.tag AS node_tag, n.is_residential AS node_is_residential,
                   n.latency_ms AS node_latency_ms, n.egress_ip AS node_egress_ip,
                   n.egress_region AS node_egress_region, n.failure_count AS node_failure_count,
                   n.hash AS node_hash
            FROM node_pool p
            LEFT JOIN nodes n ON n.id = (
                SELECT id FROM nodes WHERE ip = p.ip ORDER BY port ASC, id ASC LIMIT 1
            )
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return total, rows
    finally:
        conn.close()


def get_pool_for_export(where: str, params: list, order_by: str) -> list[sqlite3.Row]:
    """返回满足过滤条件的全部池节点（不分页），用于导出。"""
    conn = get_app_conn()
    try:
        return conn.execute(
            f"""
            SELECT p.*, n.protocol AS node_protocol, n.port AS node_port,
                   n.tag AS node_tag, n.is_residential AS node_is_residential,
                   n.latency_ms AS node_latency_ms, n.egress_ip AS node_egress_ip,
                   n.egress_region AS node_egress_region, n.failure_count AS node_failure_count,
                   n.hash AS node_hash
            FROM node_pool p
            LEFT JOIN nodes n ON n.id = (
                SELECT id FROM nodes WHERE ip = p.ip ORDER BY port ASC, id ASC LIMIT 1
            )
            {where}
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def get_pool_node(ip: str) -> Optional[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            """
            SELECT p.*, n.protocol AS node_protocol, n.port AS node_port,
                   n.tag AS node_tag, n.is_residential AS node_is_residential,
                   n.latency_ms AS node_latency_ms, n.egress_ip AS node_egress_ip,
                   n.egress_region AS node_egress_region, n.failure_count AS node_failure_count,
                   n.hash AS node_hash
            FROM node_pool p
            LEFT JOIN nodes n ON n.id = (
                SELECT id FROM nodes WHERE ip = p.ip ORDER BY port ASC, id ASC LIMIT 1
            )
            WHERE p.ip = ?
            """,
            (ip,),
        ).fetchone()
    finally:
        conn.close()


def get_feed_nodes(states: list[str]) -> list[sqlite3.Row]:
    """返回指定池状态的代表节点及其原始配置（用于生成 Resin 订阅内容）。

    通过 node_pool.representative_hash 关联 nodes，取其 raw_options_json；
    仅返回仍存在于数据源（未被清理）的节点。
    """
    if not states:
        return []
    ph = ",".join("?" for _ in states)
    conn = get_app_conn()
    try:
        return conn.execute(
            f"""
            SELECT p.ip AS ip, p.representative_hash AS hash,
                   n.raw_options_json AS raw_options_json
            FROM node_pool p
            JOIN nodes n ON n.hash = p.representative_hash
            WHERE p.state IN ({ph}) AND n.raw_options_json IS NOT NULL
            ORDER BY p.ip ASC
            """,
            states,
        ).fetchall()
    finally:
        conn.close()


def get_pool_candidates(where: str, params: list) -> list[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            f"SELECT p.*, n.protocol AS node_protocol FROM node_pool p "
            f"LEFT JOIN nodes n ON n.id = "
            f"(SELECT id FROM nodes WHERE ip = p.ip ORDER BY port ASC, id ASC LIMIT 1) {where}",
            params,
        ).fetchall()
    finally:
        conn.close()


def get_pool_due_for_recheck(now: int) -> list[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            """
            SELECT ip, state, consecutive_failures, admitted_at, evicted_at
            FROM node_pool
            WHERE state IN ('probing', 'in_pool', 'probation', 'evicted')
              AND (next_check_at IS NULL OR next_check_at <= ?)
            """,
            (now,),
        ).fetchall()
    finally:
        conn.close()


def delete_pool_node(ip: str) -> None:
    with _write_lock:
        conn = get_app_conn()
        try:
            conn.execute("DELETE FROM node_pool WHERE ip = ?", (ip,))
            conn.commit()
        finally:
            conn.close()


def get_pool_checks(ip: str, limit: int = 50) -> list[sqlite3.Row]:
    conn = get_app_conn()
    try:
        return conn.execute(
            "SELECT * FROM pool_checks WHERE ip = ? ORDER BY id DESC LIMIT ?",
            (ip, limit),
        ).fetchall()
    finally:
        conn.close()
