# 架构与实现细节

本文收纳接口参考、数据与存储设计、模型扫描性能优化、字段陷阱、跨天逻辑与皮肤 / 前端实现记录。页面与部署入口见根目录 [README](../README.md)。

## 模型监控（/llm）

双机 WorkBuddy 大模型调用的聚合看板。数据源不是 `~/.workbuddy/traces/`（trace 只有 span 时序，没有 model 也没有 usage，实测 920 个文件 0 命中），而是：

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

三个字段陷阱：

1. 顶层 `rawUsage.cached_tokens` 恒为 0（抽样 5510 条里 5454 条为 0），必须取 `prompt_tokens_details.cached_tokens`，缺失回退 `prompt_cache_hit_tokens`。用错字段会算出 0% 命中率。
2. 日志没有任何耗时字段（`duration` / `latency` / `ttft` 全库 0 命中），traceId 也关联不上。所以「耗时」用同一会话相邻调用的时间差估算，字段名 `avg_step_ms`，前端标注「步进耗时·估算」，不当接口延迟看。
3. `traces/` 与 `projects/` 是两套东西，前者不含 token 账。

聚合口径：`cache_hit_rate = cached / prompt × 100`，双机合并后按 Token 重新加权而非两台百分比平均；`reasoning_ratio` 同理。时间线 `today` 按小时、`7d/30d/all` 按天，窗口起点对齐本地自然日 0 点，两台机器分桶才能逐桶相加。KPI 由分桶结果求和，保证与趋势图口径一致。

## 应用监控（/app）

- PC 端独立 `app_poller` 线程，5 秒周期，纯标准库 `ctypes` 调 Win32 Toolhelp / `GetProcessTimes` / `GetSystemTimes` / PSAPI / 前台窗口 / `GetLastInputInfo`；句柄在 `finally` 关闭，无 psutil、无 tasklist 轮询。
- 库：`C:\hwsensor\app_usage.db`（`HWMON_APP_DB` 可覆盖），与硬件、模型库隔离；WAL，每 30 秒提交。
- AFK：最后键鼠输入超过 180 秒视为挂机；锁屏进程不计入前台应用活跃时长。窗口标题去控制字符、替换常见用户目录、截断 60 字。
- 内容：双机/单机时长、专注率、分类占比、小时与日时间线、资源趋势、实时进程 Top10 审计。

## 皮肤系统

`common-nav.css` + `common-nav.js` 提供全站统一皮肤，共 12 款，从顶栏「外观画廊」切换，选择存 `localStorage.hwmon_theme`。

| # | id | 名称 | 实现 |
|---|---|---|---|
| 1 | `blue-porcelain` | 天青·元青花釉 | Canvas 材质（亮） |
| 2 | `sunrise-gold` | 日照·梅里金山 | Canvas 材质 |
| 3 | `ink-wash` | 烟雨·徽墨宣纸 | Canvas 材质（亮） |
| 4 | `ferrofluid-chrome` | 磁流·液态水银 | Canvas 材质 |
| 5 | `black-opal` | 欧泊·黑曜变彩 | Canvas 材质 |
| 6 | `gemini-nebula` | 星云·双子流体 | Canvas 材质 |
| 7 | `gemini-porcelain` | 晨曦·白瓷彩雾 | Canvas 材质（亮） |
| 8 | `gemini-halftone` | 极光·点阵波束 | Canvas 材质 |
| 9 | `prism` | 幻彩·液态棱镜 | 原版 CSS `.orb` |
| 10 | `rainbow` | 极昼·全息极光 | 原版 CSS `.orb` |
| 11 | `liquid-glass` | 澄光·液态玻璃 | 原版 CSS `.orb` |
| 12 | `forest-breath` | 森息·林间呼吸 | 原版 CSS `.orb` |

### 渲染分工

A 类（1–8）：`.ambient-bg .orb` 全部 `display:none`，由 `.hw-material-canvas` 接管。每款一套独立的几何生成 + 光学着色算法，没有共用波形换色：

