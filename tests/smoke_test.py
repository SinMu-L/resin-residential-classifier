"""离线冒烟测试：构造示例数据源 + Mock 富化器，验证 PRD 关键验收点。

运行：
    python tests/smoke_test.py
依赖：fastapi / httpx（TestClient）
"""
import ipaddress
import json
import os
import random
import tempfile
import time

# 必须在导入 app 之前设置环境变量（config 在导入时读取）
_TMP = tempfile.mkdtemp(prefix="enricher_test_")
SRC_DB = os.path.join(_TMP, "cache", "cache.db")
APP_DB = os.path.join(_TMP, "app", "nodes.sqlite")
os.environ["SOURCE_DB_PATH"] = SRC_DB
os.environ["APP_DB_PATH"] = APP_DB
os.environ["ENRICHERS"] = "ipinfo"
os.environ["INGEST_INTERVAL"] = "0"   # 关闭定时调度，改为受控触发
os.environ["REFRESH_TOKEN"] = "secret"
os.environ["POOL_ENABLED"] = "false"  # 关闭池定时调度，测试手动触发
os.environ["POOL_EVICT_FAILURES"] = "1"
os.environ["RESIN_FEED_ENABLED"] = "true"
os.environ["RESIN_FEED_TOKEN"] = "feedsecret"

from fastapi.testclient import TestClient  # noqa: E402

from app import ingest, pool  # noqa: E402
from app.enricher.base import BaseIpEnricher, IpEnrichment  # noqa: E402
from app.risk.base import BaseRiskChecker  # noqa: E402
from scripts.make_sample_source import build as build_source  # noqa: E402


class FakeEnricher(BaseIpEnricher):
    name = "fake"

    def _data(self, ip):
        # 以网段决定标签，覆盖 datacenter / residential / residential_proxy / unknown
        if ip.startswith("138.199") or ip.startswith("45.32"):
            return {
                "as": {"asn": "AS12345", "name": "Example Hosting LLC", "type": "hosting"},
                "is_hosting": True,
                "is_anonymous": False,
                "geo": {"city": "Los Angeles", "region": "California", "country_code": "US",
                        "latitude": 34.05, "longitude": -118.24},
            }
        if ip.startswith("24.61") or ip.startswith("98.207"):
            return {
                "as": {"asn": "AS22222", "name": "Comcast Cable", "type": "isp"},
                "is_hosting": False,
                "is_anonymous": False,
                "geo": {"city": "New York", "region": "NY", "country_code": "US",
                        "latitude": 40.7, "longitude": -74.0},
            }
        if ip.startswith("71.85"):
            return {
                "as": {"asn": "AS33333", "name": "ProxyNet", "type": "business"},
                "is_hosting": False,
                "is_anonymous": True,
                "anonymous": {"is_res_proxy": True},
                "geo": {"city": "Chicago", "region": "IL", "country_code": "US"},
            }
        # 未知：仅 country
        return {"country": "JP"}

    def enrich(self, ip):
        return IpEnrichment(ip=ip, source="fake", plan="core", raw=self._data(ip),
                            enriched_at=__import__("time").time(), error=None)

    def enrich_batch(self, ips):
        return {ip: self.enrich(ip) for ip in ips}


class FakeRisk(BaseRiskChecker):
    name = "fake"

    def check(self, ip):
        # 71.85.* 视为高危（AS33333 -> 淘汰路径），其余住宅 IP 低危（入池）
        score = 90 if ip.startswith("71.85") else 5
        return {
            "ok": True, "abuse_confidence": score, "total_reports": 0,
            "distinct_users": 0, "last_reported_at": None, "is_whitelisted": False,
            "usage_type": "Fixed Line ISP", "isp": "Example ISP", "country": "US",
            "raw": {"abuseConfidenceScore": score}, "error": None,
        }


class FailingRisk(BaseRiskChecker):
    name = "failing"

    def check(self, ip):
        return {
            "ok": False, "abuse_confidence": None, "total_reports": None,
            "distinct_users": None, "last_reported_at": None, "is_whitelisted": None,
            "usage_type": None, "isp": None, "country": None, "raw": None,
            "error": "缺少 ABUSEIPDB_API_KEY",
        }


def wait_pool_idle(timeout_ticks=100):
    for _ in range(timeout_ticks):
        if not pool._run_lock.locked():
            return True
        time.sleep(0.1)
    return False


