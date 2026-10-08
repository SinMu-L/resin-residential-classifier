"""默认富化器：ipinfo.io（PRD 5.2 / 5.3 / 5.4）。

- 端点：GET  https://api.ipinfo.io/lookup/{ip}?token=...
- 批量：POST https://api.ipinfo.io/batch?token=... （传入 IP 列表）
- 回退：若新统一端点异常/结构不符，回退到 https://ipinfo.io/{ip}?token=
- 全量落库：raw 原样保存；关键字段拆分见 extract_fields
- 派生规则见 derive_residential
"""
import time
from typing import Optional

import requests

from .base import BaseIpEnricher, IpEnrichment
from .registry import register


def _as_bool(v) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


def _as_coords(geo: dict, raw: dict):
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (ValueError, TypeError):
            pass
    # 兼容旧版 ipinfo 的 "loc": "34.05,-118.24"
    loc = raw.get("loc")
    if isinstance(loc, str) and "," in loc:
        try:
            a, b = loc.split(",", 1)
            return float(a), float(b)
        except (ValueError, TypeError):
            pass
    return None, None


def extract_fields(raw: dict) -> dict:
    """从 ipinfo 返回的（可能是嵌套或扁平的）JSON 中拆分关键字段并派生住宅/机房标签。"""
    geo = raw.get("geo") or {}
    asinfo = raw.get("as") or {}
    anon = raw.get("anonymous") or {}

    asn = asinfo.get("asn") or raw.get("asn")
    as_name = asinfo.get("name") or asinfo.get("org") or raw.get("org")
    as_type = asinfo.get("type") or raw.get("type")

    is_hosting = _as_bool(raw.get("is_hosting"))
    is_anonymous = _as_bool(raw.get("is_anonymous"))
    is_res_proxy = _as_bool(anon.get("is_res_proxy"))

    country_code = raw.get("country") or geo.get("country_code") or geo.get("country")
    city = geo.get("city") or raw.get("city")
    region = geo.get("region") or raw.get("region")

    latitude, longitude = _as_coords(geo, raw)

    fields = {
        "asn": asn,
        "as_name": as_name,
        "as_type": as_type,
        "is_hosting": is_hosting,
        "is_anonymous": is_anonymous,
        "is_res_proxy": is_res_proxy,
        "country_code": country_code,
        "city": city,
        "region": region,
        "latitude": latitude,
        "longitude": longitude,
    }
    fields["is_residential"] = derive_residential(fields)
    return fields


def derive_residential(f: dict) -> Optional[int]:
    """基于全量字段派生（PRD 5.4，优先级自上而下）：

    - is_hosting==true 或 as.type=="hosting"            -> 0 机房
    - anonymous.is_res_proxy==true                       -> 2 住宅代理
    - as.type=="isp" 且 is_anonymous==false              -> 1 住宅
    - 其它 / 字段缺失                                    -> None 未知
    """
    if f.get("is_hosting") is True or f.get("as_type") == "hosting":
        return 0
    if f.get("is_res_proxy") is True:
        return 2
    if f.get("as_type") == "isp" and f.get("is_anonymous") is False:
        return 1
    return None


def detect_plan(raw: dict) -> str:
    """按返回字段尽力推断档位（仅作参考标签）。"""
    if "anonymous" in raw:
        return "max" if "last_seen" in raw else "plus"
    if "as" in raw or "is_hosting" in raw or "geo" in raw:
        return "core"
    if "country" in raw or "asn" in raw:
        return "lite"
    return "unknown"


def _make_enrichment(ip, source, plan, raw, error=None):
    return IpEnrichment(
        ip=ip,
        source=source,
        plan=plan,
        raw=raw if raw is not None else {},
        enriched_at=int(time.time()),
        error=error,
    )