- 青花瓷：球面釉光 + 偏心拉坯旋纹 + 28 条蝉翼冰裂折线 + 5 组贝塞尔缠枝 + 烟雨涟漪；高温走窑变朱砂。
- 日照金山：分层大气散射 + 三层山脊（受光面/背光面 + 金脊描边）+ 峡谷晨雾 + 16 道丁达尔体积光柱 + 金尘。
- 徽墨宣纸：生宣竹帘横纹 + 草木纤维 + 5 团极坐标分形宿墨（三层毛细扩散，`multiply` 叠加）+ 干笔飞白水线；高温朱砂点染。
- 液态水银：3 个母滴池 + 每池 28 簇 `cos^k` 包络玫瑰尖刺（指针磁吸放大）+ 磁场线 + 铬金属球面高光。
- 黑曜欧泊：58 块多面体晶格，按入射角近似布拉格衍射算干涉强度与色相，冷/暖两套游彩色板，指针邻近放大变亮。
- 双子流体：四核公转极光场 + 9 根贝塞尔真丝弦线 + 四角星芒；星云用 `screen`、白瓷用 `multiply`，两种完全不同的混合模型。
- 点阵波束：44×20 矩阵 + 三组异频波场叠加 + 光学磁吸透镜 + 点击的径向量子冲击波。

B 类（9–12）：100% 保留原有 CSS `.orb` 动效（模糊光斑 / 曲面 / 林隙），只叠加顶层专属鼠标笔触与点击特效。

### 交互与温度联动

- 顶层 `.hw-fx-canvas` 按皮肤分派 12 套鼠标跟随笔触与点击特效：青花裂纹、金光放射、墨团洇开、磁爆尖刺、晶格炸裂、双子星芒、点阵冲击波、棱镜色散、极光波纹、玻璃涟漪、叶脉扩散。
- 温度旁路：包装 `window.fetch`，命中 `/api/snapshot` 时读 `cpu.temp` / `gpu.hot` / `gpu.core`（兼容 `temp_c` / `hotspot_c`），映射 `tau = clamp((T − 38) / 52)`，驱动材质色调、内部流速与窑变/朱砂特效。也可手动 `window.setHwmonThermalState(°C)`。
- 绘制全部受 `data-motion-effective` 管控：

  | 档位 | 帧率 | 材质 | 鼠标特效 |
  |---|---|---|---|
  | `full` | 24 FPS | 流动 | 有 |
  | `lite` | 12 FPS | 仍然流动 | 无 |
  | `off` | — | 一帧静态 | 无 |

  `data-fx-paused="true"`（标签页切到后台）与系统 `prefers-reduced-motion` 一律停笔。时间推进按真实帧间隔计算，`lite` 与 `full` 的流速一致，换档不会跳帧或变速。
- 背景画布按材质降采样：云/雾/墨类 0.50–0.62，晶格/点阵类 0.72–0.74，兼顾质感与主线程占用。

### 亮色适配

`blue-porcelain` / `ink-wash` / `gemini-porcelain` 是亮底瓷/纸材质，最容易被写死的浅色坑到：

- 各自定义完整变量组（`--bg` `--surface-bg` `--line` `--txt` `--dim` `--dim2`…）与专属卡片阴影：瓷质用「内壁高光 + 弱下沉」，宣纸用「薄内框」而非光泽。
- 三个 HTML 里原本写死的 `#fff` / `#eaf2ff` / `#c3cbd8` 等共 70 余处统一改回 `var(--txt)` / `var(--dim)`；渐变文字首端从 `#fff` 改 `var(--txt)`，否则白底上整段隐形。
- 多彩渐变按钮（`--grad-flow` 含浅橙/浅粉段）在亮底上白字压不住，`.btn` / `.tool-link.active` / 分段激活胶囊改单色 `--p1` 底 + 反白字。
- 新页面规范：文字颜色只用 `var(--txt)` / `var(--dim)` / `var(--dim2)` / `var(--p1)`，不要写死浅色十六进制；渐变文字用 `var(--txt)` 作首端。

