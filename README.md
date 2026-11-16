# hwmon-telemetry — 双机硬件遥测 + 路由器统一入口

> 更新时间：2026-11-16
> 当前版本：v8 / 服务端 `hwmon/0.6`（12 款皮肤 · 8 套独立材质引擎 · 应用遥测 · 路由器长期归档）
> 统一入口：`http://10.1.1.254:8089/`

台式机 + 笔记本的实时硬件遥测，外加双机大模型（WorkBuddy）用量聚合。浏览器**只连路由器一个地址**，即可看监控、看模型用量、唤醒台式、切换全屋 DNS、查看 EasyTier 节点。服务端全部是 Python 标准库或单文件前端，零第三方依赖；采集端绑定 Windows（LHM / GPU-Z / Win32 API），这是硬件限制，不能搬到路由器。

## 访问地址

| 用途 | 地址 |
|---|---|
| 统一入口（监控页） | `http://10.1.1.254:8089/` 或 `http://192.168.31.1:8089/` |
| 模型监控（双机聚合） | `http://10.1.1.254:8089/llm` |
| 应用监控（双机聚合） | `http://10.1.1.254:8089/app` |
| 工具箱 | `http://10.1.1.254:8089/tools` |
| 静态兜底（容器挂了仍可开） | `http://10.1.1.254:8088/tools.html` |
| 笔记本备用入口 | `http://10.1.1.251:8090/`（模型页 `/llm` 为单机视角） |
| 台式备用入口 | `http://10.1.1.252:8090/`（同上） |

工具箱、监控页、模型页互为同域链接，浏览器地址不会跳到其他 IP。

## 架构

```
手机（EasyTier 或家里 WiFi）
        │  只连 10.1.1.254:8089
        ▼
路由器 小米 RC01（Docker + uhttpd）
 ├─ hwmon-hub 容器 :8089    同源页面、/hw/<dev>/* 反代、状态聚合
 ├─ uhttpd :8088            WOL / DNS / peers CGI + 静态兜底页
 └─ EasyTier 10.1.1.254     节点数据源（Local 即路由器）
        │  服务端转发，浏览器不直连 PC
        ├── 笔记本 VeT-LAPTOP 10.1.1.251:8090   agent.py + push_monitor.py
        └── 台式   VeT-PC     10.1.1.252:8090   agent.py + GPU-Z（均静默运行）
```

## 组件与部署位置

