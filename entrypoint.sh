#!/bin/sh
# 容器启动入口：先以 root 修正数据目录权限，再降权到非 root 用户运行。
# 背景：bind mount 的宿主机目录默认属主为 root，而服务以非 root 用户运行，
# 若无写权限会在 sqlite3.connect 时抛出 "unable to open database file"。
set -e

# 应用数据目录：确保存在且归 app 用户所有
mkdir -p /data/app
chown -R app:app /data/app

# 离线库目录（可写挂载）：确保存在且归 app 用户所有
mkdir -p /data/mmdb
chown -R app:app /data/mmdb

# 数据源目录（只读挂载）：确保存在，属主无所谓，仅需读权限
mkdir -p /data/source

# 离线库（Merged-IP.mmdb）初始下载：仅在开启自动下载时执行；失败不阻断启动
if [ "${MERGED_IP_AUTO_DOWNLOAD:-false}" = "true" ]; then
    echo "[entrypoint] 检查离线库: ${MERGED_IP_DB_PATH:-/data/mmdb/Merged-IP.mmdb}"
    if ! gosu app python /app/scripts/fetch_mmdb.py; then
        echo "[entrypoint][warn] 离线库下载失败，服务将继续启动（mergedip 将不可用）"
    fi
fi

# ASN 名单（L1 初筛）初始导入：内置精选首发集，幂等 upsert；失败不阻断启动
if [ "${ASN_SEED_ON_START:-true}" = "true" ]; then
    echo "[entrypoint] 导入/补齐 ASN 名单..."
    if ! gosu app python /app/scripts/seed_asn_registry.py; then
        echo "[entrypoint][warn] ASN 名单导入失败，服务将继续启动（ASN 管理初始为空）"
    fi
fi

# 以非 root 用户降权运行服务
exec gosu app uvicorn app.main:app --host 0.0.0.0 --port 8000
