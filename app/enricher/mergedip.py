"""离线富化器：NetworkCats/Merged-IP-Data 合并库（Merged-IP.mmdb）。

数据来源：https://github.com/NetworkCats/Merged-IP-Data

- 提供 ASN（号码 / 组织 / 域名）+ 地理 + 代理 / 机房 / VPN 标记，无需网络与 Token。
- 全量落库：MMDB 原始记录原样存入 ``raw["merged"]``；同时归一化出与 ipinfo 兼容的
  字段（``as`` / ``geo`` / ``is_hosting`` / ``is_anonymous``），供 ``extract_fields``
  统一解析与派生住宅/机房标签。
"""
import os
import time
from typing import Optional

import maxminddb

from .base import BaseIpEnricher, IpEnrichment
from .registry import register

PLAN = "offline"


def _fmt_asn(num) -> Optional[str]:
    """MMDB 中 ASN 为 uint32，统一格式化为 ``AS<number>`` 与 ipinfo 保持一致。"""
    if num is None:
        return None
    s = str(num).strip()
    if not s:
        return None
    return s if s.upper().startswith("AS") else f"AS{s}"


def _name(names) -> Optional[str]:
    """多语言名称取英文，回退任一可用值。"""
    if isinstance(names, dict):
        return names.get("en") or next(iter(names.values()), None)
    return names


def _derive_as_type(proxy) -> Optional[str]:
    """依据代理/机房标记推断 AS 类型（hosting / business）。"""
    if not isinstance(proxy, dict):
        return None
    if proxy.get("is_hosting"):
        return "hosting"
    if proxy.get("is_vpn") or proxy.get("is_tor") or proxy.get("is_proxy"):
        return "business"
    return None


def normalize_record(rec: dict) -> dict:
    """将 Merged-IP 记录归一化为 ipinfo 兼容结构（原始记录完整保留在 ``merged``）。"""
    rec = rec or {}
    asn = rec.get("asn") or {}
    country = rec.get("country") or {}
    location = rec.get("location") or {}
    proxy = rec.get("proxy") or {}
    city = rec.get("city") or {}
    subs = rec.get("subdivisions") or []
    region = None
    if isinstance(subs, list) and subs:
        region = _name((subs[0] or {}).get("names")) or (subs[0] or {}).get("iso_code")

    as_org = asn.get("autonomous_system_organization")
    return {
        "as": {
            "asn": _fmt_asn(asn.get("autonomous_system_number")),
            "name": as_org,
            "org": as_org,
            "domain": asn.get("as_domain"),
            "type": _derive_as_type(proxy),
        },
        "geo": {
            "country_code": country.get("iso_code"),
            "city": _name(city.get("names")),
            "region": region,
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "time_zone": location.get("time_zone"),
        },
        # 仅在字段存在时给出 bool，缺失保持 None，便于区分“未知”与“否”
        "is_hosting": proxy.get("is_hosting") if "is_hosting" in proxy else None,
        "is_anonymous": proxy.get("is_anonymous") if "is_anonymous" in proxy else None,
        "proxy": proxy,
        "merged": rec,
    }


@register("mergedip", lambda s: MergedIpEnricher(s.mergedip_db_path))
class MergedIpEnricher(BaseIpEnricher):
    name = "mergedip"

    def __init__(self, db_path: str, name: str = "mergedip"):
        self.db_path = db_path
        self.name = name
        self._reader = None
        self._reader_mtime = None

    def _get_reader(self):
        """打开（并在文件更新后热重载）MMDB。

        离线库会被定时刷新，通过比较文件 mtime 检测变化并重新打开，
        无需重启服务即可用上最新数据。
        """
        if not self.db_path or not os.path.exists(self.db_path):
            return None
        try:
            mtime = os.path.getmtime(self.db_path)
        except OSError:
            return None
        if self._reader is None or self._reader_mtime != mtime:
            if self._reader is not None:
                try:
                    self._reader.close()
                except Exception:  # noqa: BLE001 - 关闭旧 reader 失败可忽略
                    pass
            self._reader = maxminddb.open_database(self.db_path)
            self._reader_mtime = mtime
        return self._reader

    def enrich(self, ip: str) -> IpEnrichment:
        err = None
        rec = None
        try:
            reader = self._get_reader()
            if reader is None:
                err = f"离线库不可用: {self.db_path or '(未配置 MERGED_IP_DB_PATH)'}"
            else:
                rec = reader.get(ip)
        except Exception as e:  # noqa: BLE001 - 读取失败按单 IP 降级
            err = f"读取离线库失败: {e}"

        if err:
            return IpEnrichment(ip, self.name, PLAN, {}, int(time.time()), error=err)
        if not rec:
            return IpEnrichment(ip, self.name, PLAN, {}, int(time.time()), error="离线库中无该 IP 记录")
        return IpEnrichment(ip, self.name, PLAN, normalize_record(rec), int(time.time()))

    def enrich_batch(self, ips: list[str]) -> dict[str, IpEnrichment]:
        return {ip: self.enrich(ip) for ip in ips}

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None
