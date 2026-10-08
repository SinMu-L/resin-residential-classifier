"""配置项：全部来自环境变量，缺失时使用 PRD 第 7 节定义的默认值。"""
import os

from dotenv import load_dotenv

# 本地开发时从 .env 加载配置；已存在的环境变量优先（Docker env_file 行为不受影响）
load_dotenv()


def _get_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """服务配置（单例）。env 在导入时即读取，运行期不热更新。"""

    def __init__(self) -> None:
        self.source_db_path: str = os.getenv("SOURCE_DB_PATH", "/data/source/cache.db")
        self.app_db_path: str = os.getenv("APP_DB_PATH", "/data/app/nodes.sqlite")

        # 数据源节点准入过滤（FR-2.1）：允许抽取的节点类型。
        # "all" / "*" / 空 = 不限制；否则为逗号分隔的允许列表（如 "http,vless,trojan"）。
        _types = os.getenv("SOURCE_INCLUDE_TYPES", "all").strip().lower()
        self.source_include_types = (
            None if _types in ("", "all", "*")
            else {t.strip() for t in _types.split(",") if t.strip()}
        )
        # 健康准入：未熔断 + 有出口 IP + 有延迟样本（对齐 Resin 可路由口径）。
        self.source_require_healthy: bool = _get_bool(
            os.getenv("SOURCE_REQUIRE_HEALTHY", "true"), True
        )
        # 延迟上限（毫秒）；<=0 表示不启用延迟门槛。
        self.source_max_latency_ms: float = float(os.getenv("SOURCE_MAX_LATENCY_MS", "300"))

        # 启用的富化器列表（按顺序构成回退链），逗号分隔
        self.enrichers: list[str] = [
            e.strip() for e in os.getenv("ENRICHERS", "mergedip,ipinfo").split(",") if e.strip()
        ]

        self.enrich_cache_ttl: int = int(os.getenv("ENRICH_CACHE_TTL", "86400"))
        self.ipinfo_token: str = os.getenv("IPINFO_TOKEN", "")
        self.ipinfo_base_url: str = os.getenv("IPINFO_BASE_URL", "https://api.ipinfo.io").rstrip("/")
        self.ipinfo_batch_size: int = int(os.getenv("IPINFO_BATCH_SIZE", "100"))

        self.ingest_interval: int = int(os.getenv("INGEST_INTERVAL", "300"))

        self.api_host: str = os.getenv("API_HOST", "0.0.0.0")
        self.api_port: int = int(os.getenv("API_PORT", "8000"))

        self.refresh_token: str = os.getenv("REFRESH_TOKEN", "")

        self.ip2location_db_path: str = os.getenv("IP2LOCATION_DB_PATH", "")

        # 离线 ASN 库：NetworkCats/Merged-IP-Data 的 Merged-IP.mmdb
        self.mergedip_db_path: str = os.getenv("MERGED_IP_DB_PATH", "/data/source/Merged-IP.mmdb")
        # 离线库自动下载/更新（默认关闭；启用 mergedip 时建议开启）
        self.merged_ip_auto_download: bool = _get_bool(
            os.getenv("MERGED_IP_AUTO_DOWNLOAD", "true"), True
        )
        self.merged_ip_download_url: str = os.getenv(
            "MERGED_IP_DOWNLOAD_URL",
            "https://github.com/NetworkCats/Merged-IP-Data/releases/latest/download/Merged-IP.mmdb",
        )
        self.merged_ip_download_force: bool = _get_bool(
            os.getenv("MERGED_IP_DOWNLOAD_FORCE", "false"), False
        )
        self.merged_ip_download_timeout: int = int(os.getenv("MERGED_IP_DOWNLOAD_TIMEOUT", "300"))
        # 运行期刷新间隔（秒），0=仅启动时检查一次
        self.merged_ip_download_interval: int = int(os.getenv("MERGED_IP_DOWNLOAD_INTERVAL", "0"))

        # 近期已返回 IP 环形缓冲的参数（用于 /nodes/random?avoid_recent）
        self.recent_ttl: int = int(os.getenv("RECENT_TTL", "60"))
        self.recent_maxlen: int = int(os.getenv("RECENT_MAXLEN", "1000"))

        # -- 高质量节点池（L1 ASN 初筛 / L2 AbuseIPDB / L4 复检淘汰） -------- #
        self.pool_enabled: bool = _get_bool(os.getenv("POOL_ENABLED", "true"), True)
        # 池漏斗调度周期（秒），0=关闭定时调度（仍可手动 /pool/rebuild）。
        # 注意：每次 ingest 结束也会触发一次池刷新，故实际频率 ≥ INGEST_INTERVAL。
        self.pool_interval: int = int(os.getenv("POOL_INTERVAL", "1800"))
        # L1：未知 ASN 是否直接拒绝（严格白名单模式）
        self.pool_l1_strict: bool = _get_bool(os.getenv("POOL_L1_STRICT", "true"), True)

        self.abuseipdb_api_key: str = os.getenv("ABUSEIPDB_API_KEY", "")
        self.abuseipdb_base_url: str = os.getenv(
            "ABUSEIPDB_BASE_URL", "https://api.abuseipdb.com/api/v2"
        ).rstrip("/")
        # L2 放行阈值：abuseConfidenceScore < 该值即通过
        self.pool_abuse_max_score: int = int(os.getenv("POOL_ABUSE_MAX_SCORE", "30"))
        # L2 风险查询缓存 TTL（秒）。有效复检间隔 = max(本值, POOL_RECHECK_IN_POOL)，
        # 二者需同时到期才会重新送检 AbuseIPDB；日调用量 ≈ N × 86400 / 有效间隔。
        self.abuse_cache_ttl: int = int(os.getenv("ABUSE_CACHE_TTL", "21600"))
        self.abuse_max_age_days: int = int(os.getenv("ABUSE_MAX_AGE_DAYS", "90"))
        self.abuseipdb_timeout: int = int(os.getenv("ABUSEIPDB_TIMEOUT", "8"))
        self.abuseipdb_concurrency: int = int(os.getenv("ABUSEIPDB_CONCURRENCY", "8"))

        # L4 淘汰与复检间隔
        self.pool_evict_failures: int = int(os.getenv("POOL_EVICT_FAILURES", "2"))
        # in_pool 复检间隔（秒）：6h。与 ABUSE_CACHE_TTL 共同决定 L2 调用量
        self.pool_recheck_in_pool: int = int(os.getenv("POOL_RECHECK_IN_POOL", "21600"))
        self.pool_recheck_probation: int = int(os.getenv("POOL_RECHECK_PROBATION", "3600"))
        self.pool_cooldown: int = int(os.getenv("POOL_COOLDOWN", "604800"))

        # -- Resin 订阅发布（只读：Resin 主动拉取，本服务不写 Resin） -------- #
        # 是否开放 GET /resin/subscription 订阅端点（供 Resin 作为远程订阅拉取）
        self.resin_feed_enabled: bool = _get_bool(os.getenv("RESIN_FEED_ENABLED", "false"), False)
        # 访问订阅端点的 token（公网暴露时必填；留空=不鉴权，仅限内网）
        self.resin_feed_token: str = os.getenv("RESIN_FEED_TOKEN", "")
        # 纳入订阅的池状态（逗号分隔），默认仅 in_pool
        self.resin_feed_states: list[str] = [
            s.strip() for s in os.getenv("RESIN_FEED_STATES", "in_pool").split(",") if s.strip()
        ]
        # 建议在 Resin 中使用的订阅名（仅用于文档/状态展示）
        self.resin_feed_subscription_name: str = os.getenv(
            "RESIN_FEED_SUBSCRIPTION_NAME", "resin-residential-pool"
        )

        self.version: str = "0.1.0"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.refresh_token)


settings = Settings()
