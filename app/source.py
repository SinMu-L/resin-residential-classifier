"""数据源只读接入（FR-1）：以 ?mode=ro 打开 Resin 的 cache.db，解析节点与运行时指标。

读取的表：
- ``nodes_static``   ：节点静态配置（本服务的主体）
- ``node_latency``   ：每个节点对各探测域名的 EWMA 延迟（纳秒）
- ``nodes_dynamic``  ：失败计数 / 熔断 / 出口 IP、出口地区等运行时状态
"""
import ipaddress
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("node-enricher.source")


@dataclass
class NodeCandidate:
    hash: str
    ip: str
    port: int
    protocol: str          # 'http' | 'https'
    raw_type: str          # 原始 type（此处恒为 'http'）
    tag: Optional[str]
    created_at: int        # Unix 秒
    raw_options: dict      # 节点原始连接配置
    # 运行时指标（来自 node_latency / nodes_dynamic，可能缺失）
    latency_ms: Optional[float] = None       # 各域名 EWMA 的最小值（毫秒）
    latencies: dict = field(default_factory=dict)  # {domain: 毫秒}
    latency_updated_at: Optional[int] = None  # 对应最小延迟行的更新时间（Unix 秒）
    failure_count: Optional[int] = None
    circuit_open_since: Optional[int] = None
    egress_ip: Optional[str] = None
    egress_region: Optional[str] = None
    egress_updated_at: Optional[int] = None


def _parse_json(s) -> Optional[dict]:
    if s is None:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _ns_to_s(ns) -> int:
    """纳秒时间戳 -> Unix 秒。"""
    if not ns:
        return int(time.time())
    try:
        return int(int(ns) / 1_000_000_000)
    except (ValueError, TypeError):
        return int(time.time())


def _ns_to_s_opt(ns) -> Optional[int]:
    """纳秒时间戳 -> Unix 秒；空值返回 None。"""
    if not ns:
        return None
    try:
        v = int(ns)
        return int(v / 1_000_000_000) if v > 0 else None
    except (ValueError, TypeError):
        return None


def _read_latency(conn: sqlite3.Connection) -> dict:
    """{hash: {"min_ms": float, "updated_at": int|None, "map": {domain: ms}}}。"""
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT node_hash, domain, ewma_ns, last_updated_ns FROM node_latency"
        ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        ewma_ns = r["ewma_ns"]
        if ewma_ns is None:
            continue
        ms = round(float(ewma_ns) / 1_000_000, 1)
        item = out.setdefault(r["node_hash"], {"min_ms": None, "updated_at": None, "map": {}})
        item["map"][r["domain"]] = ms
        if item["min_ms"] is None or ms < item["min_ms"]:
            item["min_ms"] = ms
            item["updated_at"] = _ns_to_s_opt(r["last_updated_ns"])
    return out


def _read_dynamic(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    try:
        rows = conn.execute(
            """
            SELECT hash, failure_count, circuit_open_since, egress_ip,
                   egress_region, egress_updated_at_ns
            FROM nodes_dynamic
            """
        ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        out[r["hash"]] = {
            "failure_count": r["failure_count"],
            "circuit_open_since": _ns_to_s_opt(r["circuit_open_since"]),
            "egress_ip": r["egress_ip"] or None,
            "egress_region": r["egress_region"] or None,
            "egress_updated_at": _ns_to_s_opt(r["egress_updated_at_ns"]),
        }
    return out


def read_source_nodes():
    """读取并过滤数据源节点。

    返回 (candidates, error)。error 非空表示数据源不可达/解析失败，
    此时 candidates 为空，调用方应记录并降级，但不中断服务。
    """
    path = _source_path()
    try:
        # immutable=1：只读挂载场景下 SQLite 不会尝试创建 -shm/-wal（否则在
        # :ro 文件系统上会抛 unable to open database file）；同时不获取任何锁，
        # 从数据库层面彻底杜绝误写，契合 PRD AC-1 的只读接入要求。
        uri = f"file:{path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT hash, raw_options_json, created_at_ns FROM nodes_static"
            ).fetchall()
            latency = _read_latency(conn)
            dynamic = _read_dynamic(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 数据源不可达需整体降级
        return [], f"无法读取数据源 {path}: {e}"

    candidates: list[NodeCandidate] = []
    skipped_non_ip = 0
    for r in rows:
        raw = _parse_json(r["raw_options_json"])
        if not isinstance(raw, dict):
            continue
        typ = str(raw.get("type") or "").lower()
        # FR-2.1：仅保留 http 类型（https 由 http + tls.enabled 推导）
        if typ != "http":
            continue
        server = raw.get("server")
        port = raw.get("server_port")
        if not server or not port:
            continue
        # 仅接受 IP 形态的 server；域名字面量无法做 IP 富化/池判定，跳过
        server = str(server).strip()
        try:
            ipaddress.ip_address(server)
        except ValueError:
            skipped_non_ip += 1
            continue
        try:
            port = int(port)
        except (ValueError, TypeError):
            continue
        tls = raw.get("tls")
        tls_enabled = bool(tls.get("enabled", False)) if isinstance(tls, dict) else False
        protocol = "https" if tls_enabled else "http"

        h = str(r["hash"])
        lat = latency.get(h)
        dyn = dynamic.get(h) or {}
        candidates.append(
            NodeCandidate(
                hash=h,
                ip=server,
                port=port,
                protocol=protocol,
                raw_type="http",
                tag=raw.get("tag"),
                created_at=_ns_to_s(r["created_at_ns"]),
                raw_options=raw,
                latency_ms=(lat["min_ms"] if lat else None),
                latencies=(lat["map"] if lat else {}),
                latency_updated_at=(lat["updated_at"] if lat else None),
                failure_count=dyn.get("failure_count"),
                circuit_open_since=dyn.get("circuit_open_since"),
                egress_ip=dyn.get("egress_ip"),
                egress_region=dyn.get("egress_region"),
                egress_updated_at=dyn.get("egress_updated_at"),
            )
        )
    if skipped_non_ip:
        logger.info("跳过 %d 个 server 为域名的节点（仅支持 IP 节点）", skipped_non_ip)
    return candidates, None


def source_reachable() -> bool:
    try:
        uri = f"file:{_source_path()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1 FROM nodes_static LIMIT 1")
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _source_path() -> str:
    from .config import settings
    return settings.source_db_path
