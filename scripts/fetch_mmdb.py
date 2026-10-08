#!/usr/bin/env python
"""下载/更新 Merged-IP.mmdb 离线库（NetworkCats/Merged-IP-Data）。

用法：
    python scripts/fetch_mmdb.py [--out PATH] [--url URL] [--force] [--timeout SEC]

参数缺省时读取环境变量：
    MERGED_IP_DB_PATH / MERGED_IP_DOWNLOAD_URL / MERGED_IP_DOWNLOAD_FORCE / MERGED_IP_DOWNLOAD_TIMEOUT

Docker 场景由 entrypoint 在应用启动前调用；也可手动执行。
退出码：0=成功（下载或已存在）；1=失败。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mmdb import DEFAULT_MMDB_URL, download_mmdb  # noqa: E402


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 Merged-IP.mmdb 离线库")
    parser.add_argument("--url", default=os.getenv("MERGED_IP_DOWNLOAD_URL", DEFAULT_MMDB_URL))
    parser.add_argument("--out", default=os.getenv("MERGED_IP_DB_PATH", "/data/mmdb/Merged-IP.mmdb"))
    parser.add_argument("--force", action="store_true", default=_env_bool("MERGED_IP_DOWNLOAD_FORCE"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("MERGED_IP_DOWNLOAD_TIMEOUT", "300")))
    args = parser.parse_args()

    try:
        changed = download_mmdb(args.url, args.out, force=args.force, timeout=args.timeout)
    except Exception as e:  # noqa: BLE001
        print(f"[error] 离线库下载失败: {e}", file=sys.stderr)
        return 1
    print("[ok] 已更新" if changed else "[skip] 已存在，无需下载")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
