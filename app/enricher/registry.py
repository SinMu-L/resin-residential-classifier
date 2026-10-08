"""富化器注册表与回退链（PRD 5.5）。

注册方式：在富化器类上使用 @register(name, factory)，其中 factory(settings) -> 实例。
build(name, settings) 用于按配置实例化；build_chain(settings) 按 ENRICHERS 顺序构造回退链。
"""
from functools import partial
from typing import Callable, Optional

_Factory = Callable[["Settings"], Optional[object]]

_FACTORIES: dict[str, _Factory] = {}


def register(name: str, factory: _Factory):
    """装饰器：将富化器类注册到注册表。"""
    def deco(cls):
        _FACTORIES[name] = factory
        return cls
    return deco


def build(name: str, settings) -> Optional[object]:
    factory = _FACTORIES.get(name)
    if factory is None:
        return None
    try:
        return factory(settings)
    except Exception:  # noqa: BLE001 - 实例化失败视为该源不可用
        return None


def build_chain(settings) -> list:
    chain = []
    for name in settings.enrichers:
        inst = build(name, settings)
        if inst is not None:
            chain.append(inst)
    return chain


# 避免循环引用：延迟导入 Settings 类型提示
from ..config import Settings  # noqa: E402
