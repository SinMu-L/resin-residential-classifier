"""响应构建：将数据库行转换为 REST API 的 JSON 结构（PRD 第 5 节）。"""
import json
from typing import Any, Optional


def _b(v: Any) -> Optional[bool]:
    """数据库 INTEGER(0/1/NULL) -> JSON bool / null。"""
    if v is None:
        return None
    return bool(v)


def build_node_response(row) -> dict:
    """根据 nodes 行（含 LEFT JOIN 的 enrichment_raw）构造节点对象。"""
    enriched = bool(row["enriched"])

    enrichment = None
    if enriched:
        raw = None
        if row["enrichment_raw"]:
            try:
                raw = json.loads(row["enrichment_raw"])
            except (ValueError, TypeError):
                raw = None
        enrichment = {
            "source": row["enrich_source"],
            "plan": row["enrich_plan"],
            "asn": row["asn"],
            "as_name": row["as_name"],
            "as_type": row["as_type"],
            "is_hosting": _b(row["is_hosting"]),
            "is_anonymous": _b(row["is_anonymous"]),
            "country_code": row["country_code"],
            "city": row["city"],
            "region": row["region"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "raw": raw,
        }

    raw_options = None
    if row["raw_options_json"]:
        try:
            raw_options = json.loads(row["raw_options_json"])
        except (ValueError, TypeError):
            raw_options = None

    latencies = None
    if "latencies_json" in row.keys() and row["latencies_json"]:
        try:
            latencies = json.loads(row["latencies_json"])
        except (ValueError, TypeError):
            latencies = None

    return {
        "hash": row["hash"],
        "ip": row["ip"],
        "port": row["port"],
        "protocol": row["protocol"],
        "raw_type": row["raw_type"],
        "tag": row["tag"],
        "enriched": enriched,
        "is_residential": row["is_residential"],
        "enrichment": enrichment,
        "raw_options_json": raw_options,
        "latency_ms": row["latency_ms"] if "latency_ms" in row.keys() else None,
        "latencies": latencies,
        "latency_updated_at": row["latency_updated_at"] if "latency_updated_at" in row.keys() else None,
        "failure_count": row["failure_count"] if "failure_count" in row.keys() else None,
        "circuit_open_since": row["circuit_open_since"] if "circuit_open_since" in row.keys() else None,
        "egress_ip": row["egress_ip"] if "egress_ip" in row.keys() else None,
        "egress_region": row["egress_region"] if "egress_region" in row.keys() else None,
        "egress_updated_at": row["egress_updated_at"] if "egress_updated_at" in row.keys() else None,
        "last_enriched_at": row["last_enriched_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def build_enrichment_response(row) -> dict:
    raw = None
    if row["raw"]:
        try:
            raw = json.loads(row["raw"])
        except (ValueError, TypeError):
            raw = None
    return {
        "ip": row["ip"],
        "source": row["source"],
        "plan": row["plan"],
        "is_residential": row["is_residential"],
        "is_hosting": _b(row["is_hosting"]),
        "as_type": row["as_type"],
        "checked_at": row["checked_at"],
        "expires_at": row["expires_at"],
        "raw": raw,
    }


def error_envelope(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def build_asn_response(row) -> dict:
    return {
        "asn": row["asn"],
        "category": row["category"],
        "org": row["org"],
        "country": row["country"],
        "source": row["source"],
        "note": row["note"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def build_pool_response(row) -> dict:
    """node_pool 行（可含 LEFT JOIN 的 nodes 字段）-> 池节点对象。"""
    keys = row.keys()
    return {
        "ip": row["ip"],
        "port": row["node_port"] if "node_port" in keys else None,
        "protocol": row["node_protocol"] if "node_protocol" in keys else None,
        "tag": row["node_tag"] if "node_tag" in keys else None,
        "is_residential": row["node_is_residential"] if "node_is_residential" in keys else None,
        "latency_ms": row["node_latency_ms"] if "node_latency_ms" in keys else None,
        "egress_ip": row["node_egress_ip"] if "node_egress_ip" in keys else None,
        "egress_region": row["node_egress_region"] if "node_egress_region" in keys else None,
        "failure_count": row["node_failure_count"] if "node_failure_count" in keys else None,
        "representative_hash": (
            row["node_hash"] if "node_hash" in keys and row["node_hash"] else row["representative_hash"]
        ),
        "port_count": row["port_count"],
        "state": row["state"],
        "asn": row["asn"],
        "abuse_score": row["abuse_score"],
        "pool_score": row["pool_score"],
        "consecutive_failures": row["consecutive_failures"],
        "reason": row["reason"],
        "admitted_at": row["admitted_at"],
        "last_checked_at": row["last_checked_at"],
        "next_check_at": row["next_check_at"],
        "evicted_at": row["evicted_at"],
        "evict_reason": row["evict_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def build_pool_check_response(row) -> dict:
    return {
        "id": row["id"],
        "ip": row["ip"],
        "stage": row["stage"],
        "passed": bool(row["passed"]),
        "score": row["score"],
        "reason": row["reason"],
        "checked_at": row["checked_at"],
    }


def build_ip_risk_response(row) -> dict:
    raw = None
    if row["raw"]:
        try:
            raw = json.loads(row["raw"])
        except (ValueError, TypeError):
            raw = None
    return {
        "ip": row["ip"],
        "source": row["source"],
        "abuse_confidence": row["abuse_confidence"],
        "total_reports": row["total_reports"],
        "distinct_users": row["distinct_users"],
        "last_reported_at": row["last_reported_at"],
        "is_whitelisted": _b(row["is_whitelisted"]),
        "usage_type": row["usage_type"],
        "isp": row["isp"],
        "country": row["country"],
        "checked_at": row["checked_at"],
        "expires_at": row["expires_at"],
        "error": row["error"],
        "raw": raw,
    }
