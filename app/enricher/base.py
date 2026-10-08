"""IP 富化器统一接口（PRD 第 5 节）。

新增富化源 = 继承 BaseIpEnricher 实现 enrich / enrich_batch，并用 @register 注册，
随后在 ENRICHERS 环境变量中加入其名字即可，调用链路零改动（AC-5）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class IpEnrichment:
    ip: str
    source: str                 # 富化源名称，如 "ipinfo"
    plan: str                   # 档位：lite / core / plus / max / unknown
    raw: dict                   # 富化源返回的完整 JSON（全量，不裁剪）
    enriched_at: int            # Unix 秒
    error: Optional[str] = None  # 富化失败原因；成功为 None


class BaseIpEnricher(ABC):
    name: str = "base"

    @abstractmethod
    def enrich(self, ip: str) -> IpEnrichment:
        """对单个 IP 富化。"""
        ...

    @abstractmethod
    def enrich_batch(self, ips: list[str]) -> dict[str, IpEnrichment]:
        """批量富化，返回 {ip: IpEnrichment}。"""
        ...