### 旧存档兼容

```js
LEGACY_THEME_MAP = { hypercar: 'sunrise-gold', gold: 'ferrofluid-chrome' }
```

读到旧主题会映射并回写 `localStorage`；默认皮肤 `sunrise-gold`。

## 接口参考

### PC 上的 agent.py（:8090）

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

### 路由器 hub（:8089）

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

### 路由器 uhttpd CGI（:8088，不依赖容器）

| 路径 | 说明 |
|---|---|
| `/cgi-bin/wol` | 发送 WOL 魔术包 |
| `/cgi-bin/peers` | 路由器本地 `easytier-cli -o json peer` |
| `/cgi-bin/dns?action=status\|fallback\|restore` | 全屋 DNS 状态与切换 |

静态文件缓存策略：图片 `max-age=604800`，HTML/CSS/JS 用 `no-cache` + `ETag(mtime-size)`，部署后普通刷新即生效。

## 数据与存储

### 库表分工

| 库 | 位置 | 内容 | 保留 |
|---|---|---|---|
| `hwmon.db` | PC `C:\hwsensor\` | 硬件采样 | 7 天自动清理 |
| `llm_usage.db` | PC `C:\hwsensor\` | 模型调用索引（幂等键 = 顶层 `id`） | 永久 |
| `app_usage.db` | PC `C:\hwsensor\` | `app_daily_stats` / `app_hourly_timeline` | 永久 |
| `hub_longterm.db` | 路由器 `data/` | `hw_samples_1m`（90 天）· `hw_daily_archive` · `llm_daily_archive` · `llm_model_archive` · `app_daily_archive`（后四表永久） | 见左 |

后台线程 `hub_archiver_poller` 每 60 秒采样、每 10 分钟汇总并同步应用日账、每小时清理过期分钟数据。离线设备直接跳过采样（不写空行），曲线上的缺口就是真实关机时段。

### 模型侧性能设计

| 措施 | 原因 |
|---|---|
| 后台线程增量扫描（20 秒）+ `llm_file_state` 记 `mtime/size/byte_offset` | 只读新增字节；632 个文件的 stat 约 90ms，不能放请求路径上 |
| 4 个窗口预聚合到内存缓存（TTL 45 秒） | 运行中现场聚合要 90–110ms（读/扫线程争 GIL + WAL），缓存后降到 2–3ms |
| 常驻读连接 + 20MB 页缓存 | 每次新建连接会让页缓存从零开始，`/all` 首查多花约 110ms |
| 扫描每 120 个文件 `sleep(2ms)` 让出 GIL | 否则一轮扫盘拖慢同进程 HTTP 线程约 100ms，表现为接口尖峰 |
| 覆盖索引 `idx_llm_agg(ts, model, date_str, session_id, …)` | `/all` 三类聚合 49ms → 35ms（索引不回表） |
| 路由器离线缓存 `cache_llm_<dev>_<window>.json` | 台式不常开；离线返回缓存并标 `online:false` + `cached_at` |

实测（笔记本 2.72 万条 / 458MB）：冷启动建库约 3.7s（后台，不阻塞），增量扫描 0 条时约 90ms，接口响应 2–3ms，最差 24ms。

### 跨天逻辑

1. 离线设备的今日数据会残留：缓存里 `today_date` 早于路由器当天时，`window=today` 直接按 0 参与合并（`stale_today=true`，前端横幅明说）。
2. 7/30 天窗口不会自己衰减：离线设备一律用缓存 `all` 窗口的 `timeline` 按当前日期重新过滤求和，不用「关机那天算好的快照」。
3. `by_model` 无法按天切分：缓存里只有全量分布，离线时按窗口 Token 占比等比缩放，并用 `kpi.approx=true` 标注估算。

## 前端实现要点与踩坑

### 布局与刷新

- 切换小结不重排：切换期间画面完全不动，数据到达后复用 DOM 只改 `textContent`；固定 `dailyMeta` / `dailyEvents` / 卡片高度，卡片文本居中、单行省略。
- 曲线刷新从 `tick()` 拆成 `refreshCharts()`，切时间范围只刷曲线。
- 设备离线时清空旧详情与曲线，不显示上一台设备的数据。
- 图表切换复用 Canvas，榜单行预创建后只更新文本，不因刷新重建节点。

### 无刷新切页（common-nav.js）

- 顶栏注入 + `<a>` 拦截 + `fetch` 取目标页 → `DOMParser` 解析 → 只替换 `body` 内容，`header.topbar` 与 `.ambient-bg` 保留。
- 切页时 `teardownView()` 会清掉本视图注册的定时器与监听器（页面脚本通过被包装的 `setInterval` / `addEventListener` 登记）。
- 材质引擎的监听器如果走被包装的 `addEventListener`，切一次页就全被摘掉，鼠标特效直接失效。所以引擎用 `EventTarget.prototype.addEventListener.call(window, …)` 绕开登记。

### 圆环与强制深色

`.ring canvas` 与 `.ring .val` 都强制 `background:transparent!important` 并明确 `z-index`。Chrome 强制深色 / Dark Reader 会注入 `rgb(24,26,27)` 不透明背景，遮住圆环下半或盖掉中心数字（平板上的「半椭圆拱形」）。`drawRing` 按实际 CSS 宽高分别设位图、用较短边定半径，并在 `resize` 时重画。

### 移动端

- 仪表区第三卡跨列消除空位、状态方框三等分等宽消除位数抖动、环形读数下移。
- 顶栏在 ≤1020px 改不透明底并隔离合成层，修复滚动回顶后换肤框「不可见但仍可点」的丢层问题。
- 画廊是视口级浮层，位置由 JS 按触发器实际坐标算（`position:fixed` + `right:auto`），避免祖先 `transform` 把定位基准改掉导致飞出屏幕。

### 亮色主题对比度

- 写死 `#fff` 是最大隐患。已把 4 个页面的硬编码浅色全部换成变量。
- 用程序化对比度扫描（采样 500+ 文本节点，排除渐变背景与 `-webkit-text-fill-color:transparent`）验证：4 页面 × 3 款亮色皮肤 = 0 处低对比。
- CSS 特异性陷阱：`:is(.row .k, .tool-link, …)` 会取参数里最高特异性（`.row .k` = 2 类），把整条规则抬到 2 类，从而反向覆盖后面的 `.tool-link.active` 白字规则 → 深底深字。次级文字兜底必须逐条列选择器，不要图省事塞进一个 `:is()`。

