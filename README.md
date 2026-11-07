# hwmon-telemetry — 双机硬件遥测 + 路由器统一入口

> 更新时间：2026-11-08
> 当前版本：v5 / 服务端 `hwmon/0.6`
> 统一入口：`http://10.1.1.254:8089/`

台式机 + 笔记本的实时硬件遥测。浏览器**只连路由器一个地址**，即可看监控、唤醒台式、切换全屋 DNS、查看 EasyTier 节点。服务端全部是 Python 标准库或单文件前端，零第三方依赖；采集端绑定 Windows（LHM / GPU-Z / Win32 API），这是硬件限制，不能搬到路由器。

## 访问地址

| 用途 | 地址 |
|---|---|
| 统一入口（监控页） | `http://10.1.1.254:8089/` 或 `http://192.168.31.1:8089/` |
| 工具箱 | `http://10.1.1.254:8089/tools` |
| 静态兜底（容器挂了仍可开） | `http://10.1.1.254:8088/tools.html` |
| 笔记本备用入口 | `http://10.1.1.251:8090/` |
| 台式备用入口 | `http://10.1.1.252:8090/` |

工具箱与监控页互为同域链接，浏览器地址不会跳到其他 IP。

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
| 笔记本 `C:\hwsensor\` | `agent.py` `ui.html` `tools.html` `push_monitor.py` `push_config.json` | 采集、前端、企业微信推送 |
| 台式 `C:\hwsensor\` | `agent.py` `ui.html` `start_gpuz.ps1` | 采集（含 GPU-Z CSV）、静默启动 |
| 路由器 `/mnt/usb/hwmon-hub/` | `router_hub.py` `index.html` `monitor.html` `dns.cgi` `peers.cgi` `docker-compose.yml` `run-on-router.sh` | 统一入口、反代、CGI、部署脚本 |

笔记本与台式的 `agent.py` 都监听 `0.0.0.0:8090`；台式计划任务 `HWMon-Agent` 用 `pythonw.exe` 静默启动，GPU-Z 由 `start_gpuz.ps1` 以 `-WindowStyle Hidden` + `ShowWindow` 静默启动。

## 接口

**PC 上的 agent.py**

| 路径 | 说明 |
|---|---|
| `GET /api/snapshot` | 当前快照，含 `health` / `alerts` / `capabilities` / `source_meta` |
| `GET /api/history?window=5m\|1h\|today` | 曲线数据（5m 起走 SQLite） |
| `GET /api/summary?window=5m\|1h\|today` | 小结：min/avg/max、采样数、覆盖率、阈值以上累计秒数 |
| `GET /api/daily` | 兼容旧页面的今日统计 |
| `GET /health` | 真实健康状态与快照年龄 |
| `GET /tools`、`GET /api/easytier` | 笔记本上的工具页与节点列表 |
| `POST /api/wol` | 代理路由器 WOL（60 秒防抖） |

**路由器 hub（`:8089`）**

| 路径 | 说明 |
|---|---|
| `GET /`、`/monitor` | 监控页 `monitor.html`（`PEERS.base` 为 `/hw/desktop`、`/hw/laptop`） |
| `GET /tools` | 工具页 |
| `GET /hw/<dev>/*` | 反代到对应 PC 的 agent，query 透传；离线返回结构化 503 |
| `GET /api/status` | 两台 PC 在线状态 |
| `GET /api/easytier` | 节点表，带 `viewer_ip` 用于标记「本机」 |
| `GET\|POST /api/dns` | 反代 DNS 切换 CGI |
| `POST /wol` | 反代 WOL CGI |

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

## 备份与回滚

| 位置 | 备份 |
|---|---|
| 台式 | `C:\hwsensor\HWMon-Agent.task.backup.xml`、`start_gpuz.ps1.bak-20261108` |
| 路由器 | `/mnt/usb/hwmon-hub/dhcp.adguard.bak` |
| 笔记本 | `C:\hwsensor\_backup_*`、项目 `.workbuddy\backups\` |

回滚：覆盖对应文件并重启服务即可；数据库结构未变，不需要处理 `hwmon.db`。

## 已知限制 / 待办

- 传感器只能本机读取，两台 PC 的 agent 必须保留；笔记本宕机时推送会停（靠日报消失发现），但路由器上的工具页、唤醒与 DNS 应急仍可用。
- `8088` / `8089` 目前无认证，仅内网可达；可选加 token。
- 台式不常开，离线属正常状态；唤醒按钮在工具页。
- 仓库内 `交接文档.md` / `推送说明.md` / `部署说明.md` 是早期 v4.1 的记录，**以本 README 为准**。

## 版本记录

| 日期 | 内容 |
|---|---|
| 2026-11-07 | v5：小结跟随时间按钮、用日报替代 30 分钟心跳、新增 `/api/summary` |
| 2026-11-08 | 路由器统一入口（同域双页 + 反代）、WOL/DNS/peers CGI、台式与 GPU-Z 静默、台式离线静默、GPU-Z 缺失微信警告、EasyTier 数据源改为路由器本地、小结防重排与卡片居中、GitHub 同步 |
