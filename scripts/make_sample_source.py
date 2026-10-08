"""生成用于本地联调的示例 Resin 数据源（cache.db + nodes_static）。

无需真实 Resin 环境即可验证本服务：读取 -> 过滤 http/https -> 落库 -> 查询。
用法：
    python scripts/make_sample_source.py [输出路径] [节点总数]
默认输出：/workspace/resin-data/cache/cache.db
"""
import json
import os
import random
import sqlite3
import sys
import time

TYPES = ["vless", "shadowsocks", "http", "trojan", "vmess", "hysteria2", "anytls", "socks"]
# 抽样分布大致对齐 PRD 第 2.3 节的实测占比
WEIGHTS = [0.55, 0.16, 0.12, 0.08, 0.06, 0.02, 0.005, 0.005]

COUNTRIES = ["US", "JP", "DE", "GB", "FR", "SG", "HK", "NL", "CA", "AU"]
HOSTING_SUBNETS = ["138.199", "45.32", "103.224", "104.21", "209.141"]
RES_SUBNETS = ["24.61", "98.207", "71.85", "75.108", "173.230"]
LATENCY_DOMAINS = ["cloudflare.com", "gstatic.com"]
REGIONS = ["us", "jp", "de", "gb", "fr", "sg", "hk", "nl", "ca", "au"]


def rand_ip(force_hosting=None) -> str:
    if force_hosting:
        base = random.choice(HOSTING_SUBNETS)
    elif force_hosting is False:
        base = random.choice(RES_SUBNETS)
    else:
        base = random.choice(HOSTING_SUBNETS + RES_SUBNETS)
    return f"{base}.{random.randint(0,255)}.{random.randint(1,254)}"


def make_node(n: int):
    typ = random.choices(TYPES, weights=WEIGHTS, k=1)[0]
    cc = random.choice(COUNTRIES)
    # http 节点里约 80% 开 tls（-> https）
    if typ == "http":
        tls_on = random.random() < 0.8
        ip = rand_ip(force_hosting=random.random() < 0.6)
        raw = {
            "server": ip,
            "server_port": random.randint(1000, 9999),
            "type": "http",
            "tls": {"enabled": tls_on, "insecure": True},
            "tag": f"🇺🇸{cc}_{n}|1.4MB/s",
            "username": "null",
            "password": "null",
        }
    else:
        ip = rand_ip()
        raw = {
            "server": ip,
            "server_port": random.randint(1000, 9999),
            "type": typ,
            "uuid": "x" * 16,
            "tag": f"node_{typ}_{n}",
        }
    # 重复 ip:port 场景（多订阅）
    if random.random() < 0.1:
        raw["server_port"] = 9002
    return {
        "hash": f"hash_{n:08x}{random.randint(0, 9999):04d}",
        "raw_options_json": json.dumps(raw, ensure_ascii=False),
        "created_at_ns": int(time.time() * 1_000_000_000) - n * 1_000_000,
    }


def build(path: str, total: int = 3027):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes_static (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT UNIQUE NOT NULL,
            raw_options_json TEXT,
            created_at_ns INTEGER
        )
        """
    )
    # 运行时指标：延迟（EWMA，纳秒）与动态状态（失败/熔断/出口）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_latency (
            node_hash TEXT NOT NULL,
            domain TEXT NOT NULL,
            ewma_ns INTEGER NOT NULL,
            last_updated_ns INTEGER NOT NULL,
            PRIMARY KEY (node_hash, domain)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes_dynamic (
            hash TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            circuit_open_since INTEGER NOT NULL DEFAULT 0,
            egress_ip TEXT NOT NULL DEFAULT '',
            egress_region TEXT NOT NULL DEFAULT '',
            egress_updated_at_ns INTEGER NOT NULL DEFAULT 0,
            last_latency_probe_attempt_ns INTEGER NOT NULL DEFAULT 0,
            last_authority_latency_probe_attempt_ns INTEGER NOT NULL DEFAULT 0,
            last_egress_update_attempt_ns INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("DELETE FROM nodes_static")
    conn.execute("DELETE FROM node_latency")
    conn.execute("DELETE FROM nodes_dynamic")

    nodes = [make_node(i) for i in range(total)]
    # 强制注入少量“域名为 server”的 http 节点，覆盖 source 跳过非 IP 节点的逻辑
    injected = 0
    for n in nodes:
        if injected >= 3:
            break
        try:
            raw = json.loads(n["raw_options_json"])
        except (ValueError, TypeError):
            continue
        if raw.get("type") == "http":
            raw["server"] = f"node{injected}.example.com"
            n["raw_options_json"] = json.dumps(raw, ensure_ascii=False)
            injected += 1
    conn.executemany(
        "INSERT INTO nodes_static (hash, raw_options_json, created_at_ns) VALUES (?, ?, ?)",
        [(n["hash"], n["raw_options_json"], n["created_at_ns"]) for n in nodes],
    )

    now_ns = int(time.time() * 1_000_000_000)
    latency_rows = []
    dynamic_rows = []
    for n in nodes:
        for domain in LATENCY_DOMAINS:
            # 20~600ms，模拟好/中/差节点
            ewma_ns = random.randint(20, 600) * 1_000_000
            latency_rows.append((n["hash"], domain, ewma_ns, now_ns))
        dynamic_rows.append((
            n["hash"], 0, 0, rand_ip(), random.choice(REGIONS).lower(),
            now_ns, now_ns, now_ns, now_ns,
        ))
    conn.executemany(
        "INSERT INTO node_latency (node_hash, domain, ewma_ns, last_updated_ns) VALUES (?, ?, ?, ?)",
        latency_rows,
    )
    conn.executemany(
        """INSERT INTO nodes_dynamic (
            hash, failure_count, circuit_open_since, egress_ip, egress_region,
            egress_updated_at_ns, last_latency_probe_attempt_ns,
            last_authority_latency_probe_attempt_ns, last_egress_update_attempt_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        dynamic_rows,
    )
    conn.commit()
    http_count = conn.execute(
        "SELECT COUNT(*) FROM nodes_static WHERE raw_options_json LIKE '%\"type\": \"http\"%'"
    ).fetchone()[0]
    conn.close()
    print(f"已生成示例数据源: {path}")
    print(f"  总节点数: {total}，其中 type==http 约: {http_count}")
    print(f"  延迟记录: {len(latency_rows)}，动态记录: {len(dynamic_rows)}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/workspace/resin-data/cache/cache.db"
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 3027
    build(out, total)