### 「背景不动」的排查顺序

按顺序看，前三项占九成：

1. 看顶栏「动效」右侧的小字（`#hwMotionState`），它直接报实际档位和原因：
   - `背景缓缓流动` → full，正常；
   - `精简氛围` → lite，12 FPS 仍流动，但触屏下没有鼠标特效，观感比桌面安静；
   - `动效已关闭` → 手动选了「关闭」；
   - `系统减少动态已开启` → 系统级开关压过一切（Windows：设置 → 辅助功能 → 视觉效果 → 动画效果；iOS：辅助功能 → 动态效果 → 减弱动态效果；Android：开发者选项 → 动画缩放）；
   - `后台暂停` → 标签页在后台，`document.hidden=true` 时不绘制，属设计。
2. 动效下拉是否被选成「精简」或「关闭」（存 `localStorage.hwmon_motion`）。
3. 是不是在手机/平板上：`pointer: coarse` 时「自动」档会落到 lite（12 FPS 流动、无粒子特效）。想强制看完整效果，把下拉改成「完整」。
4. 确认版本：`curl -s http://<路由器IP>:8089/common-nav.js | wc -c`，v8 后期约 96.8 KB；明显更小说明还是旧文件。
5. 最后才怀疑代码：控制台执行 `window.getHwmonThermalState()`，1.5 秒内 `tau` 不变说明主循环没跑（多半是 `document.hidden`）。