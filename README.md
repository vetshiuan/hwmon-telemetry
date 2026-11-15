# hwmon-telemetry

Windows 双机硬件遥测面板：把 **LibreHardwareMonitor**（Web Server）与 **GPU-Z**（传感器日志）聚合成统一 JSON，单文件前端、零第三方依赖；可选路由器 hub 提供统一入口与反代。

> 本仓库是**脱敏后的核心代码**。内网地址、设备名、企业微信 webhook、SSH 凭据、运维记录一律不包含，部署时按「配置」一节填写自己的值。

## 架构

```
[每台 Windows PC]
  LibreHardwareMonitor :8085/data.json ─┐
  GPU-Z 传感器日志 gpuz.csv（独显）    ├─> agent.py :8090 ─> /api/snapshot
  系统 API（物理内存）                 ┘        ├─> /api/history、/api/summary（SQLite）
                                                └─> /api/core_history（每核温度）

[路由器 / 小主机]
  router_hub.py :8089 ─> /hw/<dev>/* 反代到各 PC
                     └─> monitor.html 统一入口 + 告警 / 日报引擎
```

- **为什么需要两个采集器**：LHM 负责 CPU、主板、内存、磁盘、风扇；GPU-Z 负责显卡温度三项与 16pin 电压/功耗。实测部分新卡（如 RTX 5090 D v2）在 LHM 0.9.6 下没有 GPU 温度节点，两者不是冗余，不能随意合并。
- **前端**：`ui.html` 原生 JS + 手写 canvas，2 秒轮询，无框架、无 CDN、无构建步骤。
- **hub 版**：`router-hub/monitor.html` 走 `/hw/<dev>` 相对反代，适合从外部网络访问。

## 每核明细

点击「CPU 温度」或「CPU 核心最高」卡片，弹出每颗物理核心的温度 / 频率 / 电压 / 距 TjMax，以及单核温度曲线。

- 数据来自 LHM `data.json` 的 `P-Core #N` / `E-Core #N` 传感器，后端 `collect_cpu_cores()` 收敛成稳定契约。
- 线程占用使用 `CPU Core #N Thread #N` 全局线程号，与 P/E 编号不是同一套体系，界面单独列出、不合并。
- 每核温度按 30 秒降频写入 `core_samples` 表（每行自带 layout，核心数变化不会错配），不参与主表聚合。
- 平台没有每核传感器时显示明确提示，不报错。

## 电压迷你曲线

16pin 电压与 CPU 电压不用静态进度条，改为各详情栏最下方的历史折线，跟随页面「5 分钟 / 1 小时 / 今日」切换。

- 16pin 固定 11.0–13.0V 量程，并画出 11.8V 预警线与 12.6V 严重线。
- CPU 电压固定 0–1.5V 量程；`cpu_volt` 历史列在启动时自动迁移补齐，旧数据库无需手工处理。
- 卡片区只保留不与详情栏重复的总览值：台式 4 张、笔记本 2 张，按卡片数自动选列数，宽屏下一行填满；「CPU 温度」卡可点开每核明细。
- 16pin 曲线颜色跟随实际告警等级；11.4V / 12.6V 为严重线、11.8V 为预警线，11.9V 属于正常范围。

## 告警分级与自检

- 告警分三类：数据源故障 > 硬件越限 > 资源提示。资源提示（内存/磁盘占用）不参与健康状态判定，只单独计数，避免盖住真正重要的告警。
- 16pin 读数断更属于「可观测性故障」而非电压异常：连续 2 分钟无更新才给 `warn`，文案明确标注「非电压异常」；真实电压越限另有独立告警。
- `/health` 附带 `lhm`（8085 是否监听、LHM 错误、agent 是否提权）与 `db_backup` 状态。
- `hwmon.db` 每周用 SQLite 在线备份 API 复制一次，保留最近 4 份，默认放在 `<db 目录>/backups/db`，可用 `HWMON_BACKUP_DIR` 改。
- 每核弹层显示核间温差与区间内「最热核心」占比；最热行用蓝色 + 文字标记，不与黄色告警混淆。

## 接口

| 路径 | 说明 |
|---|---|
| `/api/snapshot` | 实时快照，含 `cpu.cores`、`sources`、`alerts`、`health` |
| `/api/history?window=5m\|1h\|today` | 聚合指标历史 |
| `/api/summary?window=...` | 区间 min / avg / max、越线累计 |
| `/api/core_history?core=P1&window=5m` | 单核温度历史 |
| `/health` | 健康与数据源状态 |

