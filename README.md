# hwmon-telemetry

Windows 端采集硬件（LibreHardwareMonitor / GPU-Z）、本地大模型调用与前台应用时长，路由器端（Docker + uhttpd）提供统一入口、双机聚合、离线缓存与长期归档。浏览器只连路由器一个地址，就能看监控、看模型账、唤醒台式、切换全屋 DNS、查 EasyTier 节点。

采集端绑死 Windows（LHM / GPU-Z / Win32 API），这是硬件限制，搬不到路由器上；路由器只做「收、聚合、发页、兜底」。PC 关机时路由器用缓存和长期库兜底，页面不空白。

> 文中 IP（`192.0.2.x`）、主机名（`VeT-PC` / `VeT-LAPTOP`）、路径（`C:\hwsensor\`）均为示例，替换成你自己的。主访问点示例：`http://192.0.2.1:8089/`。服务端 `hwmon/0.6`（Python 3 标准库，零第三方依赖）。

## 一、项目简介

三块数据、两端分工：

- **硬件**：LHM / GPU-Z 采 CPU、GPU、内存、功耗、频率、温度，单机 5 秒级采样。
- **模型**：扫描 `~/.workbuddy/projects/<slug>/<session>.jsonl` 里带 `providerData.rawUsage` 的真实调用，累计 token、缓存命中、思考 token、会话数。
- **应用**：纯 `ctypes` 调 Win32 采前台应用与进程 CPU / 内存，算前台时长、专注率、分类占比。

**PC 端** `agent.py` 负责采集、写本地 SQLite、提供 JSON 接口，同时托管单机的四个前端页面。**路由器端** `router_hub.py` 提供同源统一入口，按需并发拉两台 PC 并加权聚合，每分钟往长期库存一份；PC 离线时回退到 `cache/` 缓存与 `data/` 归档。

设计上浏览器永远只认路由器一个源，避免跨域和「某个 IP 打不开」；两台机器的 token / 时长在路由器侧按真实数值重新加权合并，不是把两个百分比平均。

数据保留：PC 本地 `hwmon.db`（硬件采样）留 7 天，`llm_usage.db` / `app_usage.db`（模型、应用）永久；路由器 `hub_longterm.db` 分钟采样留 90 天，日汇总（硬件 / 模型 / 应用）永久。每页顶栏会显示各数据源（LHM / GPU-Z / 模型 / 应用）的在线与健康状态，某台 PC 离线时对应页给出提示而不是空白。

## 二、架构与目录说明

```
浏览器（EasyTier 或家里 WiFi）
   │ 只连路由器一个地址 :8089
   ▼
路由器（Docker + uhttpd）
 ├─ hwmon-hub 容器 :8089   同源页面 / 反代 /hw/<dev>/* / 双机聚合 / 长期归档
 ├─ uhttpd :8088           WOL / DNS / peers CGI + 静态兜底页
 └─ EasyTier              节点数据源（Local 即路由器自身）
   ├── 笔记本 :8090   agent.py（采集 + 前端 + 企业微信推送）
   └── 台式   :8090   agent.py + GPU-Z（均静默运行）
```

数据流：PC 的 `agent.py` 每几秒采样写本地 SQLite 并提供 JSON；路由器按需并发拉两台，合并后吐给浏览器，同时每分钟往 `data/` 存一份；浏览器不直连 PC，全部经路由器转发。

根目录（PC 端）：

| 文件 | 作用 |
|---|---|
| `agent.py` | 采集 + 单机前端服务（`:8090`） |
| `ui.html` `llm.html` `app.html` `tools.html` | 硬件 / 模型 / 应用 / 工具箱四页 |
| `common-nav.css` `common-nav.js` | 共享顶栏与主题系统 |

`router-hub/`（路由器端）：