| 位置 | 文件 | 作用 |
|---|---|---|
| 笔记本 `C:\hwsensor\` | `agent.py` `ui.html` `llm.html` `app.html` `tools.html` `push_monitor.py` `push_config.json` | 硬件/模型/应用遥测、前端、企业微信推送 |
| 台式 `C:\hwsensor\` | `agent.py` `ui.html` `llm.html` `app.html` `start_gpuz.ps1` | 硬件/模型/应用采集（含 GPU-Z CSV）、静默启动 |
| 路由器 `/mnt/usb/hwmon-hub/` | `router_hub.py` `index.html` `monitor.html` `llm.html` `app.html` `dns.cgi` `peers.cgi` `docker-compose.yml` `run-on-router.sh` | 统一入口、反代、双机模型/应用聚合、CGI、部署脚本 |
| 路由器 `/mnt/usb/hwmon-hub/cache/` | `cache_llm_<dev>_<window>.json` | 模型用量离线缓存（台式关机时托底） |

笔记本与台式的 `agent.py` 都监听 `0.0.0.0:8090`；台式计划任务 `HWMon-Agent` 用 `pythonw.exe` 静默启动，GPU-Z 由 `start_gpuz.ps1` 以 `-WindowStyle Hidden` + `ShowWindow` 静默启动。模型用量索引写在 `C:\hwsensor\llm_usage.db`，应用时长与进程统计写在 `C:\hwsensor\app_usage.db`（可用 `HWMON_APP_DB` 覆盖；两者独立于 `hwmon.db` 的 7 天清理）。

## 接口

**PC 上的 agent.py**

| 路径 | 说明 |
|---|---|
| `GET /api/snapshot` | 当前快照，含 `health` / `alerts` / `capabilities` / `source_meta` |
| `GET /api/history?window=5m\|1h\|today` | 曲线数据（5m 起走 SQLite） |
| `GET /api/summary?window=5m\|1h\|today` | 小结：min/avg/max、采样数、覆盖率、阈值以上累计秒数 |
| `GET /api/daily` | 兼容旧页面的今日统计 |
| `GET /api/llm/summary?window=today\|7d\|30d\|all` | 本机模型用量：KPI、按模型明细、时间线、Top10 会话 |
| `GET /api/app/summary?window=today\|7d\|30d\|all` | 本机应用时长、CPU、Working Set、Commit 与小时/日时间线 |
| `GET /llm` | 模型监控页（单机视角；由路由器打开时自动切双机聚合） |
| `GET /app` | 应用监控页（本机单机数据） |
| `GET /health` | 真实健康状态与快照年龄 |
| `GET /tools`、`GET /api/easytier` | 笔记本上的工具页与节点列表 |
| `POST /api/wol` | 代理路由器 WOL（60 秒防抖） |

**路由器 hub（`:8089`）**

| 路径 | 说明 |
|---|---|
| `GET /`、`/monitor` | 监控页 `monitor.html`（`PEERS.base` 为 `/hw/desktop`、`/hw/laptop`） |
| `GET /llm` | 模型监控页 `llm.html`（双机聚合视角） |
| `GET /api/llm/aggregate?window=today\|7d\|30d\|all` | 双机并发拉取 + 加权合并，返回 `combined` / `laptop` / `desktop` / `devices`（含 `stale_today`） |
| `GET /api/app/aggregate?window=today\|7d\|30d\|all` | 双机应用时长/资源聚合，2.5 秒超时回退缓存；今日离线缓存跨天归零 |
| `GET /app` | 双机应用监控：时长、专注率、分类、资源趋势与实时进程审计 |
| `GET /api/hw/longterm?device=laptop\|desktop&window=7d\|30d\|90d\|all` | 长期归档的按天硬件曲线，结构与 `/api/history` 同构 |
| `GET /api/hw/daily_cache?device=laptop\|desktop` | 回放最后一日的硬件小结（PC 关机/离线时用） |
| `GET /api/hw/longterm/status` | 长期库健康度：目录、各表行数、各设备最后采样时间 |
| `GET /tools` | 工具页 |
| `GET /hw/<dev>/*` | 反代到对应 PC 的 agent，query 透传；离线返回结构化 503 |
| `GET /api/status` | 两台 PC 在线状态 |
| `GET /api/easytier` | 节点表，带 `viewer_ip` 用于标记「本机」 |
| `GET\|POST /api/dns` | 反代 DNS 切换 CGI |
| `POST /wol` | 反代 WOL CGI |
| `GET\|POST /api/ltx/backup` | LTX 每日备份状态 / 立即打包 |
| `GET /ltxbackup/<name>.tar` | 备份包流式下载（支持 Range 断点续传） |

**路由器 uhttpd CGI（`:8088`，不依赖容器）**

| 路径 | 说明 |
|---|---|
| `/cgi-bin/wol` | 发送 WOL 魔术包 |
| `/cgi-bin/peers` | 路由器本地 `easytier-cli -o json peer` |
| `/cgi-bin/dns?action=status\|fallback\|restore` | 全屋 DNS 状态与切换 |

## 配置与脱敏

- **`push_config.json` 含真实企业微信 webhook，绝不入库**；`.gitignore` 已排除配置、日志、SQLite、缓存、备份与压缩包。
- 仓库只放 `push_config.example.json`（`wecom_webhook` / `pushplus_token` 为空）。
- `.gitattributes` 强制 `.cgi` / `.sh` / `.py` / `.html` 使用 LF，避免 Windows clone 后 CRLF 让路由器脚本无法执行。
- 关键配置：`daily_report=true`、`daily_report_time=21:00`；desktop `offline_alert=false`（台式不常开，离线不推）、`source_alert=true`（GPU-Z 数据缺失推警告）。

## 推送策略

- 每天 21:00 一条日报，`push_state.json` 防止重启后重复补推；日报兼作「推送器还活着」的信号。
- 保留：越线告警、持续告警（5 分钟重复）、恢复、离线/重新在线（台式离线静默）。
- GPU-Z 数据缺失：仅在「有→无 / 无→有」时各推一条 `【警告】` / `【恢复】`，长期缺失不刷屏，重启后首次观测不推。
- **推送阈值与网页告警分离**：网页 GPU 热点 90°C 预警是负载提示，推送线 108°C 才是故障判断；笔记本 CPU 常态 100°C 不监控。

## DNS 应急

工具页两个按钮走 `/cgi-bin/dns`（uhttpd CGI），容器挂了也能用：

- **回退（保底）**：删掉 `127.0.0.1#5353` 上游，dnsmasq 自动改用 `/tmp/resolv.conf.auto` 里的运营商 DNS，不断网。
- **恢复 AdGuard**：还原 `server=127.0.0.1#5353`、`strictorder=1`、`allservers=0`。
- 保险措施：只接受 POST（GET 伪造切换只返回状态）、切换前预检上游文件非空、`uci commit` 后校验模式不符就不重启 dnsmasq、10 秒冷却与原子锁、基准快照 `/mnt/usb/hwmon-hub/dhcp.adguard.bak`、页面二次确认。

切换会重启 dnsmasq，全屋 DNS 中断几秒，网络连接不断。

## 前端要点

- **切换小结不重排**：切换期间画面完全不动，数据到达后复用 DOM 只改 `textContent`；固定 `dailyMeta` / `dailyEvents` / 卡片高度，卡片文本居中、单行省略。
- 曲线刷新从 `tick()` 拆成 `refreshCharts()`，切换时间范围只刷曲线，不重绘快照区。
- 两页顶栏对称：`硬件监控 · 实时数据` + 「工具」，`硬件监控 · 工具箱` + 「← 返回监控」；运行模式改为页脚小字。
- 设备离线时清空旧详情与曲线，不显示上一台设备的数据。
- **圆环不受浏览器强制深色影响**：`.ring canvas` 与 `.ring .val` 都强制 `background:transparent!important` 并明确 `z-index`。Chrome 强制深色 / Dark Reader 会给这两层注入 `rgb(24,26,27)` 不透明背景，遮住圆环下半或盖掉中心数字，在平板上表现为「半椭圆拱形」；底弧亮度也从 `.075` 提到 `.16`。`drawRing` 按实际 CSS 宽高分别设位图、用较短边定半径，并在 `resize` 时重画。

## 皮肤系统（v8）

`common-nav.css` + `common-nav.js` 提供全站统一皮肤，共 **12 款**，分两类：

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

### 渲染分工

- **A 类（1–8）**：`.ambient-bg .orb` 全部 `display:none`，由 `.hw-material-canvas` 接管。每款皮肤一套独立的几何生成 + 光学着色算法，没有共用波形：
  - 青花瓷：球面釉光 + 拉坯旋纹 + 28 条蝉翼冰裂 + 5 组贝塞尔缠枝 + 烟雨涟漪，高温走窑变朱砂；
  - 日照金山：分层大气散射 + 三层阴阳面山脊描金 + 峡谷晨雾 + 16 道丁达尔光柱 + 金尘；
  - 徽墨宣纸：竹帘横纹 + 草木纤维 + 5 团极坐标分形宿墨（毛细扩散）+ 干笔飞白，高温朱砂点染；
  - 液态水银：3 个母滴池 + 28 簇 `cos^k` 磁流体玫瑰尖刺 + 磁场线 + 铬金属球面高光；
  - 黑曜欧泊：58 块多面体晶格 + 按入射角计算的布拉格衍射变彩，冷/暖两套游彩色板；
  - 双子流体：四核公转极光场 + 9 根贝塞尔真丝弦线 + 四角星芒（星云 `screen` / 白瓷 `multiply` 两种混合模型）；
  - 点阵波束：44×20 矩阵 + 三组异频波场 + 光学磁吸透镜 + 径向量子冲击波。
- **B 类（9–12）**：100% 保留原有 CSS `.orb` 动效，仅叠加顶层专属鼠标笔触与点击特效。

### 交互与温度联动

- 顶层 `.hw-fx-canvas` 按皮肤分派 12 套鼠标跟随笔触与点击特效（青花裂纹、金光放射、墨团洇开、磁爆尖刺、晶格炸裂、双子星芒、点阵冲击波、棱镜色散、极光波纹、玻璃涟漪、叶脉扩散）。
- 温度旁路：包装 `window.fetch`，命中 `/api/snapshot` 时读取 `cpu.temp` / `gpu.hot` / `gpu.core`（兼容 `temp_c` / `hotspot_c`），映射到 `tau = (T−38)/52`，驱动材质色调、流速与窑变/朱砂特效。也可手动调用 `window.setHwmonThermalState(°C)`。
- 全部绘制受 `data-motion-effective` 管控：`full` 才跑动画（24 FPS 节流），`lite`/`off` 只渲染一帧静态材质；`data-fx-paused="true"` 与 `prefers-reduced-motion` 一律停笔。

### 亮色适配

`blue-porcelain` / `ink-wash` / `gemini-porcelain` 为亮底瓷/纸材质：

- 各自定义完整变量组（`--bg` `--surface-bg` `--line` `--txt` `--dim` …）与瓷釉卡片阴影（内壁高光 + 弱下沉），宣纸用薄内框而非光泽。
- 原本写死 `#fff` / `#eaf2ff` / `#c3cbd8` 的文字与渐变文字统一改回 `var(--txt)` / `var(--dim)`，进度条末端白珠改主题色珠。
- 多彩渐变按钮在亮底上会撞色，` .btn` / `.tool-link.active` / 分段激活胶囊改单色 `--p1` 底 + 反白字。
- 提示：新页面若要兼容亮色皮肤，文字颜色请只用 `var(--txt)` / `var(--dim)` / `var(--dim2)`，不要写死浅色。

### 旧存档兼容

`LEGACY_THEME_MAP = { hypercar: 'sunrise-gold', gold: 'ferrofluid-chrome' }`；读取到旧主题会映射并回写 `localStorage`。默认皮肤为 `sunrise-gold`。

### 文件与同步

同一份 `common-nav.css` / `common-nav.js` 需要同步三处：工作区根目录、`router-hub/`、`C:\hwsensor\`。

## 模型用量监控（v6）

双机 WorkBuddy 大模型调用的聚合看板：`/llm`。

### 数据源

**不是** `~/.workbuddy/traces/`（trace 只有 span 时序，**没有** model、也没有 usage，实测 920 个文件 0 处命中），而是：

```
~/.workbuddy/projects/<项目slug>/<sessionId>.jsonl
```

每条带 `providerData.rawUsage` 的记录就是一次真实模型调用（约 632 个文件 / 458MB / 2.7 万条）。

| 指标 | JSON 路径 |
|---|---|
| 模型 | `providerData.model` |
| 输入 Token | `providerData.rawUsage.prompt_tokens` |
| 缓存命中 Token | `providerData.rawUsage.prompt_tokens_details.cached_tokens` |
| 输出 Token | `providerData.rawUsage.completion_tokens` |
| 思考 Token | `providerData.rawUsage.completion_tokens_details.reasoning_tokens` |
| 时间 | 顶层 `timestamp`（毫秒） |
| 会话 / 标题 | `sessionId`；同文件 `type=="ai-title"` 的 `aiTitle` |
| 幂等键 | 顶层 `id`（2.7 万条全唯一，直接做主键，不用「文件+行号」） |

**三个字段陷阱**（踩过，写死在 `agent.py` 注释里）：

1. 顶层 `rawUsage.cached_tokens` **恒为 0**（抽样 5510 条里 5454 条为 0），必须取 `prompt_tokens_details.cached_tokens`，缺失时回退 `prompt_cache_hit_tokens`。用错字段会算出 0% 命中率。
2. 日志里**没有任何耗时字段**（`duration / latency / ttft` 全库 0 命中），traces 也无法通过 `providerData.traceId` 关联。所以「耗时」用**同一会话相邻调用的时间差**估算，字段名为 `avg_step_ms`，前端明确标注「步进耗时·估算」，不当成接口延迟。
3. `~/.workbuddy/traces/` 与 `~/.workbuddy/projects/` 是两套东西，前者不含 token 账。

### 聚合口径

- `cache_hit_rate = cached_tokens / prompt_tokens × 100`，双机合并后**按 Token 重新加权**，不是两台百分比的均值。
- `reasoning_ratio = reasoning_tokens / completion_tokens × 100`，同口径重算。
- 时间线：`today` 按小时（`YYYY-MM-DD HH:00`），`7d / 30d / all` 按天（`YYYY-MM-DD`）；窗口起点一律对齐**本地自然日 0 点**，两台机器分桶才能逐桶相加。
- 合并后的 `timeline` 额外带 `laptop_calls` / `desktop_calls`，供前端画双色曲线。
- `by_model` 按 `model` 字典合并，按 `calls` 降序。
- KPI 由分桶结果求和，保证「四宫格」与「趋势图」口径永远一致。

### 性能设计

| 措施 | 原因 |
|---|---|
| 后台线程增量扫描（20 秒）+ `llm_file_state` 记录 `mtime/size/byte_offset` | 只读新增字节，632 个文件的 stat 约 90ms，不能放在请求路径上 |
| 4 个窗口**预聚合**到内存缓存（TTL 45 秒，短于刷新周期） | 在运行中的 agent 里现场聚合要 90~110ms（读线程与扫描线程争 GIL + WAL），预算后请求降到 **2~3ms** |
| 常驻读连接 + 20MB 页缓存 | 每次新建连接会让 SQLite 页缓存从零开始，`/all` 首次多花 ~110ms |
| 扫描每 120 个文件 `sleep(2ms)` 让出 GIL | 否则一轮扫盘会把同进程的 HTTP 线程拖慢约 100ms，表现为接口偶发尖峰 |
| 覆盖索引 `idx_llm_agg(ts, model, date_str, session_id, …)` | `/all` 三类聚合从 ~49ms 降到 ~35ms（索引不回表） |
| 路由器离线缓存 `cache_llm_<dev>_<window>.json` | 台式不常开；离线时返回缓存并标 `online:false` + `cached_at` |

实测（笔记本 2.72 万条 / 458MB）：冷启动建库 ~3.7s（后台，不阻塞），增量扫描 0 条时 ~90ms，**接口响应 2~3ms，最差 24ms**。

### 部署

```bash
# 1) 笔记本（本机即 10.1.1.251）：覆盖 3 个文件后重启计划任务
copy agent.py ui.html llm.html C:\hwsensor\
powershell -c "Stop-Process -Id (Get-CimInstance Win32_Process | ? {$_.CommandLine -like '*hwsensor\agent.py*'}).ProcessId -Force"
powershell -c "Start-ScheduledTask -TaskName HWMonitor-Agent"

# 2) 台式（10.1.1.252，需开机）：同样的 3 个文件 + 重启 HWMon-Agent
# 3) 路由器（10.1.1.254）：一条命令搞定（推 5 个文件 + 重建容器 + 自检）
cd router-hub && sh deploy-router.sh
```

`deploy-router.sh` 依赖 `~/.ssh/.hwmon_router_pass`（600/ACL 限本人）+ `~/.ssh/hwmon_askpass.sh`。
**为什么不用密钥**：小米 RC01 的 dropbear v2017.75 明确拒绝公钥认证——ed25519 / rsa 两把钥匙、
rsa-sha2 与 ssh-rsa 两种签名都试过，服务端一律 `Permission denied (publickey,password)`；
`/root` 又是只读 squashfs，`~/.ssh/authorized_keys` 与 `/etc/dropbear/authorized_keys` 两处都放过了仍无效。
公钥仍留在 `/data/ssh/hwmon_deploy_authorized_keys`，将来固件/patch 修好可直接启用。
撤销凭据：删掉上面两个文件即可。

`run-on-router.sh` 会建两个可写目录并挂进容器：`$DIR/cache:/app/cache`（模型用量离线缓存）、
`$DIR/data:/app/data`（长期库 `hub_longterm.db`）。容器内 `/app` 是只读的，这两个必须单独挂载。

**升级后自检**：

```bash
curl http://10.1.1.254:8089/api/hw/longterm/status     # 看 dir / counts / latest
curl "http://10.1.1.254:8089/api/hw/longterm?device=laptop&window=30d"
curl http://10.1.1.251:8090/api/llm/summary?window=all | head -c 300   # 应含 today_date
ssh root@10.1.1.254 "ls -l /mnt/usb/hwmon-hub/data/"
```

分钟采样要等 1~2 分钟才会出现第一条 `hw_samples_1m`，日汇总要等归档线程启动后 10 分钟内的第一轮。

**自测**：`curl http://10.1.1.251:8090/api/llm/summary?window=all`，把 `kpi.total_calls` / `prompt_tokens` / `cache_hit_rate` 与独立统计脚本（只读扫描 `~/.workbuddy/projects/**/*.jsonl`）对比，差值应仅为扫描期间新增的几条记录。

## 长期数据归档（v6.1）

PC 上的 `hwmon.db` 只留 7 天，台式机又常年关机。路由器把三类数据永久落到
`/mnt/usb/hwmon-hub/data/hub_longterm.db`（容器内 `/app/data/`，WAL 模式）：

| 表 | 内容 | 保留 |
|---|---|---|
| `hw_samples_1m` | 每分钟拉一次在线设备的 `/api/snapshot`，字段与 PC 采样表一致 | 90 天（自动清理） |
| `hw_daily_archive` | 每 10 分钟存一次 PC 的 `/api/summary?window=today` 原始 JSON（当天反复覆盖） | 永久 |
| `llm_daily_archive` | 每次成功拉取 `window=all` 时同步的按天模型账（60 秒节流） | 永久 |
| `llm_model_archive` | 双机模型总量快照（调用/token/思考） | 永久 |
| `app_daily_archive` | PC 应用日统计 JSON 快照（按设备/日期幂等覆盖） | 永久 |

后台线程 `hub_archiver_poller` 每 60 秒采样、每 10 分钟汇总并同步应用日账、每小时清理过期分钟数据。
离线设备直接跳过采样（不写空行），所以曲线上的缺口就是真实关机时段。

前端：监控页时间范围新增 **7 天 / 30 天**，走 `/api/hw/longterm` 按天平均曲线；
设备离线时不再整页清空，而是回放路由器归档曲线 + 最后一天的今日小结缓存。

### 三个已知的「跨天」坑与处理

1. **离线设备的今日数据会残留**：缓存里 `today_date` 若早于路由器当天，`window=today`
   的该设备直接按 0 参与合并（`devices.<dev>.stale_today=true`，前端横幅明说）。
2. **7/30 天窗口不会自己衰减**：离线设备一律用缓存的 `all` 窗口 `timeline` 按当前日期
   重新过滤求和，不再直接用「关机那天算好的 7d/30d 快照」。
3. **`by_model` 无法按天切分**：缓存里只有全量模型分布，离线时按窗口 Token 占比等比缩放，
   并在 `kpi.approx=true` 标注为估算。

## 备份与回滚

| 位置 | 备份 |
|---|---|
| 台式 | `C:\hwsensor\HWMon-Agent.task.backup.xml`、`start_gpuz.ps1.bak-20261108` |
| 路由器 | `/mnt/usb/hwmon-hub/dhcp.adguard.bak` |
| 笔记本 | `C:\hwsensor\_backup_*`、项目 `.workbuddy\backups\` |
| v6 改动前快照 | `agent.py.bak-20261112`、`ui.html.bak-20261112`、`monitor.html.bak-20261112`、`router_hub.py.bak-20261112`（笔记本与仓库各一份） |

回滚：覆盖对应文件并重启服务即可；`llm_usage.db` 是新增独立库，删掉会自动重建，不需要处理 `hwmon.db`。

## 已知限制 / 待办

- 传感器只能本机读取，两台 PC 的 agent 必须保留；笔记本宕机时推送会停（靠日报消失发现），但路由器上的工具页、唤醒与 DNS 应急仍可用。
- 模型用量的「耗时」是**估算值**：日志里没有延迟字段，`avg_step_ms` 取同会话相邻调用间隔，包含工具执行时间，不能当接口延迟看。
- 台式关机期间的模型用量只能靠路由器缓存回放（最后一次成功拉取时的快照），切换窗口若该窗口从未成功拉取过则显示空，等台式开机后自动补齐。
- `8088` / `8089` 目前无认证，仅内网可达；可选加 token。模型页会暴露会话标题（本机聊天记录摘要），不要映射到公网。
- 台式不常开，离线属正常状态；唤醒按钮在工具页。
- 仓库内 `交接文档.md` / `推送说明.md` / `部署说明.md` 是早期 v4.1 的记录，**以本 README 为准**。

## 应用监控（v6.2）

- 入口：路由器 `http://10.1.1.254:8089/app`；PC 直连 `/app`。
- PC 采样：独立 `app_poller` 后台线程，5 秒周期，纯 Python 标准库 `ctypes` 调 Win32 Toolhelp、`GetProcessTimes`、`GetSystemTimes`、PSAPI 内存 API、前台窗口与 `GetLastInputInfo`；进程句柄均在 `finally` 关闭，无 psutil/pywin32，也无 tasklist/wmic/PowerShell 轮询。
- 数据库：`C:\\hwsensor\\app_usage.db`（`HWMON_APP_DB` 覆盖），与硬件和模型数据库隔离；WAL，每 30 秒提交。`app_daily_stats` 按自然日/应用累计，`app_hourly_timeline` 存小时统计。
- AFK：最后键鼠输入超过 180 秒视为挂机；锁屏进程不计入具体前台应用活跃时长。窗口标题去控制字符、替换常见用户目录片段并截断至 60 字符，但仍可能包含其他应用标题文本，请按个人隐私偏好使用。
- 接口：PC `/api/app/summary?window=today|7d|30d|all`；路由器 `/api/app/aggregate?window=...` 并发拉取，单机失败回退 JSON 缓存；离线缓存 `today_date` 不是路由器当天时，今日活跃/挂机/类别/应用榜均置零并标记 `stale_today`。全量窗口按本机现有数据范围累积。
- 路由器 `/app` 同时返回双机/笔记本/台式机完整数据；`hub_longterm.db` 新增 `app_daily_archive`，约每 10 分钟更新当天快照，永久保留。
- `monitor.html`、路由器工具页 `index.html`、PC `ui.html`、PC/路由器 `llm.html` 与 `app.html` 顶栏均提供同域四入口；图表切换复用 Canvas，榜单行预创建后只更新文本，不因刷新重建节点。

