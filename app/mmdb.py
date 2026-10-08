"""离线 ASN 库（Merged-IP.mmdb）的获取与更新。

数据来源：NetworkCats/Merged-IP-Data 每日构建的合并 MMDB（约 92MB）。
使用标准库实现，无额外依赖：先写临时文件再原子替换，避免读到半成品。

Docker 场景下：
- 初始下载建议由 entrypoint 调 ``scripts/fetch_mmdb.py`` 完成（应用启动前就绪）；
- 运行期刷新由本模块的 ``refresh_mmdb``/``_scheduler`` 完成，enricher 按 mtime 热重载。
"""
import logging
import os
import urllib.request
from typing import Optional

logger = logging.getLogger("node-enricher.mmdb")

DEFAULT_MMDB_URL = (
    "https://github.com/NetworkCats/Merged-IP-Data/releases/latest/download/Merged-IP.mmdb"
)
# 合理性下限：正常库为几十 MB，低于此值视为下载到错误页/中断
_MIN_BYTES = 1 * 1024 * 1024
_CHUNK = 1024 * 1024


def download_mmdb(url: str, out_path: str, force: bool = False, timeout: int = 300) -> bool:
    """下载 MMDB 到 ``out_path``。

    已存在且非 ``force`` 时直接跳过。返回是否实际发生了下载。
    """
    if not out_path:
        raise ValueError("未指定输出路径（MERGED_IP_DB_PATH）")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0 and not force:
        logger.info("离线库已存在，跳过下载: %s", out_path)
        return False

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": "resin-classifier-mmdb/0.1"})
    logger.info("开始下载离线库: %s -> %s", url, out_path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
        size = os.path.getsize(tmp)
        if size < _MIN_BYTES:
            raise RuntimeError(f"下载文件异常偏小（{size} bytes），疑似失败")
        os.replace(tmp, out_path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    logger.info("离线库下载完成: %s (%d bytes)", out_path, size)
    return True


def refresh_mmdb() -> None:
    """强制拉取最新离线库（供定时刷新调用）。"""
    from .config import settings
    download_mmdb(
        settings.merged_ip_download_url,
        settings.merged_ip_db_path,
        force=True,
        timeout=settings.merged_ip_download_timeout,
    )


def ensure_mmdb() -> bool:
    """启动期确保离线库存在（仅在开启自动下载时调用）。返回是否可用。"""
    from .config import settings
    path = settings.merged_ip_db_path
    if settings.merged_ip_auto_download:
        try:
            download_mmdb(
                settings.merged_ip_download_url,
                path,
                force=settings.merged_ip_download_force,
                timeout=settings.merged_ip_download_timeout,
            )
        except Exception as e:  # noqa: BLE001 - 下载失败不应阻断服务
            logger.warning("离线库自动下载失败: %s", e)
    return bool(path) and os.path.exists(path)


def mmdb_status() -> dict:
    """返回离线库当前状态（供 /health 等展示）。"""
    from .config import settings
    path = settings.merged_ip_db_path
    exists = bool(path) and os.path.exists(path)
    return {
        "path": path,
        "exists": exists,
        "size": os.path.getsize(path) if exists else None,
        "mtime": int(os.path.getmtime(path)) if exists else None,
        "auto_download": settings.merged_ip_auto_download,
        "enabled_in_chain": "mergedip" in settings.enrichers,
    }
