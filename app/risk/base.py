"""风险检查器统一接口。

check(ip) 返回归一化字典，字段缺失用 None（便于区分“未知”与“否”）：
    {
        "ok": bool,                 # 本次查询是否成功（非网络/解析错误）
        "abuse_confidence": int,    # 0-100 风险分
        "total_reports": int,
        "distinct_users": int,
        "last_reported_at": str,
        "is_whitelisted": bool,
        "usage_type": str,
        "isp": str,
        "country": str,
        "raw": dict,                # 原始返回
        "error": str | None,
    }
"""
from abc import ABC, abstractmethod


class BaseRiskChecker(ABC):
    name = "base"

    @abstractmethod
    def check(self, ip: str) -> dict:
        ...