@register(
    "ipinfo",
    lambda s: IpinfoEnricher(s.ipinfo_token, s.ipinfo_base_url, s.ipinfo_batch_size),
)
class IpinfoEnricher(BaseIpEnricher):
    name = "ipinfo"

    def __init__(self, token: str, base_url: str, batch_size: int = 100, timeout: int = 10):
        self.token = token
        self.base_url = (base_url or "https://api.ipinfo.io").rstrip("/")
        self.batch_size = max(1, int(batch_size))
        self.timeout = timeout
        self.fallback_base = "https://ipinfo.io"

    # -- 内部 HTTP -------------------------------------------------------- #
    def _headers(self):
        return {"Accept": "application/json", "User-Agent": "resin-node-enricher/0.1"}

    def _get_json(self, url: str) -> tuple[Optional[dict], Optional[str]]:
        try:
            resp = requests.get(url, timeout=self.timeout, headers=self._headers())
        except requests.RequestException as e:
            return None, f"网络错误: {e}"
        if resp.status_code == 429:
            return None, "限流 429"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        try:
            return resp.json(), None
        except ValueError:
            return None, "JSON 解析失败"

    def _lookup(self, ip: str) -> tuple[Optional[dict], Optional[str]]:
        if not self.token:
            return None, "缺少 IPINFO_TOKEN"
        # 1) 新统一端点
        data, err = self._get_json(f"{self.base_url}/lookup/{ip}?token={self.token}")
        if data is not None:
            return data, None
        # 2) 回退到旧端点
        data2, err2 = self._get_json(f"{self.fallback_base}/{ip}?token={self.token}")
        if data2 is not None:
            return data2, None
        return None, err or err2

    # -- 公共接口 -------------------------------------------------------- #
    def enrich(self, ip: str) -> IpEnrichment:
        data, err = self._lookup(ip)
        if err:
            return _make_enrichment(ip, self.name, "unknown", {}, error=err)
        if not isinstance(data, dict):
            return _make_enrichment(ip, self.name, "unknown", {}, error="返回非 JSON 对象")
        plan = detect_plan(data)
        return _make_enrichment(ip, self.name, plan, data)

    def enrich_batch(self, ips: list[str]) -> dict[str, IpEnrichment]:
        out: dict[str, IpEnrichment] = {}
        if not self.token:
            for ip in ips:
                out[ip] = _make_enrichment(ip, self.name, "unknown", {}, error="缺少 IPINFO_TOKEN")
            return out

        # 分批调用 /batch
        for i in range(0, len(ips), self.batch_size):
            chunk = ips[i : i + self.batch_size]
            out.update(self._batch_once(chunk))

        # 对批量中失败的逐个回退单查（含旧端点）
        failed = [ip for ip, e in out.items() if e.error]
        for ip in failed:
            out[ip] = self.enrich(ip)
        return out

    def _batch_once(self, chunk: list[str]) -> dict[str, IpEnrichment]:
        url = f"{self.base_url}/batch?token={self.token}"
        try:
            resp = requests.post(
                url, json=chunk, timeout=self.timeout, headers=self._headers()
            )
        except requests.RequestException as e:
            return {ip: _make_enrichment(ip, self.name, "unknown", {}, error=f"网络错误: {e}") for ip in chunk}

        if resp.status_code == 429:
            return {ip: _make_enrichment(ip, self.name, "unknown", {}, error="限流 429") for ip in chunk}
        if resp.status_code != 200:
            return {ip: _make_enrichment(ip, self.name, "unknown", {}, error=f"HTTP {resp.status_code}") for ip in chunk}

        try:
            body = resp.json()
        except ValueError:
            return {ip: _make_enrichment(ip, self.name, "unknown", {}, error="JSON 解析失败") for ip in chunk}

        # 期望结构：{ip: {...}}；个别实现可能返回列表，做兼容
        if isinstance(body, list):
            body = {item.get("ip") or item.get("query"): item for item in body if isinstance(item, dict)}

        result: dict[str, IpEnrichment] = {}
        for ip in chunk:
            data = body.get(ip)
            if isinstance(data, dict) and data:
                result[ip] = _make_enrichment(ip, self.name, detect_plan(data), data)
            else:
                # 批量未返回该 IP，标记待单查
                result[ip] = _make_enrichment(ip, self.name, "unknown", {}, error="批量未返回")
        return result