### 部署注意

1. PC 将 `agent.py`、`app.html` 及更新后的 `ui.html`、`llm.html` 放入 `C:\\hwsensor\\`。新增线程随现有 agent 进程启动，不更改 `HWMon-Agent` 启动方式或 GPU-Z。摘要统计预聚合为内存缓存，采样落库 `commit` 后刷新四个窗口；API 请求只取缓存并拼接实时进程快照。
2. 笔记本、台式机分别更新并由现有维护流程重启 agent；不需要安装 Python 包。Win32 本机采样 12 次实测平均 8.04ms、最大 9.37ms（约 288 个进程），句柄数前后保持 136。预聚合后摘要函数连同 JSON 编码 100 次本地测量平均 0.30ms、最大 0.55ms；重载后 20 次 HTTP 往返平均 15.1ms、最大 29.9ms（包含客户端/网络/服务排队成本，不能等同服务器函数耗时）。
3. 路由器部署 `router-hub/router_hub.py`、`monitor.html`、`llm.html`、`app.html` 并重建容器；宿主机数据目录继续使用既有 `/mnt/usb/hwmon-hub/data:/app/data` 挂载。
4. 验收接口：`curl http://10.1.1.251:8090/api/app/summary?window=today`、`curl http://10.1.1.254:8089/api/app/aggregate?window=today`、`curl http://10.1.1.254:8089/api/hw/longterm/status` 检查 `app_daily_archive` 行数。路由器 API 通过 SSH 部署脚本完成了表存在和行数验收。
5. 数据累计从部署后开始，历史时长不会追溯补算。若 agent 以 Session 0 / SYSTEM 运行，Win32 前台窗口 API 可能无法返回交互桌面信息；必须在用户交互桌面 Session 1+ 验证。进程访问受保护或权限不足时会跳过该 PID，勿提高现有服务权限以绕开保护。预聚合后摘要函数加 JSON 编码 100 次本地测量平均 0.30ms、最大 0.55ms；重载后 20 次 HTTP 往返平均 15.1ms、最大 29.9ms（包含客户端/网络/排队成本，不能等同服务器函数耗时）。
6. 2026-11-13 部署状态：路由器已运行 `router-hub/deploy-router.sh` 并成功重建容器；`/api/app/aggregate` 与 `/app` 均返回 HTTP 200，在线笔记本数据参与合并；实际响应有 `combined`、`laptop`、`desktop` 和 `devices`，缓存归一化后榜首应用为 `WorkBuddy.exe`，不是 `unknown`。台式机端口 22 可达，但当前已有密钥均被拒绝，C$ 管理共享与 WinRM 不可用，未覆盖台式 `C:\\hwsensor` 文件或重启任务；聚合响应按台式今日缓存过期标记 `stale_today=true` 并将今日数值归零。要完成双机在线验收，需恢复台式机 SSH/SMB/WinRM 维护授权后再部署。路由器状态接口成功报告 `hw_samples_1m=773`、`hw_daily_archive=2`、`app_daily_archive=1`；直接从容器查询 `app_daily_archive` 表确认存在并有 1 行。

