"""FastAPI 应用入口：装配路由、生命周期（初始化、定时同步、首次同步）。"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .api import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("node-enricher")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 初始化应用库（建表）
    db.init_app_db()
    logger.info("应用库已初始化: %s", settings.app_db_path)

    # 1b) Resin 订阅端点安全提示（端点可能含节点凭据，公网暴露务必设 token）
    if settings.resin_feed_enabled and not settings.resin_feed_token:
        logger.warning(
            "已启用 Resin 订阅端点 GET /resin/subscription 但未设置 RESIN_FEED_TOKEN；"
            "公网暴露时将无鉴权，请设置 token 或仅限内网访问"
        )

    # 2) 启动定时同步调度
    scheduler = asyncio.create_task(_scheduler()) if settings.ingest_interval > 0 else None

    # 2b) 启动节点池漏斗调度（L1 + L2 + L4）
    pool_scheduler = asyncio.create_task(_pool_scheduler()) if settings.pool_enabled else None

    # 2c) 离线库（Merged-IP.mmdb）运行期刷新调度（初始下载由 entrypoint 完成）
    mmdb_scheduler = (
        asyncio.create_task(_mmdb_scheduler())
        if settings.merged_ip_auto_download and settings.merged_ip_download_interval > 0
        else None
    )

    # 3) 首次同步（后台线程，避免阻塞启动）
    from .ingest import schedule_ingest
    schedule_ingest()
    logger.info("已触发首次同步")

    yield

    for task in (scheduler, pool_scheduler, mmdb_scheduler):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _scheduler():
    from .ingest import schedule_ingest
    while True:
        await asyncio.sleep(settings.ingest_interval)
        try:
            schedule_ingest()
        except Exception as e:  # noqa: BLE001
            logger.warning("定时同步触发失败: %s", e)


async def _pool_scheduler():
    """池漏斗调度：先跑一轮（等待首次 ingest 落库），随后按 POOL_INTERVAL 周期执行。"""
    from .pool import schedule_pool_sync
    await asyncio.sleep(2)  # 给首次 ingest 一点时间
    while True:
        try:
            schedule_pool_sync()
        except Exception as e:  # noqa: BLE001
            logger.warning("池漏斗触发失败: %s", e)
        if settings.pool_interval <= 0:
            break
        await asyncio.sleep(settings.pool_interval)


async def _mmdb_scheduler():
    """离线库刷新调度：按 MERGED_IP_DOWNLOAD_INTERVAL 周期强制拉取最新库。

    下载在后台线程执行；完成后 enricher 会按文件 mtime 热重载，无需重启。
    """
    from .mmdb import refresh_mmdb
    while True:
        await asyncio.sleep(settings.merged_ip_download_interval)
        try:
            await asyncio.to_thread(refresh_mmdb)
            logger.info("离线库已刷新")
        except Exception as e:  # noqa: BLE001
            logger.warning("离线库刷新失败: %s", e)


app = FastAPI(
    title="代理节点住宅/机房识别服务",
    description="基于 Resin 数据源的只读旁路分析组件：抽取 server 为 IP 的全部类型节点（健康且低延迟），富化并识别住宅/机房属性。",
    version=settings.version,
    lifespan=lifespan,
    default_response_class=JSONResponse,
)

app.include_router(router)

# 简单数据看板（静态页面），同源调用现有 REST API
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")


@app.get("/")
def root():
    return {"service": "node-enricher", "version": settings.version, "docs": "/docs", "ui": "/ui"}
