"""L2 风险检查器：AbuseIPDB（https://www.abuseipdb.com/api）。

- 端点：GET {base}/check?ipAddress={ip}&maxAgeInDays={n}
- 头部：Key: <api key>, Accept: application/json
- 免费额度约 1000 次/天，因此调用方应只对“新 IP / 到期复检 IP”发起查询（见 pool.py 缓存）。
"""
import time
from typing import Optional

import requests

from .base import BaseRiskChecker


def _norm_bool(v) -> Optional[bool]:
    if v is None:
        return None
    return bool(v)


class AbuseIpdbClient(BaseRiskChecker):
    name = "abuseipdb"

    def __init__(self, api_key: str, base_url: str = "https://api.abuseipdb.com/api/v2",
                 max_age_days: int = 90, timeout: int = 10):
        self.api_key = api_key or ""
        self.base_url = (base_url or "https://api.abuseipdb.com/api/v2").rstrip("/")
        self.max_age_days = max(1, int(max_age_days))
        self.timeout = timeout

    def _err(self, message: str) -> dict:
        return {
            "ok": False, "abuse_confidence": None, "total_reports": None,
            "distinct_users": None, "last_reported_at": None, "is_whitelisted": None,
            "usage_type": None, "isp": None, "country": None, "raw": None,
            "error": message,
        }

    def check(self, ip: str) -> dict:
        if not self.api_key:
            return self._err("缺少 ABUSEIPDB_API_KEY")
        url = f"{self.base_url}/check"
        params = {"ipAddress": ip, "maxAgeInDays": self.max_age_days}
        headers = {"Key": self.api_key, "Accept": "application/json"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            return self._err(f"网络错误: {e}")
        if resp.status_code == 429:
            return self._err("限流 429（配额耗尽）")
        if resp.status_code == 401:
            return self._err("AbuseIPDB 鉴权失败(401)")
        if resp.status_code != 200:
            return self._err(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return self._err("JSON 解析失败")

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return self._err("返回缺少 data 字段")
        return {
            "ok": True,
            "abuse_confidence": data.get("abuseConfidenceScore"),
            "total_reports": data.get("totalReports"),
            "distinct_users": data.get("numDistinctUsers"),
            "last_reported_at": data.get("lastReportedAt"),
            "is_whitelisted": _norm_bool(data.get("isWhitelisted")),
            "usage_type": data.get("usageType"),
            "isp": data.get("isp"),
            "country": data.get("countryCode"),
            "raw": data,
            "error": None,
        }


def make_client(settings) -> AbuseIpdbClient:
    return AbuseIpdbClient(
        api_key=settings.abuseipdb_api_key,
        base_url=settings.abuseipdb_base_url,
        max_age_days=settings.abuse_max_age_days,
        timeout=getattr(settings, "abuseipdb_timeout", 8),
    )