## 运行方式与自愈（2026-11-13 加固）

### 笔记本（10.1.1.251，本机采集端）

- **唯一自启入口是计划任务 `HWMonitor-Agent`**（身份 `45508` / Interactive / Limited），触发器＝「登录时」+ 每 5 分钟重复，`MultipleInstancesPolicy=IgnoreNew`，失败重试 3 次 × 1 分钟，`ExecutionTimeLimit=PT0S`（不超时）。
- **曾经有两处自启在打架**：注册表 `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run\HWMon-Agent` 和上面这个计划任务各起一份，两份都监听 `0.0.0.0:8090`，先绑定成功的那个提供服务，另一个静默空转（2026-11-13 实测 PID 8688 无端口 + PID 19316 占端口）。**Run 项已删除，不要再加回去。**
- 改回滚：`C:\hwsensor\backups\autostart-20261113\` 下有 `HWMonitor-Agent.xml`（计划任务原始定义）和 `HKCU-Run.reg`（注册表原值）。
- 计划任务是「登录时」触发，**重启后必须先登录 Windows（含 RDP）agent 才会起来**。改成「无人登录也运行」的副作用见下节。

### 路由器（10.1.1.254）

- dockerd 由固件 init 脚本开机拉起（`/etc/rc.d/S99mi_docker -> ../init.d/mi_docker`，来自 squashfs 只读层，重启不丢），容器本身是 `restart=unless-stopped`。
- 已知缺口：docker 数据目录在 USB（`/dev/sda1` → `/mnt/usb`），**USB 挂载晚于 docker 启动时容器起不来**。兜底是 `/mnt/usb/hwmon-hub/guard.sh`，由 `/etc/crontabs/root` 每 5 分钟调用，检查顺序：`dockerd` 进程 → 容器 `State.Running` → `127.0.0.1:8089/health`，动作写 `guard.log`（**日志为空＝一切正常**，不要以为是没跑）。
- 但**容器自身 `restart=unless-stopped` 已能覆盖「进程退出 / 被 stop」**：2026-11-13 实测 `docker stop hwmon-hub` 后不到 1 秒就自动 `Up`。所以 guard.sh 真正兜的是 **dockerd 没起来**（USB 晚挂载）和**容器被删 / start 失败**这两类。
- 健康检查带 **3 分钟宽限期**：首次无响应只把时间戳写进 `.health_fail_at`，连续失败超过 180 秒才 `docker restart`。原因：实测发现容器刚被 restart 策略拉起时端口尚未 ready，旧版守护会立刻再重启一次（日志出现连续两条「健康检查失败，重启容器」），5 分钟 cron 正好撞上启动窗口就会反复重启。
- `/etc` 是 ramfs，重启清空，所以**守护脚本本体必须放 `/mnt/usb/`**；只有 crontab 那一行在持久分区（`/etc/crontabs` 是 ubifs）。

### 桌面启动脚本

`C:\Users\45508\Desktop\启动硬件监控服务.cmd`（本体同 `C:\hwsensor\` 一份）：检查 8090 → 没起就调计划任务拉起 → 检查 8089 → 没响应就 SSH 拉容器 → 打开监控页。

**编码硬要求：GBK + CRLF。** 存成 UTF-8 或纯 LF 会让 cmd 按当前代码页逐行解析错位，表现为 `'о' 不是内部或外部命令` 这类乱码错误。改完用 `iconv -f UTF-8 -t GBK` + 转 CRLF 重新生成。

### router_hub.py 的两份副本（重要，出过事故）

- 2026-11-13 之前两份代码已经分叉：
  - `硬件监控/router-hub/router_hub.py`（v6）：独有 `/api/hw/longterm`、`/api/hw/daily_cache`、`/api/hw/longterm/status`、`/api/llm/aggregate`；
  - `ltx/router_hub.py`（ltx 分支）：独有 `/api/ltx/ingest`、`/changes`、`/115`、`/115-refresh`、`/delete`、`/revive`、`/rebuild`。
- 00:51 用 v6 版**整文件覆盖**部署后，路由器上 `/api/ltx/*` 全部 404：油猴推数据失败（"推不到路由器（hub 没开或局域网不通）" 是脚本的固定文案，不是真的 hub 挂了），页面日志面板拿到 404 的 HTML 去 `JSON.parse`，报 `SyntaxError: Unexpected token '<', "<!DOCTYPE"`。
- 已把 ltx 的 7 个接口、配套函数（`ltx_changes`/`ltx_delete`/`ltx_revive`/`ltx_rebuild`/`ltx_115_*`/`ltx_ingest_async` 等）、`_read_json`、`import hmac`、`ltx_stale_loop` 线程合并进 v6 版，部署文件 100429 字节。同时按 ltx 版原样把 `LTX_SYNC_ON` 写死 `False`（裸抓被 Cloudflare 挡，必然失败）。
- **今后只维护 `硬件监控/router-hub/router_hub.py` 这一份**；`ltx/router_hub.py` 仅作历史参考，不要再整文件覆盖部署。
- 排错口诀：前端出现 `Unexpected token '<', "<!DOCTYPE"` ＝ 该接口返回了 404 的 HTML。先 `curl -s -o /dev/null -w "%{http_code}" http://10.1.1.254:8089/api/xxx` 确认接口存在，再 `grep -n "/api/xxx" /mnt/usb/hwmon-hub/router_hub.py` 确认部署的是哪一版。
- 部署校验顺序（避免容器 crashloop）：上传 → `docker exec hwmon-hub python -c "import ast;ast.parse(open('/app/router_hub.py',encoding='utf-8').read())"` → `docker restart` → 探测 `/health`。旧文件备份在 `/mnt/usb/hwmon-hub/backup/`。

### 「无人登录也运行」的代价（2026-11-13 已启用 S4U）

把计划任务改成 `-LogonType S4U` 或「不管用户是否登录都运行」后：

| 影响 | 说明 |
|---|---|
| 前台应用 / AFK 统计失真 | `agent.py` 用 `GetForegroundWindow` / `GetLastInputInfo` 采前台窗口与 3 分钟 AFK（v6.2 应用遥测）。无交互桌面会话时拿不到真实前台窗口，这部分数据会变空或不准确。硬件温度、CPU、内存、进程列表不受影响。 |
| 需存凭据（仅「保存密码」方式） | 任务会把账户密码存进 LSA secret，改密码后任务静默失败；S4U 方式不需要密码，但只能访问本机资源（够用，agent 不出站）。 |
| 模型用量扫描仍正常 | `os.path.expanduser("~")` 仍解析为该用户的 `USERPROFILE`，`.workbuddy/projects` 能读到。**只有改成 SYSTEM 身份才会失效**（`~` 变成 systemprofile，`/llm` 页会空）。 |

**实测结果（2026-11-13 09:08，笔记本）**：任务已改为 `LogonType=S4U`，触发器＝开机后 45 秒（带 5 分钟重复）＋登录时（带 5 分钟重复），`MultipleInstancesPolicy=IgnoreNew` 防重复。S4U 启动的进程落在 **SessionId 0**，实测 `/health` 200、`snapshot_age_ms` 383ms、`/api/llm/summary` 正常扫到 `.workbuddy`（当天 648 次调用），`/api/app/summary` 返回 `is_afk=true`、`fg_app=""`——即上表第 1 条失真，符合预期并已接受。回滚文件：`C:\hwsensor\backups\autostart-20261113\HWMonitor-Agent.xml`（`Register-ScheduledTask -Xml (Get-Content ... -Raw) -TaskName HWMonitor-Agent -Force`）。

### 文件关联

`.py` 关联此前在系统里不存在（`HKLM\SOFTWARE\Classes\.py` 为空），双击弹「选择打开方式」。已在 `HKCU\SOFTWARE\Classes` 建立：`.py → Python.File → "C:\Users\45508\.workbuddy\binaries\python\versions\3.13.12\python.exe" "%1"`。撤销＝删掉 `HKCU\SOFTWARE\Classes\.py` 与 `Python.File` 两项。注意该路径随 WorkBuddy 升级可能变化。

## 版本记录

| 日期 | 内容 |
|---|---|
| 2026-11-07 | v5：小结跟随时间按钮、用日报替代 30 分钟心跳、新增 `/api/summary` |
| 2026-11-08 | 路由器统一入口（同域双页 + 反代）、WOL/DNS/peers CGI、台式与 GPU-Z 静默、台式离线静默、GPU-Z 缺失微信警告、EasyTier 数据源改为路由器本地、小结防重排与卡片居中、平板圆环修复（强制深色背景遮挡）、GitHub 同步 |
| 2026-11-13 | v6：双机大模型用量聚合监控。PC 端新增 `llm_usage.db` 增量索引与 `/api/llm/summary`；路由器新增 `/api/llm/aggregate`（2.5 秒超时 + 离线缓存托底）与 `/llm` 页面；`monitor.html` / `ui.html` 顶栏加「模型监控」入口；`llm.html` 四宫格 KPI（双圆环）、双 Canvas 趋势、模型效能表、Top10 会话审计；切换视角/时间范围复用 DOM 不重排 |
| 2026-11-13 | v6.1：修复 4 处缺陷并升级路由器长期存储。① 扫描器改逐行读取，超长行（>64MB）丢弃并推进 offset，不再永久卡住；② `step_ms` 改毫秒精度、门限收紧到 60 秒（历史数据自动重建，步进 24.9s→13.0s、TPS 36→74.5）；③ 归档线程空闲时跳过 4 窗口重算，仅续期缓存；④ 接口新增 `today_date`。路由器新增 `hub_longterm.db`（分钟采样 90 天 + 日汇总/模型日账永久）、`/api/hw/longterm`、`/api/hw/daily_cache`，监控页加 7/30 天与离线回放，模型页显示「离线 · 缓存于 MM-DD HH:MM」并修正跨天今日残留 |
| 2026-11-13 | v6.2 应用遥测：PC 增加独立 `app_poller`，每 5 秒用 `ctypes` 采集进程 CPU 时间差分、Working Set、Private Commit、前台应用与 3 分钟 AFK；SQLite 独立存储和 `/api/app/summary`。路由器新增 `/api/app/aggregate`、`/app`、离线缓存与今日跨天归零，`hub_longterm.db` 永久保存 `app_daily_archive`；四页顶栏统一加入应用监控入口。纯标准库，无子进程轮询。本机 287 进程一次采集实测约 11.7ms，略高于目标 10ms。
| 2026-11-13 | 运行加固：① 清掉笔记本重复自启（注册表 `Run\HWMon-Agent` 与计划任务 `HWMonitor-Agent` 抢 8090），只留计划任务并加「5 分钟重复 + IgnoreNew」自愈；② 路由器新增 `guard.sh` + crontab 每 5 分钟守护 docker/容器；③ 合并 `router_hub.py` 分叉，恢复 `/api/ltx/*` 七个接口、保留 v6 的 `longterm`/`aggregate`，`LTX_SYNC_ON` 写死 `False`；④ 新增桌面启动脚本与 `.py` 文件关联 |
| 2026-11-16 | v7 视觉系统重构：六套主题（超跑熔岩·流光 / 液态棱镜·幻彩 / 黑金奢华·鎏金 / 全息极光·星云 / 澄光玻璃·折射 / 森息林间·呼吸），各自独立环境氛围层，仅用 transform/opacity 动画；顶栏「外观画廊」逐项材质预览 + 键鼠可访问；动效档位 full(默认)/auto/lite/off，系统 prefers-reduced-motion 优先并在界面显示实际状态；模型页与应用页补 Canvas 主题配色（仅新主题生效，换肤复用缓存重绘，不新增请求）；移动端修复：仪表区第三卡跨列消除空位、状态方框等宽消除位数抖动、环形读数下移、顶栏合成层丢层导致换肤框不可见、模型页时间筛选左对齐、画廊按触发器定位不再飞出屏幕；补齐 `common-nav.css/js` 与 llm/app 页面进入版本管理；新增 MIT 许可并全量脱敏同步 GitHub |
| 2026-11-16 | v7.1 视觉系统收尾：六套主题统一「两字·四字」命名（曜夜·超跑熔岩 / 幻彩·液态棱镜 / 香槟·黑金奢华 / 极昼·全息极光 / 澄光·液态玻璃 / 森息·林间呼吸）；主题画廊改为 body 级视口浮层并按触发器坐标计算位置，修复移动端飞出屏幕与切页后失效；滚动自动收起、缩放重定位；环形读数下移；模型页时间筛选左对齐；新增 MIT 许可 |
| 2026-11-16 | v8 皮肤系统底层重构：皮肤从 6 款扩到 12 款，并彻底禁止「一套正弦波换颜色」的共用实现。新增 8 套独立物理材质 Canvas 渲染管线（青花瓷 / 日照金山 / 徽墨宣纸 / 液态水银 / 黑曜欧泊 / 双子星云 / 白瓷彩雾 / 极光点阵），每套自带几何生成与光学着色算法；`prism` / `rainbow` / `liquid-glass` / `forest-breath` 四款原版 CSS `.orb` 完整保留，只叠加顶层特效。新增 12 套专属鼠标跟随笔触与点击特效、`/api/snapshot` 温度旁路联动（tau 驱动色调/流速/窑变）、三款亮色瓷器纸本皮肤（含瓷釉卡片阴影与全套文字对比修复）、12 款画廊缩略图、`hypercar→sunrise-gold` / `gold→ferrofluid-chrome` 旧存档迁移；修复引擎监听器被无刷新切页误摘、点阵 880 点降采样过重、移动端顶栏裁切；已回归 12 主题 × 4 页面（含 router-hub 与 C:\hwsensor 共 6 页）并同步三处部署目录 |
