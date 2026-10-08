"""富化器包入口：导入即触发各实现类的 @register 注册。"""
from .base import BaseIpEnricher, IpEnrichment
from .registry import build, build_chain, register

# 触发注册（导入各实现模块会执行其 @register 装饰器）
from . import ipinfo  # noqa: F401
from . import mergedip  # noqa: F401

__all__ = [
    "BaseIpEnricher", "IpEnrichment", "register", "build", "build_chain",
    "ipinfo", "mergedip",
]