## 配置

全部走环境变量，代码里的默认值都是本机回环地址，不含真实网络信息。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HWMON_DB` | 同目录 `hwmon.db` | SQLite 路径 |
| `HWMON_WOL_URL` | `http://127.0.0.1:8088/cgi-bin/wol` | WOL CGI |
| `EASYTIER_CLI` | 自动探测 C:/D: | 工具页用，可不用 |
| `HWMON_LAPTOP` / `HWMON_DESKTOP` | `http://127.0.0.1:8090` | hub 反代目标 |
| `HWMON_WOL` / `HWMON_DNS_CGI` / `HWMON_PEERS_CGI` | 本机 | hub 侧工具接口 |

前端 `ui.html` 的 `PEERS` 数组填自己两台机器的地址；若部署 hub，直接用 `router-hub/monitor.html`，无需改地址。

## 部署要点

1. LHM 必须以 **SYSTEM / 管理员** 身份启动，否则 `StartHttpListener()` 会静默失败、8085 无监听（不弹窗、不报错）。
2. GPU-Z 是 GUI 程序，需开启传感器日志输出到 CSV；无人登录时可能停止刷新，agent 用 `GPUZ_STALE_MS` 判定过期并自动降级到 LHM 数据。
3. 告警引擎在 `router_hub.py`，真实配置 `push_config.json` **不要提交**，示例见 `push_config.example.json`。
4. 容器必须挂载可写的 `state/` 目录保存推送状态。

## 文件

| 文件 | 作用 |
|---|---|
| `agent.py` | 采集、聚合、告警判定、SQLite、HTTP |
| `common-nav.css` | 全站设计系统：六套主题 token、环境氛围层、顶栏、卡片、主题画廊 |
| `common-nav.js` | 顶栏注入、六主题切换、动效档位、无刷新视图切换与生命周期回收 |
| `ui.html` / `llm.html` / `app.html` / `tools.html` | 直连版四个页面（硬件 / 模型 / 应用 / 工具箱） |
| `router-hub/router_hub.py` | 反代、统一入口、告警与日报 |
| `router-hub/monitor.html` / `llm.html` / `app.html` / `index.html` | hub 版四个页面 |
| `router-hub/common-nav.css` / `common-nav.js` | hub 版公共资源（与根目录内容一致） |
| `router-hub/docker-compose.yml` / `run-on-router.sh` | 容器编排与启动 |

## 主题与动效

六套主题，保存在 `localStorage.hwmon_theme`，通过顶栏「外观画廊」直接选择；每行带该主题材质的动态缩略预览。

| 键 | 名称 | 背景语汇 |
|---|---|---|
| `hypercar` | 超跑熔岩·流光 | 黑曜石底 + 橙红熔岩光带缓慢推进 |
| `prism` | 液态棱镜·幻彩 | 紫粉与冰青流体交叠透色 |
| `gold` | 黑金奢华·鎏金 | 深黑底 + 香槟金丝游走 |
| `rainbow` | 全息极光·星云 | 多层极光帷幕横向舒展 |
| `liquid-glass` | 澄光玻璃·折射 | 冷银曲面流动、折边与透色 |
| `forest-breath` | 森息林间·呼吸 | 12 秒林光呼吸 + 36 秒薄雾漂移 |

六套名称统一为「四字主题 · 两字特质」，顶栏触发器显示当前主题名，与画廊中选中项名称、缩略预览、强调色完全一致。

动效档位 `localStorage.hwmon_motion`：`full`（默认）/ `lite` / `off` / `auto`。系统 `prefers-reduced-motion: reduce` 始终优先，页面会显示当前实际生效状态。`lite` 关闭背景模糊与指针特效，仅保留单层低透明度变化；`off` 冻结全部装饰动画，不影响数据刷新。

氛围层复用 `.ambient-bg` 内三个既有节点，只用 `transform` / `opacity` 动画，不含视频、WebGL、外部字体或 CDN。

## 不包含

真实 `push_config.json`、SQLite 数据库、GPU-Z 日志、截图、备份、运行日志、运维交接文档，以及任何含内网地址或凭据的文件。见 `.gitignore`。

## 许可

MIT License，见 `LICENSE`。可自由用于个人或商业项目；如二次分发，保留版权声明即可。仓库内的具体部署配置需自行按「配置」一节填写。

