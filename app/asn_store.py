"""ASN 名单存储（L1 初筛）：ASN 归一化、内存缓存、分类与增删改。

名单不再落 txt，统一存于应用库 ``asn_registry`` 表（category = cloud | residential）。
读路径（classify）走内存缓存；任何写操作后调用 ``invalidate()`` 使缓存失效。
"""
import re
import threading
import time
from typing import Optional

from . import db

ASN_RE = re.compile(r"^AS\d+$", re.IGNORECASE)

_CATEGORIES = ("cloud", "residential")

_cache_lock = threading.Lock()
_cache = {
    "cloud": set(),
    "residential": set(),
    "loaded": False,
}


def normalize_asn(value) -> Optional[str]:
    """把 16509 / 'as16509' / 'AS16509' 统一为 'AS16509'；非法返回 None。"""
    if value is None:
        return None
    s = str(value).strip().upper()
    if not s:
        return None
    if not s.startswith("AS"):
        s = "AS" + s
    return s if ASN_RE.match(s) else None


def is_valid_category(category: str) -> bool:
    return category in _CATEGORIES


def load(force: bool = False) -> None:
    """加载启用中的 ASN 名单到内存缓存。"""
    with _cache_lock:
        if _cache["loaded"] and not force:
            return
        _cache["cloud"] = {r["asn"] for r in db.get_enabled_asn("cloud")}
        _cache["residential"] = {r["asn"] for r in db.get_enabled_asn("residential")}
        _cache["loaded"] = True


def invalidate() -> None:
    with _cache_lock:
        _cache["loaded"] = False


def classify(asn: Optional[str]) -> Optional[str]:
    """返回 'cloud' / 'residential' / None（未知）。asn 需为归一化值。"""
    if not asn:
        return None
    load()
    with _cache_lock:
        if asn in _cache["cloud"]:
            return "cloud"
        if asn in _cache["residential"]:
            return "residential"
    return None


def counts() -> dict:
    load()
    with _cache_lock:
        return {"cloud": len(_cache["cloud"]), "residential": len(_cache["residential"])}


# --------------------------------------------------------------------------- #
# 增删改（写后失效缓存）                                                       #
# --------------------------------------------------------------------------- #

def upsert(asn: str, category: str, org: Optional[str] = None,
           country: Optional[str] = None, note: Optional[str] = None,
           source: str = "manual", enabled: bool = True) -> str:
    normalized = normalize_asn(asn)
    if normalized is None:
        raise ValueError(f"非法 ASN: {asn}")
    if not is_valid_category(category):
        raise ValueError(f"非法 category: {category}")
    now = int(time.time())
    db.upsert_asn({
        "asn": normalized,
        "category": category,
        "org": org,
        "country": country,
        "source": source,
        "note": note,
        "enabled": 1 if enabled else 0,
        "created_at": now,
        "updated_at": now,
    })
    invalidate()
    return normalized


def update(asn: str, fields: dict) -> bool:
    normalized = normalize_asn(asn)
    if normalized is None:
        raise ValueError(f"非法 ASN: {asn}")
    if "category" in fields and not is_valid_category(fields["category"]):
        raise ValueError(f"非法 category: {fields['category']}")
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    fields["updated_at"] = int(time.time())
    ok = db.update_asn(normalized, fields)
    invalidate()
    return ok


def delete(asn: str) -> bool:
    normalized = normalize_asn(asn)
    if normalized is None:
        raise ValueError(f"非法 ASN: {asn}")
    ok = db.delete_asn(normalized)
    invalidate()
    return ok


def import_lines(category: str, text: str, source: str = "import") -> int:
    """批量导入：每行 ``AS123 机构名(可选)`` 或 ``AS123,机构名``。返回导入条数。"""
    if not is_valid_category(category):
        raise ValueError(f"非法 category: {category}")
    now = int(time.time())
    n = 0
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[,\s]+", line, maxsplit=1)
        asn = normalize_asn(parts[0])
        if asn is None:
            continue
        org = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        db.upsert_asn({
            "asn": asn,
            "category": category,
            "org": org,
            "country": None,
            "source": source,
            "note": None,
            "enabled": 1,
            "created_at": now,
            "updated_at": now,
        })
        n += 1
    invalidate()
    return n