| 文件 | 作用 |
|---|---|
| `router_hub.py` | 统一入口、反代、聚合、归档、推送（`:8089`） |
| `monitor.html` `index.html` `llm.html` `app.html` | 硬件 / 工具箱 / 模型 / 应用四页 |
| `common-nav.css` `common-nav.js` | 与根目录同源，三处保持一致 |
| `run-on-router.sh` | 部署脚本（`docker run` 建容器） |
| `docker-compose.yml` | 等价备用编排（未装 compose 插件时用） |
| `push_config.example.json` | 推送配置模板（真实 `push_config.json` 不入库） |

路由器上的页面文件名与本地不同名但同源：`monitor.html` = 硬件监控，`index.html` = 工具箱，`llm.html` = 模型，`app.html` = 应用。运行时目录 `/mnt/usb/hwmon-hub/` 下，代码只读挂载，另外单独挂三个可写目录：

- `cache/`：`cache_llm_<dev>_<window>.json`，台式离线时的模型用量回退缓存。
- `data/`：`hub_longterm.db`，长期归档（分钟采样 + 日汇总，永久）。
- `state/`：`push_state.json` / `hub_push.log`，日报去重与跨天重置，必须落 SSD。

PC 部署目录（示例 `C:\hwsensor\`）运行时会生成 `hwmon.db` / `llm_usage.db` / `app_usage.db` 三个库，以及 `push_config.json` 与推送器 `push_monitor.py`（企业微信推送，可选）。

## 三、页面与核心能力

| 路由 | 内容 |
|---|---|
| `/`（硬件） | 硬件监控主页 |
| `/llm`（模型） | 双机模型用量聚合 |
| `/app`（应用） | 前台应用时长统计 |
| `/tools`（工具箱） | WOL 唤醒 / DNS 切换 / 节点表 |

**硬件监控 `/`**：主卡显示 GPU 名称、热点温度大环、内存占用、功耗与频率、LHM / GPU-Z 数据源状态；三张仪表卡（CPU 温度、CPU 核心最高、GPU 核心）；处理器 / 显卡面板逐条进度条。时间范围 5 分钟 / 1 小时 / 今日（走 PC `/api/history`）与 7 天 / 30 天（走路由器 `/api/hw/longterm`）；可切换台式 / 笔记本；点 CPU 温度卡展开每核详情。设备离线时不清空页面，回放路由器归档曲线与最后一日小结。

**模型监控 `/llm`**：双机 WorkBuddy 用量聚合，数据源是 `projects/*.jsonl` 的 `rawUsage`（不是 `traces/`）。展示 KPI、按模型明细、趋势图、Top10 会话。耗时用「同会话相邻调用间隔」估算，前端标注「步进耗时·估算」，不当接口延迟看。双机按 token 加权合并；离线设备的今日数据跨天归零，详见 architecture。

**应用监控 `/app`**：前台应用时长、专注率、分类占比、小时 / 日时间线、资源趋势、进程 Top10。最后键鼠输入超过 180 秒算 AFK，锁屏进程不计入前台活跃时长。数据存 PC 本地 `app_usage.db`。

**工具箱 `/tools`**：一键唤醒台式（`POST /wol` → uhttpd CGI，容器挂了也能用）、全屋 DNS 回退 / 恢复、EasyTier 节点表（带「本机」标记）。DNS 切换会重启 dnsmasq，全屋解析中断几秒，连接不断。

四页共用一套顶栏与主题系统，内置 12 款皮肤，可从顶栏「外观画廊」热切换，选择记在 `localStorage.hwmon_theme`。皮肤分 Canvas 材质与经典 CSS 两类，细节见 [docs/architecture.md](docs/architecture.md)。

## 四、部署与环境变量

依赖：PC 端 Windows + Python 3（纯标准库，无第三方包）；路由器端 Docker（`python:3.12-alpine` 镜像）+ uhttpd + EasyTier。

**PC 端**（两台都装）：

1. 把 `agent.py`、四个页面、`common-nav.{css,js}` 复制到部署目录（示例 `C:\hwsensor\`）。
2. `agent.py` 监听 `:8090`，用计划任务静默自启（`pythonw.exe`，无人登录也跑）。
3. GPU-Z 由 `start_gpuz.ps1` 以 `-WindowStyle Hidden` 静默启动；台式需保持开机才会被采集。

**路由器端**：

1. 在 `router-hub/` 下执行 `sh run-on-router.sh`。
2. 脚本预检 8089 端口与 python 镜像、建 `cache/data/state` 三个可写目录、`docker run` 起 `hwmon-hub`，最后轮询 `/health`。
3. `docker-compose.yml` 是等价备用；`hwmon-hub` 容器 `--restart unless-stopped`，另有 `guard.sh` + crontab 每 5 分钟守护，挂了自动拉起。

路由器 hub 的关键环境变量（在 `run-on-router.sh` / `docker-compose.yml` 里设置）：

| 变量 | 示例值 | 说明 |
|---|---|---|
| `HWMON_LAPTOP` / `HWMON_DESKTOP` | `http://192.0.2.11:8090` / `http://192.0.2.12:8090` | 两台 PC 的 agent 地址 |
| `HWMON_WOL` | `http://192.0.2.1:8088/cgi-bin/wol` | WOL CGI |
| `HWMON_PORT` | `8089` | 容器监听端口 |
| `HWMON_STATE_DIR` | `/state` | 推送状态目录（必须挂 SSD，否则重建容器即归零） |
| `HWMON_LTX_SYNC` / `HWMON_LTX_HOURS` | `1` / `6` | 资源库同步开关与周期 |
| `TZ` | `Asia/Shanghai` | 跨天判定依赖它，缺失会按 UTC 分桶导致日账错位 |

可选：PC 侧 `HWMON_APP_DB`（应用库路径）、`HWMON_DB` / `HWMON_LLM_DB`；路由器侧 `HWMON_TIMEOUT` / `HWMON_PROBE_TTL` / `HWMON_PEER_TTL`（超时与缓存 TTL）。

**升级后自检**：

```bash
curl http://192.0.2.1:8089/health
curl http://192.0.2.1:8089/api/hw/longterm/status      # 看目录 / 行数 / 最后采样时间
```

分钟采样要等 1–2 分钟才有第一条 `hw_samples_1m`，日汇总要等归档线程启动后 10 分钟内第一轮。

**静态文件同步**：`common-nav.css` / `common-nav.js` 是全站共享的顶栏 + 主题系统，根目录、`router-hub/`、PC 部署目录三处必须完全一致。改完用 `md5sum` 三处比对，应只有 2 个不同哈希（css 一个、js 一个）。各页面自己的 `<style>` 只放本页布局，共享部分不要塞进去。纯改前端可只 `scp` 这两个文件热更，不必重建容器。

## 五、隐私与安全

- `push_config.json` 含真实企业微信 webhook，**绝不入库**；`.gitignore` 已排除配置、日志、SQLite、缓存、备份与压缩包。仓库只放 `push_config.example.json`（`wecom_webhook` / `pushplus_token` 留空）。
- `.gitattributes` 强制 `.cgi` / `.sh` / `.py` / `.html` 使用 LF，避免 Windows clone 后 CRLF 让路由器脚本无法执行。
- `8088` / `8089` 无认证，仅限内网可达；模型页会暴露会话标题（聊天记录摘要），**禁止映射到公网**。
- `state/` 目录（推送状态）必须落 SSD，否则容器重建后日报去重状态归零，表现为「推送悄悄停了」。
- 备份目录（`backups/`、`_backup_*/`、`*.zip`）已在 `.gitignore` 内；回滚步骤见 [docs/operations.md](docs/operations.md)。

## 六、扩展文档索引

- [docs/architecture.md](docs/architecture.md)：模型 / 应用监控细节（字段陷阱、聚合口径、扫描性能优化）、皮肤系统、接口参考、SQLite 库表与跨天逻辑、前端实现踩坑。
- [docs/operations.md](docs/operations.md)：部署命令、升级自检、自愈、推送策略、DNS 应急、备份回滚、已知限制、历史版本记录（v5–v8）。

## 许可

MIT，见 [LICENSE](LICENSE)。