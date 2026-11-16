# hwmon-telemetry — 双机硬件遥测 + 路由器统一入口

> 更新时间：2026-11-16 · 当前版本 **v8**（12 款皮肤 / 8 套独立材质引擎）
> 主访问点：`http://10.1.1.254:8089/`
> 服务端：`hwmon/0.6`（Python 3 标准库，零第三方依赖）

台式机 + 笔记本的实时硬件遥测，加上双机大模型用量与应用时长聚合。浏览器**只连路由器一个地址**，就能看监控、看模型账、唤醒台式、切换全屋 DNS、查 EasyTier 节点。

采集端绑死 Windows（LibreHardwareMonitor / GPU-Z / Win32 API），这是硬件限制，搬不到路由器上；路由器只做「收、聚合、发页、兜底」。

---

## 目录

1. [访问入口](#1-访问入口)
2. [系统架构](#2-系统架构)
3. [部署位置与文件同步](#3-部署位置与文件同步)
4. [页面与功能](#4-页面与功能)
5. [皮肤系统（v8）](#5-皮肤系统v8)
6. [接口参考](#6-接口参考)
7. [数据与存储](#7-数据与存储)
8. [配置与环境变量](#8-配置与环境变量)
9. [部署与运维](#9-部署与运维)
10. [前端实现要点与踩坑](#10-前端实现要点与踩坑)
11. [已知限制](#11-已知限制)
12. [版本记录](#12-版本记录)

---

## 1. 访问入口

| 用途 | 地址 |
|---|---|
| **统一入口（硬件监控）** | `http://10.1.1.254:8089/` 或 `http://192.168.31.1:8089/` |
| 模型监控（双机聚合） | `http://10.1.1.254:8089/llm` |
| 应用监控（双机聚合） | `http://10.1.1.254:8089/app` |
| 工具箱 | `http://10.1.1.254:8089/tools` |
| 静态兜底（容器挂了仍可开） | `http://10.1.1.254:8088/tools.html` |
| 笔记本备用入口 | `http://10.1.1.251:8090/`（`/llm` 为单机视角） |
| 台式备用入口 | `http://10.1.1.252:8090/`（同上） |

四个页面互为同域链接，切换时浏览器地址不会跳到别的 IP。`8088` / `8089` 无认证，仅内网可达。

---

## 2. 系统架构

```
手机 / 平板（EasyTier 或家里 WiFi）
        │  只连 10.1.1.254:8089
        ▼
路由器 小米 RC01（Docker + uhttpd）
 ├─ hwmon-hub 容器 :8089    同源页面 · /hw/<dev>/* 反代 · 状态聚合 · 长期归档
 ├─ uhttpd :8088            WOL / DNS / peers CGI + 静态兜底页
 └─ EasyTier 10.1.1.254     节点数据源（Local 即路由器自身）
        │  服务端转发，浏览器不直连 PC
        ├── 笔记本 VeT-LAPTOP 10.1.1.251:8090   agent.py + push_monitor.py
        └── 台式   VeT-PC     10.1.1.252:8090   agent.py + GPU-Z（均静默运行）
```

**数据流一句话**：PC 上的 `agent.py` 每几秒采一次硬件/模型/应用数据写 SQLite 并提供 JSON 接口；路由器容器按需并发拉两台 PC，合并后吐给浏览器，同时每分钟往自己的长期库里存一份。

设计取舍：浏览器永远只认一个源，避免跨域与"某个 IP 打不开"；PC 关机时路由器用缓存和长期库兜底，页面不空白。

---

## 3. 部署位置与文件同步

| 位置 | 关键文件 | 作用 |
|---|---|---|
| 笔记本 `C:\hwsensor\` | `agent.py` `ui.html` `llm.html` `app.html` `tools.html` `index.html` `monitor.html` `common-nav.css` `common-nav.js` `push_monitor.py` `push_config.json` | 本机采集 + 前端 + 企业微信推送 |
| 台式 `C:\hwsensor\` | `agent.py` `ui.html` `llm.html` `app.html` `start_gpuz.ps1` | 采集（含 GPU-Z CSV）+ 静默启动 |
| 路由器 `/mnt/usb/hwmon-hub/` | `router_hub.py` `monitor.html` `index.html` `llm.html` `app.html` `common-nav.css` `common-nav.js` `dns.cgi` `peers.cgi` `run-on-router.sh` `deploy-router.sh` | 统一入口、反代、聚合、CGI、部署脚本 |
| 路由器 `/mnt/usb/hwmon-hub/data/` | `hub_longterm.db` | 长期归档（分钟采样 / 日汇总，永久） |
| 路由器 `/mnt/usb/hwmon-hub/cache/` | `cache_llm_<dev>_<window>.json` | 模型用量离线缓存 |
| 路由器 `/mnt/usb/hwmon-hub/state/` | `push_state.json` `hub_push.log` | 日报去重 / 跨天重置，必须落 SSD |

### 文件同步关系（改皮肤必看）

`common-nav.css` / `common-nav.js` 是全站共享的顶栏 + 主题系统，**必须三处完全一致**：

```
开发源（唯一真源）
  Desktop\工作区\硬件监控\common-nav.{css,js}
        ├─ 复制 → Desktop\工作区\硬件监控\router-hub\
        └─ 复制 → C:\hwsensor\
```

四处页面各自的 `<style>` 只放本页布局，共享部分不放。同步后用 `md5sum` 三处比对，应只有 2 个不同哈希（css 一个、js 一个）。

路由器上的页面文件名与本地不同名但同源：`monitor.html` = 硬件监控，`index.html` = 工具箱，`llm.html` = 模型，`app.html` = 应用。

---

## 4. 页面与功能

### 4.1 硬件监控 `/`

- 主卡：GPU 名称、热点温度大环、内存占用、功耗与频率、LHM / GPU-Z 数据源状态。
- 三张仪表卡：CPU 温度、CPU 核心最高、GPU 核心（270° 环 + 数字）。
- 处理器 / 显卡面板：封装温度、核心最高、功耗、占用、频率、电压，逐条进度条。
- 时间范围：`5 分钟` / `1 小时` / `今日`（走 PC `/api/history`）与 `7 天` / `30 天`（走路由器 `/api/hw/longterm`）。
- 设备切换：`台式机` / `笔记本`；离线时不清空页面，回放路由器归档曲线 + 最后一天的今日小结缓存。
- 点 CPU 温度卡可展开每核详情弹层（含每核温度/频率/电压）。

### 4.2 模型监控 `/llm`

双机 WorkBuddy 大模型调用的聚合看板。数据源**不是** `~/.workbuddy/traces/`（trace 只有 span 时序，没有 model 也没有 usage，实测 920 个文件 0 命中），而是：

```
~/.workbuddy/projects/<项目slug>/<sessionId>.jsonl
```

带 `providerData.rawUsage` 的记录才是真实调用（约 632 文件 / 458MB / 2.7 万条）。

| 指标 | JSON 路径 |
|---|---|
| 模型 | `providerData.model` |
| 输入 Token | `providerData.rawUsage.prompt_tokens` |
| 缓存命中 Token | `providerData.rawUsage.prompt_tokens_details.cached_tokens` |
| 输出 Token | `providerData.rawUsage.completion_tokens` |
| 思考 Token | `providerData.rawUsage.completion_tokens_details.reasoning_tokens` |
| 时间 | 顶层 `timestamp`（毫秒） |
| 会话 / 标题 | `sessionId`；同文件 `type=="ai-title"` 的 `aiTitle` |
| 幂等键 | 顶层 `id`（2.7 万条全唯一，直接做主键） |

**三个字段陷阱**（踩过，写死在 `agent.py` 注释里）：

1. 顶层 `rawUsage.cached_tokens` **恒为 0**（抽样 5510 条里 5454 条为 0），必须取 `prompt_tokens_details.cached_tokens`，缺失回退 `prompt_cache_hit_tokens`。用错字段会算出 0% 命中率。
2. 日志**没有任何耗时字段**（`duration` / `latency` / `ttft` 全库 0 命中），traceId 也关联不上。所以「耗时」用**同一会话相邻调用的时间差**估算，字段名 `avg_step_ms`，前端标注「步进耗时·估算」，不当接口延迟看。
3. `traces/` 与 `projects/` 是两套东西，前者不含 token 账。

**聚合口径**：`cache_hit_rate = cached / prompt × 100`，双机合并后**按 Token 重新加权**而非两台百分比平均；`reasoning_ratio` 同理。时间线 `today` 按小时、`7d/30d/all` 按天，窗口起点对齐**本地自然日 0 点**，两台机器分桶才能逐桶相加。KPI 由分桶结果求和，保证与趋势图口径一致。

### 4.3 应用监控 `/app`

- PC 端独立 `app_poller` 线程，5 秒周期，纯标准库 `ctypes` 调 Win32 Toolhelp / `GetProcessTimes` / `GetSystemTimes` / PSAPI / 前台窗口 / `GetLastInputInfo`；句柄在 `finally` 关闭，无 psutil、无 tasklist 轮询。
- 库：`C:\hwsensor\app_usage.db`（`HWMON_APP_DB` 可覆盖），与硬件、模型库隔离；WAL，每 30 秒提交。
- AFK：最后键鼠输入超过 180 秒视为挂机；锁屏进程不计入前台应用活跃时长。窗口标题去控制字符、替换常见用户目录、截断 60 字。
- 内容：双机/单机时长、专注率、分类占比、小时与日时间线、资源趋势、实时进程 Top10 审计。

### 4.4 工具箱 `/tools`

- 一键唤醒台式（`POST /wol` → uhttpd CGI，容器挂了也能用）。
- 本机应用入口（Amame / Peach / LTX 资源库）。
- EasyTier 节点表（带 `viewer_ip` 标记「本机」）。
- 全屋 DNS 回退 / 恢复（见 [9.5](#95-dns-应急)）。

### 4.5 皮肤系统

见下一章。

---

## 5. 皮肤系统（v8）

`common-nav.css` + `common-nav.js` 提供全站统一皮肤，共 **12 款**，从顶栏「外观画廊」切换，选择存 `localStorage.hwmon_theme`。

| # | id | 名称 | 副标题 | 实现 |
|---|---|---|---|---|
| 1 | `blue-porcelain` | 天青·元青花釉 | 汝窑天青 · 素笔缠枝 | Canvas 材质（亮） |
| 2 | `sunrise-gold` | 日照·梅里金山 | 丁达尔光 · 阴阳雪峰 | Canvas 材质 |
| 3 | `ink-wash` | 烟雨·徽墨宣纸 | 生宣宿墨 · 气韵洇纸 | Canvas 材质（亮） |
| 4 | `ferrofluid-chrome` | 磁流·液态水银 | 液态水银 · 强磁玫瑰刺 | Canvas 材质 |
| 5 | `black-opal` | 欧泊·黑曜变彩 | 多面晶格 · 入射游彩 | Canvas 材质 |
| 6 | `gemini-nebula` | 星云·双子流体 | 旋涡星云 · 真丝星芒 | Canvas 材质 |
| 7 | `gemini-porcelain` | 晨曦·白瓷彩雾 | 羊脂白瓷 · 贝塞尔纱带 | Canvas 材质（亮） |
| 8 | `gemini-halftone` | 极光·点阵波束 | 动态点阵 · 光学透镜 | Canvas 材质 |
| 9 | `prism` | 幻彩·液态棱镜 | 液态棱镜 · 流体透色 | 原版 CSS `.orb` |
| 10 | `rainbow` | 极昼·全息极光 | 全息极光 · 光幕舒展 | 原版 CSS `.orb` |
| 11 | `liquid-glass` | 澄光·液态玻璃 | 澄光玻璃 · 冷银折射 | 原版 CSS `.orb` |
| 12 | `forest-breath` | 森息·林间呼吸 | 森息林间 · 呼吸薄雾 | 原版 CSS `.orb` |

### 5.1 渲染分工

**A 类（1–8）**：`.ambient-bg .orb` 全部 `display:none`，由 `.hw-material-canvas` 接管。每款一套独立的几何生成 + 光学着色算法，**没有共用波形换色**：

- **青花瓷**：球面釉光 + 偏心拉坯旋纹 + 28 条蝉翼冰裂折线 + 5 组贝塞尔缠枝 + 烟雨涟漪；高温走窑变朱砂。
- **日照金山**：分层大气散射 + 三层山脊（受光面/背光面 + 金脊描边）+ 峡谷晨雾 + 16 道丁达尔体积光柱 + 金尘。
- **徽墨宣纸**：生宣竹帘横纹 + 草木纤维 + 5 团极坐标分形宿墨（三层毛细扩散，`multiply` 叠加）+ 干笔飞白水线；高温朱砂点染。
- **液态水银**：3 个母滴池 + 每池 28 簇 `cos^k` 包络玫瑰尖刺（指针磁吸放大）+ 磁场线 + 铬金属球面高光。
- **黑曜欧泊**：58 块多面体晶格，按入射角近似布拉格衍射算干涉强度与色相，冷/暖两套游彩色板，指针邻近放大变亮。
- **双子流体**：四核公转极光场 + 9 根贝塞尔真丝弦线 + 四角星芒；星云用 `screen`、白瓷用 `multiply`，两种完全不同的混合模型。
- **点阵波束**：44×20 矩阵 + 三组异频波场叠加 + 光学磁吸透镜 + 点击的径向量子冲击波。

**B 类（9–12）**：100% 保留原有 CSS `.orb` 动效（模糊光斑 / 曲面 / 林隙），只叠加顶层专属鼠标笔触与点击特效。

### 5.2 交互与温度联动

- 顶层 `.hw-fx-canvas` 按皮肤分派 12 套鼠标跟随笔触与点击特效：青花裂纹、金光放射、墨团洇开、磁爆尖刺、晶格炸裂、双子星芒、点阵冲击波、棱镜色散、极光波纹、玻璃涟漪、叶脉扩散。
- **温度旁路**：包装 `window.fetch`，命中 `/api/snapshot` 时读 `cpu.temp` / `gpu.hot` / `gpu.core`（兼容 `temp_c` / `hotspot_c`），映射 `tau = clamp((T − 38) / 52)`，驱动材质色调、内部流速与窑变/朱砂特效。也可手动 `window.setHwmonThermalState(°C)`。
- 绘制全部受 `data-motion-effective` 管控：

  | 档位 | 帧率 | 材质 | 鼠标特效 |
  |---|---|---|---|
  | `full` | 24 FPS | 流动 | 有 |
  | `lite` | 12 FPS | **仍然流动** | 无 |
  | `off` | — | 一帧静态 | 无 |

  `data-fx-paused="true"`（标签页切到后台）与系统 `prefers-reduced-motion` 一律停笔。时间推进按真实帧间隔计算，`lite` 与 `full` 的流速一致，换档不会跳帧或变速。
- 背景画布按材质降采样：云/雾/墨类 0.50–0.62，晶格/点阵类 0.72–0.74，兼顾质感与主线程占用。

### 5.3 亮色适配

`blue-porcelain` / `ink-wash` / `gemini-porcelain` 是亮底瓷/纸材质，最容易被写死的浅色坑到：

- 各自定义完整变量组（`--bg` `--surface-bg` `--line` `--txt` `--dim` `--dim2`…）与专属卡片阴影：瓷质用「内壁高光 + 弱下沉」，宣纸用「薄内框」而非光泽。
- 三个 HTML 里原本写死的 `#fff` / `#eaf2ff` / `#c3cbd8` 等共 70 余处统一改回 `var(--txt)` / `var(--dim)`；渐变文字首端从 `#fff` 改 `var(--txt)`，否则白底上整段隐形。
- 多彩渐变按钮（`--grad-flow` 含浅橙/浅粉段）在亮底上白字压不住，`.btn` / `.tool-link.active` / 分段激活胶囊改**单色 `--p1` 底 + 反白字**。
- **新页面规范**：文字颜色只用 `var(--txt)` / `var(--dim)` / `var(--dim2)` / `var(--p1)`，不要写死浅色十六进制；渐变文字用 `var(--txt)` 作首端。

### 5.4 旧存档兼容

```js
LEGACY_THEME_MAP = { hypercar: 'sunrise-gold', gold: 'ferrofluid-chrome' }
```

读到旧主题会映射并回写 `localStorage`；默认皮肤 `sunrise-gold`。

---

## 6. 接口参考

### 6.1 PC 上的 `agent.py`（`:8090`）

| 路径 | 说明 |
|---|---|
| `GET /api/snapshot` | 当前快照，含 `health` / `alerts` / `capabilities` / `source_meta` |
| `GET /api/history?window=5m\|1h\|today` | 曲线数据（5m 起走 SQLite） |
| `GET /api/summary?window=5m\|1h\|today` | 小结：min/avg/max、采样数、覆盖率、阈值以上累计秒数 |
| `GET /api/daily` | 兼容旧页面的今日统计 |
| `GET /api/llm/summary?window=today\|7d\|30d\|all` | 本机模型用量：KPI、按模型明细、时间线、Top10 会话 |
| `GET /api/app/summary?window=today\|7d\|30d\|all` | 本机应用时长、CPU、Working Set、Commit、小时/日时间线 |
| `GET /` `GET /llm` `GET /app` `GET /tools` | 前端页面（单机视角） |
| `GET /health` | 真实健康状态与快照年龄 |
| `GET /api/easytier` | 节点列表（笔记本） |
| `POST /api/wol` | 代理路由器 WOL（60 秒防抖） |

### 6.2 路由器 hub（`:8089`）

| 路径 | 说明 |
|---|---|
| `GET /`、`/monitor` | 监控页 `monitor.html`（`PEERS.base` = `/hw/desktop`、`/hw/laptop`） |
| `GET /llm`、`/app`、`/tools` | 其余三页 |
| `GET /api/llm/aggregate?window=…` | 双机并发拉取 + 加权合并，返回 `combined` / `laptop` / `desktop` / `devices`（含 `stale_today`） |
| `GET /api/app/aggregate?window=…` | 双机应用聚合，2.5 秒超时回退缓存；今日离线缓存跨天归零 |
| `GET /api/hw/longterm?device=…&window=7d\|30d\|90d\|all` | 长期归档按天曲线，结构与 `/api/history` 同构 |
| `GET /api/hw/daily_cache?device=…` | 回放最后一日的硬件小结 |
| `GET /api/hw/longterm/status` | 长期库健康度：目录、各表行数、各设备最后采样时间 |
| `GET /hw/<dev>/*` | 反代到对应 PC 的 agent，query 透传；离线返回结构化 503 |
| `GET /api/status` | 两台 PC 在线状态 |
| `GET /api/easytier` | 节点表，带 `viewer_ip` 标记「本机」 |
| `GET\|POST /api/dns` | 反代 DNS 切换 CGI |
| `POST /wol` | 反代 WOL CGI |
| `GET\|POST /api/ltx/backup`、`GET /ltxbackup/<name>.tar` | LTX 每日备份状态 / 立即打包 / 流式下载（支持 Range） |
| `GET /health` | 容器自身健康 |

### 6.3 路由器 uhttpd CGI（`:8088`，不依赖容器）

| 路径 | 说明 |
|---|---|
| `/cgi-bin/wol` | 发送 WOL 魔术包 |
| `/cgi-bin/peers` | 路由器本地 `easytier-cli -o json peer` |
| `/cgi-bin/dns?action=status\|fallback\|restore` | 全屋 DNS 状态与切换 |

静态文件缓存策略：图片 `max-age=604800`，HTML/CSS/JS 用 `no-cache` + `ETag(mtime-size)`，部署后普通刷新即生效。

---

## 7. 数据与存储

### 7.1 库表分工

| 库 | 位置 | 内容 | 保留 |
|---|---|---|---|
| `hwmon.db` | PC `C:\hwsensor\` | 硬件采样 | 7 天自动清理 |
| `llm_usage.db` | PC `C:\hwsensor\` | 模型调用索引（幂等键 = 顶层 `id`） | 永久 |
| `app_usage.db` | PC `C:\hwsensor\` | `app_daily_stats` / `app_hourly_timeline` | 永久 |
| `hub_longterm.db` | 路由器 `data/` | `hw_samples_1m`（90 天）· `hw_daily_archive` · `llm_daily_archive` · `llm_model_archive` · `app_daily_archive`（后四表永久） | 见左 |

后台线程 `hub_archiver_poller` 每 60 秒采样、每 10 分钟汇总并同步应用日账、每小时清理过期分钟数据。离线设备直接跳过采样（不写空行），曲线上的缺口就是真实关机时段。

### 7.2 模型侧性能设计

| 措施 | 原因 |
|---|---|
| 后台线程增量扫描（20 秒）+ `llm_file_state` 记 `mtime/size/byte_offset` | 只读新增字节；632 个文件的 stat 约 90ms，不能放请求路径上 |
| 4 个窗口预聚合到内存缓存（TTL 45 秒） | 运行中现场聚合要 90–110ms（读/扫线程争 GIL + WAL），缓存后降到 **2–3ms** |
| 常驻读连接 + 20MB 页缓存 | 每次新建连接会让页缓存从零开始，`/all` 首查多花约 110ms |
| 扫描每 120 个文件 `sleep(2ms)` 让出 GIL | 否则一轮扫盘拖慢同进程 HTTP 线程约 100ms，表现为接口尖峰 |
| 覆盖索引 `idx_llm_agg(ts, model, date_str, session_id, …)` | `/all` 三类聚合 49ms → 35ms（索引不回表） |
| 路由器离线缓存 `cache_llm_<dev>_<window>.json` | 台式不常开；离线返回缓存并标 `online:false` + `cached_at` |

实测（笔记本 2.72 万条 / 458MB）：冷启动建库约 3.7s（后台，不阻塞），增量扫描 0 条时约 90ms，**接口响应 2–3ms，最差 24ms**。

### 7.3 三个「跨天」坑与处理

1. **离线设备的今日数据会残留**：缓存里 `today_date` 早于路由器当天时，`window=today` 直接按 0 参与合并（`stale_today=true`，前端横幅明说）。
2. **7/30 天窗口不会自己衰减**：离线设备一律用缓存 `all` 窗口的 `timeline` 按当前日期重新过滤求和，不用「关机那天算好的快照」。
3. **`by_model` 无法按天切分**：缓存里只有全量分布，离线时按窗口 Token 占比等比缩放，并用 `kpi.approx=true` 标注估算。

---

## 8. 配置与环境变量

`run-on-router.sh` 里的容器环境变量：

| 变量 | 值 | 说明 |
|---|---|---|
| `HWMON_LAPTOP` / `HWMON_DESKTOP` | `http://10.1.1.251:8090` / `.252:8090` | 两台 PC 的 agent 地址 |
| `HWMON_WOL` | `http://10.1.1.254:8088/cgi-bin/wol` | WOL CGI |
| `HWMON_PORT` | `8089` | 容器监听端口 |
| `HWMON_STATE_DIR` | `/state` | 推送状态目录（必须挂 SSD） |
| `HWMON_LTX_SYNC` / `HWMON_LTX_HOURS` | `1` / `6` | LTX 资源库同步开关与周期 |
| `TZ` | `Asia/Shanghai` | 跨天判定依赖它 |

PC 侧可选：`HWMON_APP_DB`（应用库路径）、`HWMON_TIMEOUT` / `HWMON_PROBE_TTL` / `HWMON_PEER_TTL`（超时与缓存 TTL）。

**脱敏规则**：

- `push_config.json` 含真实企业微信 webhook，**绝不入库**；`.gitignore` 已排除配置、日志、SQLite、缓存、备份与压缩包。
- 仓库只放 `push_config.example.json`（`wecom_webhook` / `pushplus_token` 为空）。
- `.gitattributes` 强制 `.cgi` / `.sh` / `.py` / `.html` 使用 LF，避免 Windows clone 后 CRLF 让路由器脚本无法执行。
- 关键配置：`daily_report=true`、`daily_report_time=21:00`；台式 `offline_alert=false`（不常开，离线不推）、`source_alert=true`（GPU-Z 缺失推警告）。

---

## 9. 部署与运维

### 9.1 部署命令

```bash
# 1) 笔记本（本机即 10.1.1.251）
copy agent.py ui.html llm.html app.html tools.html common-nav.css common-nav.js C:\hwsensor\
powershell -c "Stop-Process -Id (Get-CimInstance Win32_Process | ? {$_.CommandLine -like '*hwsensor\agent.py*'}).ProcessId -Force"
powershell -c "Start-ScheduledTask -TaskName HWMonitor-Agent"

# 2) 台式（10.1.1.252，需开机）：同样的文件 + 重启 HWMon-Agent

# 3) 路由器（10.1.1.254）：一条命令（推文件 + 重建容器 + 自检）
cd "Desktop/工作区/硬件监控/router-hub" && sh deploy-router.sh

# 4) 只改皮肤的纯前端热更（不重建容器）
scp -O common-nav.css common-nav.js hwmon-router:/mnt/usb/hwmon-hub/
```

`deploy-router.sh` 推送 `router_hub.py monitor.html llm.html app.html index.html common-nav.css common-nav.js run-on-router.sh docker-compose.yml` 后执行 `run-on-router.sh` 重建容器。

**为什么用密码不用密钥**：小米 RC01 的 dropbear v2017.75 明确拒绝公钥认证（ed25519 / rsa 两把钥匙、rsa-sha2 与 ssh-rsa 两种签名都试过，服务端一律 `Permission denied (publickey,password)`）；`/root` 又是只读 squashfs，`authorized_keys` 放 `/root/.ssh` 与 `/etc/dropbear` 两处都无效。所以长期凭据做成 600 权限的密码文件 + askpass：

```
~/.ssh/.hwmon_router_pass    # 一行密码
~/.ssh/hwmon_askpass.sh      # 把密码喂给 ssh
```

撤销：删掉这两个文件即可。公钥仍留在 `/data/ssh/hwmon_deploy_authorized_keys`，将来固件修好可直接启用。

容器内 `/app` 是只读挂载，所以必须单独挂三个可写目录：`cache`（模型离线缓存）、`data`（长期库）、`state`（推送状态；不挂会重建即归零，表现为「推送悄悄停了」）。

### 9.2 升级后自检

```bash
curl http://10.1.1.254:8089/health
curl http://10.1.1.254:8089/api/hw/longterm/status          # 看 dir / counts / latest
curl "http://10.1.1.254:8089/api/hw/longterm?device=laptop&window=30d"
curl http://10.1.1.251:8090/api/llm/summary?window=all | head -c 300   # 应含 today_date
ssh root@10.1.1.254 "ls -l /mnt/usb/hwmon-hub/data/"
```

分钟采样要等 1–2 分钟才有第一条 `hw_samples_1m`；日汇总要等归档线程启动后 10 分钟内第一轮。

### 9.3 自愈

- **笔记本**：清掉了重复自启（注册表 `Run\HWMon-Agent` 与计划任务 `HWMonitor-Agent` 抢 8090），只留计划任务并加「5 分钟重复 + IgnoreNew」。
- **路由器**：`guard.sh` + crontab 每 5 分钟检查 docker 与 `hwmon-hub` 容器，挂了就拉起。
- 容器 `--restart unless-stopped`；台式 `HWMon-Agent` 用 `pythonw.exe` 静默启动（S4U，无人登录也跑），GPU-Z 由 `start_gpuz.ps1` 以 `-WindowStyle Hidden` + `ShowWindow` 静默启动。

### 9.4 推送策略

- 每天 21:00 一条日报（`push_state.json` 防重启后重复补推），兼作「推送器还活着」的信号。
- 保留：越线告警、持续告警（5 分钟重复）、恢复、离线/重新在线（台式离线静默）。
- GPU-Z 数据缺失：仅在「有→无 / 无→有」各推一条`【警告】`/`【恢复】`，长期缺失不刷屏，重启后首次观测不推。
- **推送阈值与网页告警分离**：网页 GPU 热点 90°C 是负载提示，推送线 108°C 才是故障判断；笔记本 CPU 常态 100°C 不监控。

### 9.5 DNS 应急

工具页两个按钮走 `/cgi-bin/dns`（uhttpd CGI），容器挂了也能用：

- **回退（保底）**：删掉 `127.0.0.1#5353` 上游，dnsmasq 自动改用 `/tmp/resolv.conf.auto` 的运营商 DNS，不断网。
- **恢复 AdGuard**：还原 `server=127.0.0.1#5353`、`strictorder=1`、`allservers=0`。
- 保险：只接受 POST（GET 只返回状态）、切换前预检上游文件非空、`uci commit` 后校验模式不符就不重启、10 秒冷却与原子锁、基准快照 `dhcp.adguard.bak`、页面二次确认。

切换会重启 dnsmasq，全屋 DNS 中断几秒，连接不断。

### 9.6 备份与回滚

| 位置 | 备份 |
|---|---|
| 台式 | `HWMon-Agent.task.backup.xml`、`start_gpuz.ps1.bak-20261108` |
| 路由器 | `dhcp.adguard.bak`、`_backup_v8_<时间戳>/`（本次皮肤重构前快照） |
| 笔记本 | `C:\hwsensor\_backup_*`、项目 `backups/pre-v8-skins/` |
| 历史快照 | `agent.py.bak-20261112`、`ui.html.bak-20261112`、`monitor.html.bak-20261112`、`router_hub.py.bak-20261112` |

回滚：覆盖对应文件并重启服务即可。`llm_usage.db` / `app_usage.db` 是独立新库，删掉会自动重建，不需要动 `hwmon.db`。

---

## 10. 前端实现要点与踩坑

### 布局与刷新

- **切换小结不重排**：切换期间画面完全不动，数据到达后复用 DOM 只改 `textContent`；固定 `dailyMeta` / `dailyEvents` / 卡片高度，卡片文本居中、单行省略。
- 曲线刷新从 `tick()` 拆成 `refreshCharts()`，切时间范围只刷曲线。
- 设备离线时清空旧详情与曲线，不显示上一台设备的数据。
- 图表切换复用 Canvas，榜单行预创建后只更新文本，不因刷新重建节点。

### 无刷新切页（`common-nav.js`）

- 顶栏注入 + `<a>` 拦截 + `fetch` 取目标页 → `DOMParser` 解析 → 只替换 `body` 内容，`header.topbar` 与 `.ambient-bg` 保留。
- 切页时 `teardownView()` 会清掉本视图注册的定时器与监听器（页面脚本通过被包装的 `setInterval` / `addEventListener` 登记）。
- **踩坑**：材质引擎的监听器如果走被包装的 `addEventListener`，切一次页就全被摘掉，鼠标特效直接失效。所以引擎用 `EventTarget.prototype.addEventListener.call(window, …)` 绕开登记。

### 圆环与强制深色

`.ring canvas` 与 `.ring .val` 都强制 `background:transparent!important` 并明确 `z-index`。Chrome 强制深色 / Dark Reader 会注入 `rgb(24,26,27)` 不透明背景，遮住圆环下半或盖掉中心数字（平板上的「半椭圆拱形」）。`drawRing` 按实际 CSS 宽高分别设位图、用较短边定半径，并在 `resize` 时重画。

### 移动端

- 仪表区第三卡跨列消除空位、状态方框三等分等宽消除位数抖动、环形读数下移。
- 顶栏在 ≤1020px 改不透明底并隔离合成层，修复滚动回顶后换肤框「不可见但仍可点」的丢层问题。
- 画廊是视口级浮层，位置由 JS 按触发器实际坐标算（`position:fixed` + `right:auto`），避免祖先 `transform` 把定位基准改掉导致飞出屏幕。

### 亮色主题对比度

- 写死 `#fff` 是最大隐患。本次已把 4 个页面的硬编码浅色全部换成变量。
- 用程序化对比度扫描（采样 500+ 文本节点，排除渐变背景与 `-webkit-text-fill-color:transparent`）验证：4 页面 × 3 款亮色皮肤 = 0 处低对比。
- **CSS 特异性陷阱**：`:is(.row .k, .tool-link, …)` 会取参数里**最高**特异性（`.row .k` = 2 类），把整条规则抬到 2 类，从而反向覆盖后面的 `.tool-link.active` 白字规则 → 深底深字。次级文字兜底必须**逐条列选择器**，不要图省事塞进一个 `:is()`。

### 「背景不动」的排查顺序

按顺序看，前三项占九成：

1. **看顶栏「动效」右侧的小字**（`#hwMotionState`），它直接报实际档位和原因：
   - `背景缓缓流动` → full，正常；
   - `精简氛围` → lite，12 FPS 仍流动，但触屏下没有鼠标特效，观感比桌面安静；
   - `动效已关闭` → 手动选了「关闭」；
   - `系统减少动态已开启` → 系统级开关压过一切（Windows：设置 → 辅助功能 → 视觉效果 → 动画效果；iOS：辅助功能 → 动态效果 → 减弱动态效果；Android：开发者选项 → 动画缩放）；
   - `后台暂停` → 标签页在后台，`document.hidden=true` 时不绘制，属设计。
2. **动效下拉**是否被选成「精简」或「关闭」（存 `localStorage.hwmon_motion`）。
3. **是不是在手机/平板上**：`pointer: coarse` 时「自动」档会落到 lite（12 FPS 流动、无粒子特效）。想强制看完整效果，把下拉改成「完整」。
4. 确认版本：`curl -s http://10.1.1.254:8089/common-nav.js | wc -c`，v8 后期约 96.8 KB；明显更小说明还是旧文件。
5. 最后才怀疑代码：控制台执行 `window.getHwmonThermalState()`，1.5 秒内 `tau` 不变说明主循环没跑（多半是 `document.hidden`）。

---

## 11. 已知限制

- 传感器只能本机读，两台 PC 的 agent 必须保留；笔记本宕机时推送会停（靠日报消失发现），但路由器上的工具页、唤醒与 DNS 应急仍可用。
- 模型用量的「耗时」是**估算值**：取同会话相邻调用间隔，含工具执行时间，不能当接口延迟。
- 台式关机期间的模型用量只能靠路由器缓存回放；若该窗口从未成功拉取过则显示空，开机后自动补齐。
- `8088` / `8089` 无认证，仅内网可达；模型页会暴露会话标题（聊天记录摘要），不要映射到公网。
- 材质引擎在 `lite` / `off` 档位只画一帧静态材质，不参与动画；点阵皮肤是 880 个圆，低端设备建议用 `lite`。
- 仓库内 `交接文档.md` / `推送说明.md` / `部署说明.md` 是早期 v4.1 记录，**以本 README 为准**。

---

## 12. 版本记录

| 日期 | 内容 |
|---|---|
| 2026-11-07 | v5：小结跟随时间按钮、用日报替代 30 分钟心跳、新增 `/api/summary` |
| 2026-11-08 | 路由器统一入口（同域双页 + 反代）、WOL/DNS/peers CGI、台式与 GPU-Z 静默、台式离线静默、GPU-Z 缺失微信警告、EasyTier 数据源改路由器本地、小结防重排与卡片居中、平板圆环修复、GitHub 同步 |
| 2026-11-13 | v6：双机大模型用量聚合。PC 端 `llm_usage.db` 增量索引 + `/api/llm/summary`；路由器 `/api/llm/aggregate`（2.5s 超时 + 离线缓存）与 `/llm`；`llm.html` 四宫格 KPI、双 Canvas 趋势、模型效能表、Top10 会话审计 |
| 2026-11-13 | v6.1：① 扫描器改逐行读取，超长行（>64MB）丢弃并推进 offset，不再永久卡住；② `step_ms` 改毫秒精度、门限收紧 60 秒；③ 归档线程空闲时跳过重算；④ 接口新增 `today_date`。新增 `hub_longterm.db`（分钟采样 90 天 + 日汇总永久）、`/api/hw/longterm`、`/api/hw/daily_cache`，监控页加 7/30 天与离线回放 |
| 2026-11-13 | v6.2 应用遥测：PC `app_poller` 每 5 秒用 `ctypes` 采进程 CPU 时间差分、Working Set、Private Commit、前台应用与 3 分钟 AFK；路由器 `/api/app/aggregate`、`/app`、离线缓存与跨天归零，`app_daily_archive` 永久保存；四页顶栏统一加入口。287 进程一次采集约 11.7ms |
| 2026-11-13 | 运行加固：清掉笔记本重复自启（注册表与计划任务抢 8090），只留计划任务 + 5 分钟重复自愈；路由器 `guard.sh` + crontab 每 5 分钟守护；合并 `router_hub.py` 分叉，恢复 `/api/ltx/*` 七接口 |
| 2026-11-16 | v7：视觉系统重构，六套主题各自独立环境氛围层，仅用 transform/opacity；顶栏「外观画廊」逐项材质预览 + 键鼠可访问；动效档位 full/auto/lite/off，`prefers-reduced-motion` 优先；移动端修复仪表区跨列、状态方框抖动、环形读数、顶栏丢层、画廊定位 |
| 2026-11-16 | v7.1：六套主题统一「两字·四字」命名；画廊改 body 级视口浮层并按触发器坐标定位，修复移动端飞出屏幕与切页失效；新增 MIT 许可 |
| 2026-11-16 | **v8 皮肤系统底层重构**：6 → 12 款，禁止「一套正弦波换颜色」。新增 8 套独立物理材质 Canvas 管线（青花瓷 / 日照金山 / 徽墨宣纸 / 液态水银 / 黑曜欧泊 / 双子星云 / 白瓷彩雾 / 极光点阵），各自独立几何与光学算法；`prism` / `rainbow` / `liquid-glass` / `forest-breath` 原版 CSS `.orb` 完整保留，仅叠顶层特效。新增 12 套专属鼠标笔触与点击特效、`/api/snapshot` 温度旁路联动（tau 驱动色调/流速/窑变）、三款亮色瓷纸皮肤（瓷釉卡片阴影 + 全套文字对比修复）、12 款画廊缩略图、`hypercar→sunrise-gold` / `gold→ferrofluid-chrome` 旧存档迁移。修复：引擎监听器被无刷新切页误摘、点阵 880 点降采样过重、移动端顶栏裁切、`:is()` 特异性反噬导致深底深字。已回归 12 主题 × 4 页面（含 router-hub 与 `C:\hwsensor` 共 14 页）并同步三处部署目录 |

---

## 许可

MIT，见 `LICENSE`。
