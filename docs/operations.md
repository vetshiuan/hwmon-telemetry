# 部署与运维

本文收纳部署命令、升级自检、自愈、推送策略、DNS 应急、备份回滚、已知限制与历史版本记录。架构与接口见 [architecture.md](architecture.md)，总览见根目录 [README](../README.md)。

> 文中 IP（`192.0.2.x`）、主机名（`VeT-PC` / `VeT-LAPTOP`）、路径（`C:\hwsensor\`）均为示例，换成你自己的。

## 部署命令

```bash
# 1) 笔记本（示例 192.0.2.11）：复制文件 + 重启计划任务
copy agent.py ui.html llm.html app.html tools.html common-nav.css common-nav.js C:\hwsensor\
powershell -c "Stop-Process -Id (Get-CimInstance Win32_Process | ? {$_.CommandLine -like '*hwsensor\agent.py*'}).ProcessId -Force"
powershell -c "Start-ScheduledTask -TaskName HWMonitor-Agent"

# 2) 台式（示例 192.0.2.12，需开机）：同样的文件 + 重启 HWMon-Agent

# 3) 路由器（示例 192.0.2.1）：一条命令（推文件 + 重建容器 + 自检）
cd router-hub && sh run-on-router.sh

# 4) 只改皮肤的纯前端热更（不重建容器）
scp -O common-nav.css common-nav.js hwmon-router:/mnt/usb/hwmon-hub/
```

`deploy-router.sh`（若使用）会推送 `router_hub.py monitor.html llm.html app.html index.html common-nav.css common-nav.js run-on-router.sh docker-compose.yml` 后执行 `run-on-router.sh` 重建容器。当前仓库以 `run-on-router.sh` 为准（`docker-compose.yml` 是等价备用，路由器未装 compose 插件时用）。

为什么用密码不用密钥：小米 RC01 的 dropbear v2017.75 明确拒绝公钥认证（ed25519 / rsa 两把钥匙、rsa-sha2 与 ssh-rsa 两种签名都试过，服务端一律 `Permission denied (publickey,password)`）；`/root` 又是只读 squashfs，`authorized_keys` 放 `/root/.ssh` 与 `/etc/dropbear` 两处都无效。所以长期凭据做成 600 权限的密码文件 + askpass：

```
~/.ssh/.hwmon_router_pass    # 一行密码
~/.ssh/hwmon_askpass.sh      # 把密码喂给 ssh
```

撤销：删掉这两个文件即可。公钥仍留在 `/data/ssh/hwmon_deploy_authorized_keys`，将来固件修好可直接启用。

容器内 `/app` 是只读挂载，所以必须单独挂三个可写目录：`cache`（模型离线缓存）、`data`（长期库）、`state`（推送状态；不挂会重建即归零，表现为「推送悄悄停了」）。

## 升级后自检

```bash
curl http://192.0.2.1:8089/health
curl http://192.0.2.1:8089/api/hw/longterm/status          # 看 dir / counts / latest
curl "http://192.0.2.1:8089/api/hw/longterm?device=laptop&window=30d"
curl http://192.0.2.11:8090/api/llm/summary?window=all | head -c 300   # 应含 today_date
ssh root@192.0.2.1 "ls -l /mnt/usb/hwmon-hub/data/"
```

分钟采样要等 1–2 分钟才有第一条 `hw_samples_1m`；日汇总要等归档线程启动后 10 分钟内第一轮。

## 自愈

- 笔记本：清掉了重复自启（注册表 `Run\HWMon-Agent` 与计划任务 `HWMonitor-Agent` 抢 8090），只留计划任务并加「5 分钟重复 + IgnoreNew」。
- 路由器：`guard.sh` + crontab 每 5 分钟检查 docker 与 `hwmon-hub` 容器，挂了就拉起。
- 容器 `--restart unless-stopped`；台式 `HWMon-Agent` 用 `pythonw.exe` 静默启动（S4U，无人登录也跑），GPU-Z 由 `start_gpuz.ps1` 以 `-WindowStyle Hidden` + `ShowWindow` 静默启动。

## 推送策略

- 每天 21:00 一条日报（`push_state.json` 防重启后重复补推），兼作「推送器还活着」的信号。
- 保留：越线告警、持续告警（5 分钟重复）、恢复、离线/重新在线（台式离线静默）。
- GPU-Z 数据缺失：仅在「有→无 / 无→有」各推一条`【警告】`/`【恢复】`，长期缺失不刷屏，重启后首次观测不推。
- 推送阈值与网页告警分离：网页 GPU 热点 90°C 是负载提示，推送线 108°C 才是故障判断；笔记本 CPU 常态 100°C 不监控。

## DNS 应急

工具页两个按钮走 `/cgi-bin/dns`（uhttpd CGI），容器挂了也能用：

- 回退（保底）：删掉 `127.0.0.1#5353` 上游，dnsmasq 自动改用 `/tmp/resolv.conf.auto` 的运营商 DNS，不断网。
- 恢复 AdGuard：还原 `server=127.0.0.1#5353`、`strictorder=1`、`allservers=0`。
- 保险：只接受 POST（GET 只返回状态）、切换前预检上游文件非空、`uci commit` 后校验模式不符就不重启、10 秒冷却与原子锁、基准快照 `dhcp.adguard.bak`、页面二次确认。