def main():
    random.seed(20241008)  # 固定随机，保证覆盖住宅/机房各类样本
    build_source(SRC_DB, total=500)

    # 注入 Mock 富化器 / 风险检查器，规避真实网络与 Token
    ingest._enricher_chain = [FakeEnricher()]
    pool._risk_client = FakeRisk()

    from app.main import app
    with TestClient(app) as client:
        # 等待首次同步完成
        for _ in range(50):
            r = client.get("/health").json()
            if r.get("last_sync_status") == "ok":
                break
            __import__("time").sleep(0.1)

        health = client.get("/health").json()
        assert health["status"] == "ok", health
        assert health["source_db"]["reachable"] is True
        print("[OK] /health:", health["status"], "nodes=", health["app_db"]["nodes"])

        tasks = client.get("/tasks").json()
        assert "ingest" in tasks, tasks
        assert tasks["ingest"]["finished_at"] is not None, tasks["ingest"]
        print(f"[OK] /tasks ingest: 阶段={tasks['ingest']['phase']} percent={tasks['ingest']['percent']}")

        stats = client.get("/stats").json()
        total = stats["nodes_total"]
        http_cnt = stats["by_protocol"]["http"] + stats["by_protocol"]["https"]
        assert total == http_cnt, stats
        assert stats["enriched"] == total, stats  # Mock 全部成功
        print(f"[OK] /stats: 总节点={total}, http={stats['by_protocol']['http']}, "
              f"https={stats['by_protocol']['https']}, "
              f"机房={stats['by_residential']['datacenter']}, "
              f"住宅={stats['by_residential']['residential']}, "
              f"住宅代理={stats['by_residential']['residential_proxy']}")

        # 列表 + 过滤
        r = client.get("/nodes", params={"protocol": "https", "limit": 5}).json()
        assert r["total"] > 0 and len(r["items"]) <= 5, r
        print(f"[OK] /nodes 过滤 https: total={r['total']}")

        # 延迟过滤 + 排序
        rl = client.get("/nodes", params={"max_latency_ms": 600, "sort": "latency_ms", "limit": 10}).json()
        assert rl["items"], rl
        assert all(i["latency_ms"] is not None and i["latency_ms"] <= 600 for i in rl["items"]), rl
        print(f"[OK] /nodes max_latency_ms + sort=latency_ms: total={rl['total']}")

        # 单节点查询
        sample = r["items"][0]
        node = client.get(f"/node/{sample['ip']}/{sample['port']}").json()
        assert node["enriched"] is True
        assert node["is_residential"] in (0, 1, 2, None), node
        raw = node["enrichment"]["raw"]
        if raw.get("as"):
            assert raw["as"]["type"] in ("hosting", "isp", "business"), node
        print(f"[OK] /node/{sample['ip']}/{sample['port']}: is_residential={node['is_residential']}, "
              f"as_type={node['enrichment']['as_type']}")

        # 运行时指标：延迟 / 出口（来自 node_latency / nodes_dynamic）
        assert node.get("latency_ms") is not None, node
        assert node.get("latencies"), node
        print(f"[OK] 延迟: {node['latency_ms']} ms {node['latencies']}, "
              f"出口={node.get('egress_region')} {node.get('egress_ip')}")

        # 陈旧节点应被 ingest 清理：所有节点都应带延迟
        alln = client.get("/nodes", params={"limit": 500}).json()
        assert alln["items"], alln
        assert all(i["latency_ms"] is not None for i in alln["items"]), \
            "存在无延迟节点（可能有陈旧节点未清理）"
        for i in alln["items"]:
            ipaddress.ip_address(i["ip"])  # 抛异常即失败：不应存在域名节点
        print(f"[OK] 全部 {len(alln['items'])} 个节点均为合法 IP 且带延迟")

        # 不存在节点 -> 404
        bad = client.get("/node/1.2.3.4/9999")
        assert bad.status_code == 404, bad.status_code
        print("[OK] 不存在节点返回 404")

        # IP 级富化
        enr = client.get(f"/enrichment/{sample['ip']}").json()
        assert enr["source"] == "fake" and enr["raw"], enr
        print(f"[OK] /enrichment/{sample['ip']}: plan={enr['plan']}")

        # 随机节点（带过滤 + 排除近期）
        rr = client.get("/nodes/random", params={"residential": "residential", "avoid_recent": True}).json()
        assert rr["is_residential"] == 1, rr
        print(f"[OK] /nodes/random?residential=residential: {rr['ip']}:{rr['port']}")

        # 无匹配 -> 404
        none = client.get("/nodes/random", params={"country": "ZZ"})
        assert none.status_code == 404, none.status_code
        print("[OK] 无匹配随机节点返回 404")

        # 鉴权：无 token 应 401
        noauth = client.post("/refresh")
        assert noauth.status_code == 401, noauth.status_code
        # 带 token
        ok = client.post("/refresh", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 202, ok.status_code
        print("[OK] /refresh 鉴权与触发 (202)")

        # ---- ASN 管理 + 节点池（L1 + L2 + L4） ------------------------- #
        auth = {"Authorization": "Bearer secret"}

        noauth = client.post("/asn", json={"asn": "AS99999", "category": "cloud"})
        assert noauth.status_code == 401, noauth.status_code
        print("[OK] /asn 写操作鉴权 401")

        # 与 FakeEnricher 输出的 ASN 对齐：12345=机房，22222/33333=住宅
        for asn, cat in [("AS12345", "cloud"), ("AS22222", "residential"), ("AS33333", "residential")]:
            r = client.post("/asn", json={"asn": asn, "category": cat, "org": asn}, headers=auth)
            assert r.status_code == 201, (r.status_code, r.text)
        print("[OK] POST /asn 新增 3 条")

        lst = client.get("/asn", params={"category": "residential"}).json()
        assert lst["total"] >= 2, lst
        print(f"[OK] GET /asn residential total={lst['total']}")

        r = client.put("/asn/AS33333", json={"enabled": False}, headers=auth)
        assert r.status_code == 200 and r.json()["enabled"] is False, r.text
        r = client.put("/asn/AS33333", json={"enabled": True}, headers=auth)
        assert r.json()["enabled"] is True, r.text
        print("[OK] PUT /asn 启用/禁用")

        imp = client.post("/asn/import", json={"category": "cloud", "text": "AS64512 Test Cloud"}, headers=auth)
        assert imp.status_code == 200 and imp.json()["count"] == 1, imp.text
        print("[OK] POST /asn/import 导入 1 条")

        # 触发池重建并等待完成
        r = client.post("/pool/rebuild", headers=auth)
        assert r.status_code == 202 and r.json()["status"] == "accepted", (r.status_code, r.text)
        assert wait_pool_idle(), "池重建超时"
        pstats = client.get("/pool/stats").json()
        assert pstats.get("total", 0) > 0, pstats
        assert pstats["by_state"]["in_pool"] > 0, pstats
        print(f"[OK] /pool/stats total={pstats['total']} states={pstats['by_state']} "
              f"avg_abuse={pstats['avg_abuse_score']}")

        pl = client.get("/pool", params={"state": "in_pool", "limit": 5}).json()
        assert pl["total"] > 0 and pl["items"], pl
        sample_pool = pl["items"][0]
        assert sample_pool["asn"] in ("AS22222", "AS33333"), sample_pool
        assert sample_pool.get("latency_ms") is not None, sample_pool
        print(f"[OK] /pool in_pool total={pl['total']} 例: {sample_pool['ip']} asn={sample_pool['asn']} "
              f"latency={sample_pool['latency_ms']}ms")

        pr = client.get("/pool/random").json()
        assert pr["state"] == "in_pool", pr
        print(f"[OK] /pool/random: {pr['ip']}:{pr['port']}")

        pd = client.get(f"/pool/{sample_pool['ip']}/0").json()
        assert pd["ip"] == sample_pool["ip"] and "checks" in pd, pd
        print("[OK] /pool/{ip}/{port} 详情含检测历史")

        # 单节点强制重检：无 token 401；带 token 同步返回最新详情
        na = client.post(f"/pool/{sample_pool['ip']}/0/recheck")
        assert na.status_code == 401, na.status_code
        rc = client.post(f"/pool/{sample_pool['ip']}/0/recheck", headers=auth)
        assert rc.status_code == 200 and rc.json()["ip"] == sample_pool["ip"], rc.text
        assert "checks" in rc.json(), rc.text
        print("[OK] POST /pool/{ip}/{port}/recheck 单节点强制重检")

        # 高危住宅节点（71.85.*）应被淘汰（POOL_EVICT_FAILURES=1）
        assert pstats["by_state"]["evicted"] > 0, pstats
        print("[OK] 高危节点进入淘汰态")

        # 回归：L2 失败(如缺少 key)不应被 next_check_at 锁死，下一轮应自动重试
        pool._risk_client = FailingRisk()
        rb = client.post("/pool/rebuild", params={"force": True}, headers=auth)
        assert rb.json()["status"] == "accepted", rb.text
        assert wait_pool_idle(), "强制重建超时"
        s1 = client.get("/pool/stats").json()
        assert s1["by_state"]["probing"] > 0, s1
        print(f"[OK] L2 失败 -> probing={s1['by_state']['probing']}")

        pool._risk_client = FakeRisk()
        rb = client.post("/pool/rebuild", headers=auth)  # force=false：probing 仍应重试
        assert rb.json()["status"] == "accepted", rb.text
        assert wait_pool_idle(), "恢复重建超时"
        s2 = client.get("/pool/stats").json()
        assert s2["by_state"]["probing"] == 0 and (s2["by_state"]["in_pool"] + s2["by_state"]["evicted"]) > 0, s2
        print(f"[OK] L2 失败重试：probing 已恢复 -> states={s2['by_state']}")

        r = client.delete("/asn/AS64512", headers=auth)
        assert r.status_code == 200, r.status_code
        print("[OK] DELETE /asn")

        # 导出（CSV / JSON）
        csv_resp = client.get("/export/nodes", params={"format": "csv"})
        assert csv_resp.status_code == 200, csv_resp.status_code
        assert csv_resp.headers["content-type"].startswith("text/csv"), csv_resp.headers
        assert csv_resp.text.startswith("\ufeff") and "ip,port,protocol" in csv_resp.text
        json_resp = client.get("/export/nodes", params={"format": "json"})
        assert json_resp.status_code == 200 and isinstance(json_resp.json(), list), json_resp.text[:200]
        pcsv = client.get("/export/pool", params={"format": "csv", "state": "in_pool"})
        assert pcsv.status_code == 200 and "state" in pcsv.text, pcsv.text[:200]
        acsv = client.get("/export/asn", params={"format": "csv"})
        assert acsv.status_code == 200 and "category" in acsv.text, acsv.text[:200]
        bad = client.get("/export/nodes", params={"format": "xml"})
        assert bad.status_code == 400, bad.status_code
        print("[OK] 导出 /export/nodes|pool|asn (csv/json) + 非法 format 返回 400")

        # ---- Resin 订阅发布（只读端点，供 Resin 远程订阅拉取） ---------- #
        # token 鉴权：缺失/错误 -> 401
        na = client.get("/resin/subscription")
        assert na.status_code == 401, na.status_code
        wrong = client.get("/resin/subscription", params={"token": "bad"})
        assert wrong.status_code == 401, wrong.status_code

        cur = client.get("/pool/stats").json()
        feed = client.get("/resin/subscription", params={"token": "feedsecret"})
        assert feed.status_code == 200, feed.status_code
        assert feed.headers.get("cache-control") == "no-store", feed.headers
        sub = feed.json()
        assert isinstance(sub, dict) and isinstance(sub.get("outbounds"), list), sub
        assert len(sub["outbounds"]) == cur["by_state"]["in_pool"], (len(sub["outbounds"]), cur)
        assert all(o.get("type") == "http" and o.get("tag", "").startswith("pool-")
                   for o in sub["outbounds"]), sub["outbounds"][:1]
        print(f"[OK] /resin/subscription token 鉴权 + in_pool 订阅 outbounds={len(sub['outbounds'])}")

        st = client.get("/resin/status", params={"token": "feedsecret"}).json()
        assert st["enabled"] is True and st["node_count"] == len(sub["outbounds"]), st
        assert st["token_required"] is True and st["content_hash"], st
        print(f"[OK] /resin/status node_count={st['node_count']} contains_credentials={st['contains_credentials']}")

        exp = client.get("/export/resin-subscription", params={"token": "feedsecret"})
        assert exp.status_code == 200 and "outbounds" in exp.text, exp.text[:120]
        print("[OK] /export/resin-subscription 手动导出")

    print("\n全部冒烟测试通过 [PASS]")


if __name__ == "__main__":
    main()
