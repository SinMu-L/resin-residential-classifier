# 代理节点住宅/机房识别服务

基于 [Resinat/Resin](https://github.com/Resinat/Resin) 单独拆出的 **只读旁路分析组件**。它从 Resin 运行目录的 `cache.db` 中**只读抽取 http/https 代理节点**，调用 [ipinfo.io](https://ipinfo.io) 做 IP 情报富化（**全量字段落库**），并派生住宅（residential）/ 机房（datacenter）等属性，最终通过 REST API 按 `ip:port` 对外提供查询。

> 定位：零侵入、只读、可独立部署。不修改、不回写 Resin 的任何数据（AC-1）。

---

## 1. 技术栈

Python 3.11 · FastAPI · SQLite · Docker / Docker Compose

---

## 2. 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 应用 + 生命周期（初始化 / 定时同步 / 首次同步）
│   ├── config.py          # 配置（环境变量，PRD 第 7 节）
│   ├── db.py              # SQLite 建表、读写、索引（PRD 第 6 节 DDL）
│   ├── models.py          # 响应构建（节点 / 富化 / 错误信封）
│   ├── source.py          # 数据源只读接入（mode=ro 读取 nodes_static + 过滤归一化）
│   ├── ingest.py          # 同步管线编排（读取→过滤→批量富化→缓存→Upsert→统计）
│   ├── recent_buffer.py   # /nodes/random?avoid_recent 的近期 IP 环形缓冲
│   ├── asn_store.py       # ASN 名单（L1 初筛）：缓存 + 分类 + 增删改
│   ├── pool.py            # 高质量节点池漏斗（L1 ASN → L2 AbuseIPDB → L4 复检淘汰）
│   ├── resin_feed.py      # Resin 订阅内容生成（sing-box JSON，只读发布）
│   ├── mmdb.py            # 离线库 Merged-IP.mmdb 的下载/更新（stdlib）
│   ├── enricher/
│   │   ├── base.py        # BaseIpEnricher 接口 + IpEnrichment 数据类（PRD 5.1）
│   │   ├── registry.py    # 富化器注册表 + 回退链
│   │   ├── ipinfo.py      # 默认实现 IpinfoEnricher（lookup/batch、全量落库、住宅/机房派生）
│   │   ├── mergedip.py    # 离线实现 MergedIpEnricher（Merged-IP.mmdb，ASN/地理/代理标记）
│   │   └── __init__.py
│   ├── risk/
│   │   ├── base.py        # BaseRiskChecker 风险检查器接口（L2）
│   │   ├── abuseipdb.py   # AbuseIpdbClient（abuseConfidenceScore 等）
│   │   └── __init__.py
│   └── static/            # 简单数据看板（原生 HTML/JS，无构建，挂载于 /ui）
│       ├── index.html     # 节点总览
│       ├── pool.html/.js  # 节点池视图
│       ├── asn.html/.js   # ASN 名单管理（增删改）
│       ├── common.js      # 池/ASN 页共用工具
│       ├── progress.js    # 后台任务进度条（轮询 /tasks）
│       ├── app.js
│       └── style.css
├── scripts/
│   ├── make_sample_source.py   # 生成示例 Resin 数据源（无需真实环境即可联调）
│   ├── seed_asn_registry.py    # 初始化 ASN 名单（精选首发集）
│   └── fetch_mmdb.py           # 下载/更新 Merged-IP.mmdb 离线库
├── tests/
│   └── smoke_test.py      # 离线冒烟测试（Mock 富化器，验证关键验收点）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## 3. 快速开始

### 3.1 本地运行（无需 Docker，使用 uv）

```bash
# 1) 安装依赖（uv 自动管理 .venv 与 uv.lock）
uv sync

# 2) 生成示例数据源（脚本默认路径为 /workspace/...，必须显式传参）
uv run python scripts/make_sample_source.py ./resin-data/cache/cache.db

# 3) 准备配置（服务启动时自动加载 .env；已存在的环境变量优先）
cp .env.example .env          # 按需填入 IPINFO_TOKEN，并改为本地路径：
#   SOURCE_DB_PATH=./resin-data/cache/cache.db
#   APP_DB_PATH=./app-data/nodes.sqlite
#   INGEST_INTERVAL=0
#   IPINFO_TOKEN=你的token

# 4) 启动服务
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后访问：
- 数据看板：http://localhost:8000/ui
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

> 说明：应用库 `APP_DB_PATH` 在启动时自动建表，无需手动准备；`INGEST_INTERVAL=0` 时首次同步仍会执行，之后可手动 `POST /refresh`。

### 3.2 Docker / Compose（推荐交付方式）

```bash
# 准备真实 Resin 数据目录，例如 ./resin-data/cache/cache.db
cp .env.example .env          # 填入 IPINFO_TOKEN
docker compose up -d          # 只读挂载数据源，应用库持久化到 ./app-data
```

`docker-compose.yml` 已配置：
- `./resin-data/cache` → `/data/source` **只读 (`ro`)** 挂载（文件系统级防误写，AC-1）
- `./app-data` → `/data/app` 读写持久化
- `./mmdb-data` → `/data/mmdb` 读写持久化（离线库 `Merged-IP.mmdb`，见 3.3）
- `healthcheck` 调用 `/health`

### 3.3 离线 ASN 库（Merged-IP.mmdb）

无需联网/Token 即可为节点补齐 **ASN（号码 / 组织 / 域名）** 及地理、代理/机房标记，数据来自
[NetworkCats/Merged-IP-Data](https://github.com/NetworkCats/Merged-IP-Data) 每日构建的合并 MMDB（约 92MB）。

为避免把 92MB 数据烤进镜像，Docker 场景使用**独立可写卷 `/data/mmdb`**，并支持自动下载/更新：

**方式 A：自动下载（推荐，Docker）**

在 `.env` 中：
```bash
ENRICHERS=mergedip,ipinfo          # 优先离线库；未命中再回退在线 ipinfo
MERGED_IP_AUTO_DOWNLOAD=true       # 启动时由 entrypoint 自动下载到 /data/mmdb
MERGED_IP_DOWNLOAD_INTERVAL=86400  # 运行期每天刷新一次（0=仅启动检查）
```
然后 `docker compose up -d`。首次启动会下载离线库到 `./mmdb-data/Merged-IP.mmdb`；
后续每 `MERGED_IP_DOWNLOAD_INTERVAL` 秒强制拉取最新库，enricher 按文件 mtime **热重载**，无需重启。
`MERGED_IP_DOWNLOAD_URL` 可改为镜像/代理地址以适配受限网络。

**方式 B：手动放置（本地/离线）**

```bash
# 下载 / 拷贝到自定义路径，再通过 MERGED_IP_DB_PATH 指向它
python scripts/fetch_mmdb.py --out ./resin-data/cache/Merged-IP.mmdb
# 或
curl -L --fail -o ./mmdb-data/Merged-IP.mmdb \
  https://github.com/NetworkCats/Merged-IP-Data/releases/latest/download/Merged-IP.mmdb
```
```bash
ENRICHERS=mergedip,ipinfo
MERGED_IP_DB_PATH=./resin-data/cache/Merged-IP.mmdb   # Docker 内默认 /data/mmdb/Merged-IP.mmdb
```

> 回退链语义：某个 IP 被链条中**第一个成功返回**的富化器处理后即停止，后续富化器不再处理该 IP。
> 因此 `mergedip,ipinfo` 时命中离线库的 IP 不会调用 ipinfo；若更看重 ipinfo 的住宅判定，可改为 `ipinfo,mergedip`。

---

## 4. 配置项（环境变量）

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| `SOURCE_DB_PATH` | Resin `cache.db` 路径（只读） | `/data/source/cache.db` |
| `APP_DB_PATH` | 应用 SQLite 路径 | `/data/app/nodes.sqlite` |
| `ENRICHERS` | 启用的富化器（按顺序回退，逗号分隔），可选 `ipinfo` / `mergedip` | `ipinfo` |
| `MERGED_IP_DB_PATH` | 离线 ASN 库路径（NetworkCats/Merged-IP-Data 的 `Merged-IP.mmdb`） | `/data/source/Merged-IP.mmdb`（compose 覆盖为 `/data/mmdb/Merged-IP.mmdb`） |
| `MERGED_IP_AUTO_DOWNLOAD` | 启动时自动下载离线库（entrypoint 执行） | `false` |
| `MERGED_IP_DOWNLOAD_URL` | 离线库下载地址（可换镜像/代理） | GitHub Release latest |
| `MERGED_IP_DOWNLOAD_FORCE` | 强制重新下载（忽略已存在文件） | `false` |
| `MERGED_IP_DOWNLOAD_TIMEOUT` | 单次下载超时（秒） | `300` |
| `MERGED_IP_DOWNLOAD_INTERVAL` | 运行期刷新间隔（秒），0=仅启动检查一次 | `0` |
| `ENRICH_CACHE_TTL` | IP 富化缓存有效期（秒） | `86400` |
| `IPINFO_TOKEN` | ipinfo.io API Token（启用 ipinfo 时必填） | 空 |
| `IPINFO_BASE_URL` | ipinfo API 基址 | `https://api.ipinfo.io` |
| `IPINFO_BATCH_SIZE` | 单次 `/batch` 批量富化的 IP 数 | `100` |
| `INGEST_INTERVAL` | 自动同步间隔（秒），0=关闭 | `300` |
| `API_HOST` / `API_PORT` | 监听地址 | `0.0.0.0:8000` |
| `REFRESH_TOKEN` | `POST /refresh` 鉴权（可选，留空=不鉴权） | 空 |
| `RECENT_TTL` / `RECENT_MAXLEN` | `avoid_recent` 环形缓冲参数 | `60` / `1000` |
| `POOL_ENABLED` | 启用节点池定时调度 | `true` |
| `POOL_INTERVAL` | 池复检周期（秒），0=关闭定时 | `86400` |
| `POOL_L1_STRICT` | L1 未知 ASN 是否直接拒绝 | `true` |
| `ABUSEIPDB_API_KEY` | AbuseIPDB API Key（L2） | 空 |
| `ABUSEIPDB_BASE_URL` | AbuseIPDB API 基址 | `https://api.abuseipdb.com/api/v2` |
| `ABUSEIPDB_TIMEOUT` / `ABUSEIPDB_CONCURRENCY` | 风险查询单次超时（秒）/ 并发数 | `8` / `8` |
| `POOL_ABUSE_MAX_SCORE` | L2 放行阈值（`abuseConfidenceScore <` 该值） | `30` |
| `ABUSE_CACHE_TTL` | 风险查询缓存 TTL（秒） | `86400` |
| `ABUSE_MAX_AGE_DAYS` | 统计举报覆盖天数 | `90` |
| `POOL_EVICT_FAILURES` | 连续失败多少次淘汰 | `2` |
| `POOL_RECHECK_IN_POOL` / `POOL_RECHECK_PROBATION` | 各状态复检间隔（秒） | `604800` / `86400` |
| `POOL_COOLDOWN` | 淘汰后冷却期（秒） | `604800` |
| `RESIN_FEED_ENABLED` | 启用 `GET /resin/subscription` 订阅端点（供 Resin 远程拉取） | `false` |
| `RESIN_FEED_TOKEN` | 订阅端点 token（公网必填；留空=不鉴权） | 空 |
| `RESIN_FEED_STATES` | 纳入订阅的池状态（逗号分隔） | `in_pool` |
| `RESIN_FEED_SUBSCRIPTION_NAME` | 建议在 Resin 中使用的订阅名 | `resin-residential-pool` |

---

## 5. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务与数据源/应用库可达性、最近同步状态 |
| GET | `/node/{ip}/{port}` | 按 `ip:port` 取单节点详情（含富化、延迟、出口） |
| GET | `/nodes` | 列表查询（过滤 `protocol`/`residential`/`as_type`/`country`/`enriched`/`max_latency_ms` + 分页 `limit`/`offset` + `sort`，可按 `latency_ms` 排序） |
| GET | `/nodes/random` | 随机返回一个可用节点，支持 `distinct_ip` / `avoid_recent` / `max_latency_ms` |
| GET | `/enrichment/{ip}` | 取某 IP 的富化原始记录（IP 级，跨节点共享） |
| GET | `/stats` | 聚合统计 |
| GET | `/tasks` | 后台任务实时进度（`ingest` / `pool`，供前端进度条轮询） |
| GET | `/export/nodes` | 导出节点（遵循 `/nodes` 的过滤/排序，`format=csv\|json`，不分页） |
| GET | `/export/pool` | 导出节点池（遵循 `/pool` 的过滤/排序） |
| GET | `/export/asn` | 导出 ASN 名单 |
| GET | `/resin/subscription` | 订阅端点：返回 sing-box 订阅内容（供 Resin 远程订阅拉取，token 走 query） |
| GET | `/resin/status` | 订阅端点状态（启用情况 / 节点数 / 内容哈希，不返回内容） |
| GET | `/export/resin-subscription` | 手动下载订阅内容（便于 dry-run 校验，不要求端点已启用） |
| GET | `/asn` | ASN 名单查询（过滤 `category`/`enabled`/`q` + 分页） |
| POST | `/asn` | 新增/覆盖一条 ASN（鉴权） |
| PUT | `/asn/{asn}` | 修改 ASN（鉴权） |
| DELETE | `/asn/{asn}` | 删除 ASN（鉴权） |
| POST | `/asn/import` | 批量导入 ASN（鉴权，body: `{category, text}`） |
| GET | `/pool` | 池内列表（过滤 `state`/`protocol`/`max_abuse_score`/`min_pool_score`/`max_latency_ms` + 分页/排序） |
| GET | `/pool/random` | 从 `in_pool` 随机取一个（`avoid_recent` / `max_latency_ms`） |
| GET | `/pool/stats` | 池状态分布 / 平均风险分 / Top ASN |
| GET | `/pool/{ip}/{port}` | 池节点详情（含检测历史） |
| POST | `/pool/{ip}/{port}/recheck` | 强制重新检测单个节点（忽略缓存，鉴权） |
| POST | `/pool/rebuild` | 触发一次池漏斗重建（`?force=true` 忽略缓存强制重检，鉴权，返回 `202`） |
| POST | `/refresh` | 触发一次重新同步（可选 `Authorization: Bearer <REFRESH_TOKEN>`，返回 `202`） |

错误统一信封：`{ "error": { "code": "NOT_FOUND", "message": "..." } }`。

数据看板：`GET /ui`（原生 HTML/JS 静态页面，同源调用上述接口，无构建步骤；仅只读展示，`/refresh` 由页面上的 token 输入框鉴权）。触发同步/重建池后，页面顶部的进度条会实时显示阶段与进度（数据来自 `/tasks`）。

---

## 6. 核心设计要点

- **只读安全**：数据源以 `file:<path>?mode=ro` 打开，容器层 `ro` 挂载，任何写操作只发生在独立的应用库（AC-1）。
- **精确过滤**：仅保留 `type == "http"`；`tls.enabled == true` 记为 `https`，否则 `http`（实测 75 http + 298 https，合计 373）。
  仅接受 `server` 为合法 IP 的节点，域名字面量节点会被跳过（无法做 IP 富化/池判定）。
- **运行时指标**：只读抽取 Resin 的 `node_latency`（各探测域名 EWMA 延迟，取最小值作为 `latency_ms`）与 `nodes_dynamic`
  （`failure_count` / `circuit_open_since` / `egress_ip` / `egress_region`），落库并在 API/看板暴露；支持按延迟过滤与排序。
  节点池的运行时指标按 **IP** 关联当前节点，能抵御 Resin 节点 hash 变化；同步结束会清理已从数据源移除的节点并自动刷新池。
- **全量落库**：ipinfo 返回的全部字段原样存入 `ip_enrichments.raw`，关键字段拆分为独立列便于查询/索引。
- **可事后重算**：住宅/机房标签由 `raw` 派生（`is_hosting` / `as.type` / `anonymous.is_res_proxy` / `is_anonymous`），规则变更无需重新调用 API。
- **可插拔富化器**：统一 `BaseIpEnricher` 接口 + 注册表回退链；内置 `IpinfoEnricher`（在线 `/batch` 批量与 `429` 回退）与 `MergedIpEnricher`（离线 Merged-IP.mmdb，ASN/地理/代理标记，无需 Token）。
- **幂等与缓存**：节点按 `hash` Upsert；IP 维度缓存富化结果（TTL）避免重复调用与限流。

### 6.1 高质量节点池（漏斗式筛选，AC-7）

在富化基础上按 **IP 去重**构建一个自动维护的高质量节点池，规则为一个“漏斗”：

- **L1 ASN 初筛**：名单存于 `asn_registry` 表（`cloud` 云/机房黑名单 + `residential` 住宅 ISP 白名单），
  可在网页「ASN 管理」增删改。命中 cloud 直接拒；命中 residential 进 L2；未知 ASN 由 `POOL_L1_STRICT` 决定。
- **L2 动态风险**：对存活 IP 调 AbuseIPDB（`abuseConfidenceScore`），`< POOL_ABUSE_MAX_SCORE` 才准入；
  结果按 IP 缓存于 `ip_risk`，仅对“新 IP / 到期复检 IP”发起请求以控制配额。
- **L4 动态更新**：状态机 `probing → in_pool ⇄ probation → evicted`，带滞回（连续 `POOL_EVICT_FAILURES`
  次超标才淘汰）与冷却期 `POOL_COOLDOWN`；定时（`POOL_INTERVAL`）在整轮漏斗中按各状态 `next_check_at` 复检。
- **L3 场景实测（暂未实现，已预留）**：`pool_score` 与状态位可承载后续 Netflix/ChatGPT/YouTube 解锁检测。

名单初始化（精选首发集：约 70 条云 ASN + 约 110 条住宅 ISP ASN）：

```bash
PYTHONPATH=. python scripts/seed_asn_registry.py          # 用 .env 的 APP_DB_PATH
# 或显式指定应用库路径：
PYTHONPATH=. python scripts/seed_asn_registry.py ./app-data/nodes.sqlite
```

看板新增两个页面：`/ui/pool.html`（节点池）与 `/ui/asn.html`（ASN 管理，写操作需 `REFRESH_TOKEN`）。

### 6.2 把高质量节点池回供给 Resin（可选，只读发布）

让 Resin 的实际路由只用本服务筛出的优质节点。**不改动 Resin**：利用 Resin 原生的
「远程订阅 + Platform 标签过滤」能力——本服务只暴露一个订阅 URL，Resin 定时自己来拉。

**原理**：Resin 远程订阅会定期 GET 一个 URL，解析 sing-box JSON `{"outbounds":[...]}`，
按节点 Hash（忽略 tag）与全局池去重合并，并自动增量重建 Platform 视图；Platform 用
MUST 正则 `*<订阅名>` 即可只认这批节点。本服务回填的正是节点原始配置，哈希与 Resin 侧
一致，因此**命中已有节点、只加引用与标签、共享健康状态，不产生重复节点**。整个链路本服务
依旧**只读** Resin（AC-1 不破坏）。

```
分类器：node_pool(state=in_pool) + nodes.raw_options_json
      → GET /resin/subscription  （sing-box JSON，token 保护）
Resin ：远程订阅 URL 指到该端点 → 定时拉取 → 去重合并 → Platform 自动更新
客户端：<Platform>.<account>:<PROXY_TOKEN> 接入 → 只走优质节点
```

**步骤一：本服务启用订阅端点**（`.env`）

```bash
RESIN_FEED_ENABLED=true
RESIN_FEED_TOKEN=生成一个长随机串
RESIN_FEED_STATES=in_pool
```

启动后端点：`https://<你的域名>/resin/subscription?token=<RESIN_FEED_TOKEN>`。
`docker-compose.yml` 已映射 `8000:8000`；公网建议前置 HTTPS 反代（Caddy/Nginx）。

**步骤二：Resin 一次性配置**（WebUI，无需改代码）

1. **Subscriptions** → 新增远程订阅：URL 填上面的端点，`update_interval` 建议 `5m`。
2. **Platforms** → 新建 Platform：`RegexFilters` 填 MUST 规则 `*<订阅名>`（如需限定地区再加
   `RegionFilters`）。
3. 客户端改用该 Platform 接入，例如：
   `curl -x http://resin:2260 -U "<PlatformName>.<account>:<PROXY_TOKEN>" https://api.ipify.org`。

**上线前 dry-run 校验（强烈建议）**：先用 `GET /export/resin-subscription?token=...` 拿到内容，
再调 Resin 的 `POST /api/v1/platforms/preview-filter`（或 `GET /api/v1/nodes`）核对命中节点数，
确认无重复节点后再让订阅生效。

**安全须知**：节点 `raw_options` 可能包含代理账号密码，订阅内容属敏感数据。
- 公网暴露**必须**设置 `RESIN_FEED_TOKEN`（否则启动会告警且无鉴权）；
- 建议用 HTTPS；仅暴露 `/resin/subscription` 这一条路径，其余 API/UI 加访问控制；
- 端点已设置 `Cache-Control: no-store`，且内容不写日志。`GET /resin/status` 的
  `contains_credentials` 可提示当前内容是否疑似含凭据。

---

## 7. 如何新增一种富化源（AC-5）

新增富化源只需实现接口并注册，**调用链路零改动**：

```python
# app/enricher/maxmind.py
from .base import BaseIpEnricher, IpEnrichment
from .registry import register

@register("maxmind", lambda s: MaxMindEnricher(s.ip2location_db_path))
class MaxMindEnricher(BaseIpEnricher):
    name = "maxmind"
    def __init__(self, db_path): ...
    def enrich(self, ip) -> IpEnrichment: ...
    def enrich_batch(self, ips) -> dict: ...
```

然后在 `.env` 中加入：`ENRICHERS=ipinfo,maxmind`（按顺序构成回退链）。

---

## 8. 测试

```bash
PYTHONPATH=. python tests/smoke_test.py
```

该测试会生成示例数据源、用 Mock 富化器替换真实 ipinfo（避免网络/Token 依赖），并验证：只读抽取与 http/https 过滤、按 IP 富化与派生标签、`/node` `/nodes` `/nodes/random` `/enrichment` `/stats` 端点、404 边界、`/refresh` 鉴权与 202 触发。

---

## 9. 验收对照（PRD 第 11 节）

| 验收项 | 状态 |
| --- | --- |
| AC-1 只读接入，不修改源文件 | ✅ `mode=ro` + 容器 `ro` 挂载 |
| AC-2 仅 373 个 http/https 节点纳入 | ✅ 按 `type==http` 过滤 + `tls` 归一化 |
| AC-3 `/node/{ip}/{port}` 返回富化与 `is_residential` | ✅ |
| AC-4 ipinfo 字段全量落库，可事后重算 | ✅ `ip_enrichments.raw` |
| AC-5 新增富化源仅实现接口并注册 | ✅ 注册表机制 |
| AC-6 `docker compose up` 启动、`/health` 正常、`/docs` 可访问 | ✅ |
| AC-7 漏斗式节点池（L1 ASN → L2 AbuseIPDB → L4 复检淘汰）+ ASN 名单 DB/网页管理 | ✅ `/pool*` `/asn*` + `/ui/pool.html` `/ui/asn.html` |