切换会重启 dnsmasq，全屋 DNS 中断几秒，连接不断。

## 备份与回滚

| 位置 | 备份 |
|---|---|
| 台式 | `HWMon-Agent.task.backup.xml`、`start_gpuz.ps1.bak-20261108` |
| 路由器 | `dhcp.adguard.bak`、`_backup_v8_<时间戳>/`（皮肤重构前快照） |
| 笔记本 | `C:\hwsensor\_backup_*`、项目 `backups/pre-v8-skins/` |
| 历史快照 | `agent.py.bak-20261112`、`ui.html.bak-20261112`、`monitor.html.bak-20261112`、`router_hub.py.bak-20261112` |

回滚：覆盖对应文件并重启服务即可。`llm_usage.db` / `app_usage.db` 是独立新库，删掉会自动重建，不需要动 `hwmon.db`。

## 已知限制

- 传感器只能本机读，两台 PC 的 agent 必须保留；笔记本宕机时推送会停（靠日报消失发现），但路由器上的工具页、唤醒与 DNS 应急仍可用。
- 模型用量的「耗时」是估算值：取同会话相邻调用间隔，含工具执行时间，不能当接口延迟。
- 台式关机期间的模型用量只能靠路由器缓存回放；若该窗口从未成功拉取过则显示空，开机后自动补齐。
- `8088` / `8089` 无认证，仅内网可达；模型页会暴露会话标题（聊天记录摘要），不要映射到公网。
- 材质引擎在 `lite` / `off` 档位只画一帧静态材质，不参与动画；点阵皮肤是 880 个圆，低端设备建议用 `lite`。
- 仓库内 `交接文档.md` / `推送说明.md` / `部署说明.md` 是早期 v4.1 记录，以 README 与本文为准。

## 版本记录

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
| 2026-11-16 | v8 皮肤系统底层重构：6 → 12 款，禁止「一套正弦波换颜色」。新增 8 套独立物理材质 Canvas 管线（青花瓷 / 日照金山 / 徽墨宣纸 / 液态水银 / 黑曜欧泊 / 双子星云 / 白瓷彩雾 / 极光点阵），各自独立几何与光学算法；`prism` / `rainbow` / `liquid-glass` / `forest-breath` 原版 CSS `.orb` 完整保留，仅叠顶层特效。新增 12 套专属鼠标笔触与点击特效、`/api/snapshot` 温度旁路联动（tau 驱动色调/流速/窑变）、三款亮色瓷纸皮肤（瓷釉卡片阴影 + 全套文字对比修复）、12 款画廊缩略图、`hypercar→sunrise-gold` / `gold→ferrofluid-chrome` 旧存档迁移。修复：引擎监听器被无刷新切页误摘、点阵 880 点降采样过重、移动端顶栏裁切、`:is()` 特异性反噬导致深底深字。已回归 12 主题 × 4 页面（含 router-hub 与部署目录共 14 页）并同步三处部署目录 |