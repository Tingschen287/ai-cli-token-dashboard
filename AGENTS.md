# AGENTS.md — ai-cli-token-dashboard

面向 AI 编码代理的项目说明。项目全部文档与代码注释使用**简体中文**，修改时请保持一致。

## 项目概览

本地 AI 编码 CLI 的 token 消耗看板。**零埋点**：数据全部来自各 CLI 工具自己写的
会话记录（jsonl），不改代理、不装 hook。一屏展示日历热力图 + 按模型 / 按项目
用量排行，随时间窗口联动。

**技术栈**：零第三方依赖。

- 后端：Python 3.8+，只用标准库（`http.server` / `sqlite3` / `urllib` / `json`）
- 前端：原生 HTML/CSS/JS 按职责拆分（`template.html` 骨架 + `style.css` + 8 个 JS），
  无构建步骤、无 npm——`collect.py` 输出时内联拼装回单文件

## 目录结构

| 文件 | 角色 |
|---|---|
| `collect.py` | 扫描、聚合、渲染和 HTTP 服务入口 |
| `coding_plans.py` | 部门个人套餐的独立采集模块；四家官方额度 / 余额接口，凭据与原始响应不出后端 |
| `coding-plans.js` | 标题旁 Coding Plan 按钮、部门额度 dialog、独立刷新和低余额提醒 |
| `coding-plans.local.json.example` | 多账号凭据模板；实际 `.json` 已忽略，禁止提交 |
| `newapi.local.json.example` | ccs 行 new-api 订阅余额的面板访问令牌模板；实际 `.json` 已忽略 |
| `test_coding_plans.py` | 额度解析、旧值保留和凭据隔离回归测试，使用虚构数据 |
| `cache_compare.py` | 独立小脚本：对比 K3 在 kimi 官方接入与 cc-switch 转发下的缓存命中率，复用 `collect.py` 的解析器 |
| `template.html` | 前端骨架模板（约 110 行）：HTML + 三个占位符 `/*__STYLE__*/`、`/*__APP__*/`、`/*__DATA__*/null` |
| `style.css` | 全部样式（含末尾的编辑态样式一节） |
| `brand.js` | BRAND 品牌表 + 配色派生（brandOf/modelColor/LOGOS），纯数据为主 |
| `prices.js` | PRICES 模型价格表（$/1M：输入/输出/缓存读/缓存写，源自 AA 全量 RSC 快照）+ `priceOf()` 模型名归一匹配；新模型重新抓一次全量替换即可 |
| `data.js` | 工具函数 + `applyData` 数据整形 + 时间窗口（activeWindow/windowTotals/winSub/renderMeta） |
| `layout.js` | 布局状态（localStorage 读写/自愈/新来源落位）+ 编辑态全部交互（候补池/增删挪/调占比） |
| `calendar.js` | 额度区渲染 + `renderCalendar` 日历槽位 |
| `charts.js` | 排行板（`rankTab`：Model | Project 两榜同构合并渲染，独享侧栏 2/3 高度）+ 占比饼图 |
| `vps.js` | VPS 带宽胶囊 + 点开的每日分账号堆叠柱状图（可选功能，没配就整块不出现）。配色两套：`VPS_TONES` 是按用量百分比走的四档电量色（绿→金→橙→红），`VPS_PALETTE` 是按账号分色的暖色盘，都刻意留在暖色系里 |
| `sys.js` | 右侧性能卡：CPU/内存/GPU/磁盘四环。独立 2 秒轮询 `/api/sys`，只重画自己，不碰主屏 60 秒重绘；静态打开无服务时 fetch 静默失败、停在骨架 |
| `app.js` | 入口：tooltip、render/renderAll、分段控件、长区间遮罩、口径说明、自动同步、刷新 |
| `vps_probe.py` | 在 VPS 上就地聚合流量的脚本。**不部署到远端**，由 `collect.py` 通过 SSH stdin 喂给 `python3 -` 执行 |
| `vps.local.json.example` | VPS 连接配置模板；实际的 `vps.local.json` 含服务器地址，已在 `.gitignore` 排除 |
| `dashboard.html` | **生成物**，由 `collect.py` 渲染产出，内嵌真实项目名，已在 `.gitignore` 排除，**绝不提交** |
| `cc-token-dashboard.service.example` | systemd 用户服务模板，两处 `%h/path/to/...` 需改成实际路径 |
| `README.md` | 面向用户的完整文档（口径、布局、隐私、部署），改行为时同步更新 |

JS 是朴素全局脚本、无 module 系统，**加载顺序即依赖顺序**（`collect.py` 顶部
`JS_FILES` 常量）：brand → prices → data → layout → calendar → charts → vps →
sys → coding-plans → app。跨文件
调用的都是全局函数；新增文件要同步加进 `JS_FILES`。

**`app.js` 必须排最后**：全部 JS 拼进同一个 `<script>` 块，`app.js` 末尾会立即
执行 `applyData()` / `renderAll()`。函数声明会提升，但 `const` / `let` 不会——
排在 `app.js` 后面的文件，其顶层常量在 `renderAll()` 跑的时候还在暂时性死区里，
一旦被调用路径碰到就是 ReferenceError。`vps.js` 因此排在 `app.js` 之前。

## collect.py 内部结构

- `PROFILES`（文件顶部）：扫描来源配置。key 是看板标识，`format` 决定解析器。增删来源改这里。
- 解析器：都产出统一的 `Row` namedtuple，下游聚合只认 `Row`。
  `parse_claude_file`（`<dir>/projects/**/*.jsonl`，含 subagents 子目录）、
  `parse_grok_file`（`<dir>/sessions/*/*/updates.jsonl`）、
  `parse_kimi_file`（wire.jsonl 的 `usage.record`，time 是 unix 毫秒）、
  `parse_codex_file`（rollout-*.jsonl 的 `token_count`，取 `last_token_usage`
  增量；模型/cwd 在同文件 session_meta/turn_context 行）、
  `parse_opencode_file`（整个存储是单个 SQLite 库 `opencode.db`，message.data
  JSON 的 tokens 字段；实测 input 不含 cache read；cost 是 USD 实估值折 cost_ticks）。
- 文件级增量缓存 `_CACHE`：按 `_sig_of(path)` 签名判断，只重读变过的文件。
  会话记录 append-only，这是安全的。SQLite 是例外：WAL 模式下新数据先进
  `-wal` 文件、主库 mtime 滞后到 checkpoint，所以 `.db` 的签名要把 `-wal`
  的 mtime/size 并进去，否则进行中的 opencode 会话漏刷新。
- `collect()`：聚合产出 `daily` / `models` / `projects`（都带 date 维度，供前端
  按时间窗口重新聚合；`projects` 还带 model 维度——按项目面板的分段是模型）和
  `profiles` 总量。codex 的额度（rollout 文件自带的 `rate_limits`）在扫描时
  顺手读最新文件尾部，挂进 `meta["codex"]["quota"]`，纯本地不联网。oc 的
  `meta["oc"]["zen_limit"]` 同理：Zen 免费模型无限额接口，扫 `opencode.log`
  里的限额报错日，取那些日 muse-spark 全 token 用量（含缓存读）的均值当
  估计分母，日志按 (mtime, size) 缓存。
  个人套餐的 `meta[key]["est_limit"]`（cco 5h+周、codex、grok）是撞墙窗口
  均值：`Row.ts`（epoch 秒）支撑滚动聚合，撞墙信号按来源各扫各——cco 是
  jsonl 里 `isApiErrorMessage` 的 "hit your … limit" 报错行（按 resets 时刻
  去重）、codex 是 rate_limits 窗口 used_percent ≥99（按 resets_at 去重，
  primary=5h / secondary=周）加上 `rate_limit_reached_type` 非空（归入周窗）、
  grok 是
  updates.jsonl 的 `retry_state` failed 且文案为限额类；样本 = 撞墙时刻往前
  5h/7d 的全 token（增量+缓存读+reasoning，各家窗口计量都含缓存读）合计，
  多次撞墙取均值。`_tail_hits` 对 append-only 文件做字节级增量扫描。
  `LIMIT_SINCE` 记套餐切换日，旧套餐的撞墙不进样本（换套餐时改它）。
  cco 周窗撞墙前先撞 5h、没有周报错样本：serve 侧对额度 ≥90% 的进行中周窗
  补一个 live 样本（重置时间反推窗口起点，日粒度近似）。前端不占额度区
  视觉位，悬停额度条时在气泡里显示「窗口上限 ~X（N 个窗口样本均值）」。
- 额度轮询（仅 `--serve` 模式联网）：`QuotaPoller` 后台线程每 180 秒轮询
  cco（Anthropic OAuth `/api/oauth/usage`）、ccs（cc-switch 库里私网地址的
  供应商按 new-api 处理，读 `~/.cc-switch/cc-switch.db` 拿 token；配了
  `newapi.local.json` 里面板「系统访问令牌」的加查账号层——订阅制站点的
  余额在 `/api/subscription/self`（钱包 quota 恒为 0），无订阅站回退钱包
  quota，展示币种跟 `/api/status` 走（JWIPC 是 CNY）；sk- key 是无限令牌、
  不配令牌时只有 billing 接口的累计已用）、grok（CLI 内部 billing 接口，
  OIDC token 从 `~/.grok/auth.json` 复用/refresh）、xai（xAI Management API
  查团队 API key 的本月实付：读 `~/.grok/xai-management-key.env` 的
  `XAI_MANAGEMENT_KEY`/`XAI_TEAM_ID`，用量分析按 `api_key_id` 分组，本机 key
  靠列表 `redactedApiKey` 尾四位与 `~/.grok/xai-api-key.env` 的 key 尾缀匹配
  识别；普通团队 key 调不通，必须 Management Key）。单家失败沿用旧数据。
  codex 不走这里（见上）。
- 性能快照（`SysPoller`，仅 `--serve`）：每 2 秒采一轮本机性能存内存快照，
  `/api/sys` 只做转发。**CPU/内存取 Windows 宿主机视角**（与任务管理器一致）：
  `WindowsSampler` 常驻一个 PowerShell 子进程用 .NET 性能计数器循环吐数
  （powershell.exe 冷启动 2s+，不能每轮起进程；stdout 用 `readline()` 读，
  `for line in` 会撞块缓冲），interop 关闭或进程死透时回退 `/proc` 的 WSL
  视角兜底；GPU 走 WSL 直通的 `/usr/lib/wsl/lib/nvidia-smi`（利用率/显存/
  温度，本来就是 Windows 整卡数据，不存在时为 null）。**磁盘只看读写负载
  不看容量**：负载 = 100 − `LogicalDisk % Idle Time(_Total)`（= 任务管理器
  「活动时间」；这台机器 `PhysicalDisk` 类别被禁用，typeperf 查不到，用
  LogicalDisk 的空闲时间取反）。单块失败填 null，前端显示 `--`。WSL 是
  虚拟机，**CPU 温度与频率拿不到**——`/sys/class/thermal` 只有
  cooling_device，别试着补。
- kimi 的额度挂 kimi code 行：与 cc-switch 的 Kimi 供应商同一个
  `https://api.kimi.com/coding/v1/usages` 接口（同一账号），认证用
  `~/.kimi-code/credentials/kimi-code.json` 的 OAuth token。access_token 只有
  15 分钟，基本每次都要 refresh（`POST https://auth.kimi.com/api/oauth/token`，
  client_id 是 CLI 二进制内置的公共值）；**refresh_token 会轮换，refresh 后必须
  先重读文件再合并写回（原子写、chmod 600）**，否则会顶掉 kimi CLI 的登录态。
- `Snapshot`：后台线程按 `--interval`（默认 60s）定时重扫，`/api/data?force=1`
  立即重扫（页面刷新按钮）。
- `CodingPlanPoller`：从 `coding-plans.local.json` 热读部门账号，独立每 180 秒并发
  查询；`/api/coding-plans?refresh=1` 唤醒采集（最短间隔 15 秒），只在内存留快照。
  返回值严格白名单，错误仅用固定文案。换 Key、删除账号不能沿用另一账号的旧值。
  MiniMax 的 `remaining_percent` 优先于零计数；GLM 支持实际返回的 `CREDIT_LIMIT`。
  JWIPC 是 cc-switch 个人的网关额度，不进部门弹窗，走 ccs 行（见上）。
- `serve()`：`http.server.ThreadingHTTPServer`，含 `/api/data`、`/api/coding-plans`、
  首页与 `/healthz`；本地凭据文件没有 HTTP 路由。

## 前端约定

- 模板占位符：`template.html` 里有 `/*__STYLE__*/`（style.css 内容）、`/*__APP__*/`
  （按 `JS_FILES` 顺序拼接的全部 JS）和 `let DATA = /*__DATA__*/null;` 三处。
  `build_html()`（collect.py）完成前两处替换——serve 每次请求现读现拼（开发改完
  刷新即生效），render 产物仍是内联一切的单文件；`render()`/`serve()` 再把聚合
  JSON 替换进 DATA 占位符，转义 `</` 防止提前闭合 script 块。
- 刷新会整体换掉 `DATA`，所以派生结构在 `applyData()` 里重算，**不能做成顶层 const**
  （`PROFILES`/`byProfile`/`domModel`/`LAYOUT` 都是 `let`，applyData 末尾重算）。
- 布局是一屏到底不滚动。**布局是用户状态不是代码常量**（layout.js）：
  localStorage `tdb-layout-v1` 存 `{ rows: [[{k, w}]...], known: [...] }`，
  `w` 是行内宽度权重（占比），一行 1~N 个槽位按权重分宽；`DEFAULT_LAYOUT`
  是无存档时的默认（cco 独占 / codex+grok / ccs+kimi）。加载时自愈（剔除未知
  key、去重、回收空行）；`known` 记录所有见过的来源——**用户移除进候补池的 key
  仍在 known 里，不会被 `getLayout()` 的新来源自动落位复活**，只有 PROFILES 里
  真正新增的来源才自动追加为新行。候补池不存储，派生 = 有数据来源 − 已放置。
  编辑态（顶栏 Layout 按钮，`body.editing`）：− 徽标移除、HTML5 拖拽换位/开新行、
  相邻槽位分隔条拖像素换算权重、Reset 恢复默认；覆盖层在 `render()` 末尾由
  `applyEditChrome()` 重挂（autoSync 重渲不丢），槽位靠 `data-k` 定位。
  **格子是固定 17px 正方形（`CELL` 常量），绝不拉伸**，列数随槽位宽度能放几列
  放几列（主屏不设上限，遮罩受 weeks 约束）；窄于 940px 退回单栏、槽位纵向堆叠
  （分隔条不显示）。月份轴在每个槽位内部（标题之下），按各自的列几何对齐，
  不是全局共享轴。
  主屏 `state.weeks = 13` 只管右侧排行的统计窗口；半年/一年在 `#longview`
  全屏遮罩里，用独立的 `lvState` 渲染——`renderCalendar(boxId, view, weeks)`、
  `activeWindow(view, weeks)`、`windowTotals(view, weeks)` 都是参数化的，
  主屏和遮罩各调各的，不要回退成读全局 state；遮罩跟随同一份 LAYOUT。
- 顶栏是 `1fr auto 1fr` 三列网格：标题与 Coding Plan 按钮靠左、`#total` 压正中、`.topbar-right`
  （带宽胶囊 + 三个按钮）靠右。**中间列要真的落在中线上就得用网格**——flex
  加 spacer 只能让它居中于「剩余空间」，标题一长就偏。窄屏（≤940px）`#total`
  隐藏，此时**必须连模板一起改成两列**：`display:none` 的元素不再占网格位，
  光藏不改模板的话右侧那组会掉进中间列，贴不到右边缘。≤500px 顶栏分成标题和右控件两行。
- 部门额度用原生 `<dialog>`，Esc 关闭并回焦入口，输入和刷新快捷键不传播到主屏。
  单独请求 `/api/coding-plans`，不依赖 `DATA` 或主屏 signature；每 30 秒读快照。
  缺失窗口显示未知，过了 reset 不能擅自恢复为 100%。所有账号简称必须转义。
- 顶栏一排控件（`.btn` / `.vps-pill` / `#total`）高度统一锁在 `--ctl-h`。三者
  内容字号不同，靠 padding 对不齐，`#total` 内部还是 baseline 对齐会额外撑高
  行盒——直接锁盒高最省事，改一处全跟着走。
- 顶栏两张数字卡共用 `.big` / `.lab` / `.sub` 三个类：`#total` 是全期总账
  （不随 tab 变），面板里 `#metrics` 是当前窗口合计。**改其中一个的排版要两个一起改**。
  次要信息（`#total` 的日期范围、`#metrics` 的统计窗口）都收在 hover 里，
  卡面上只留两个数。
- 「距上次同步多久」长在刷新按钮里，只有数字没有字。`statusUntil` 是这套的关键：
  点刷新后按钮临时显示 Scanning / Updated / Need server，这段时间 `tickSync()`
  必须让位，否则每秒的倒计时会把状态字冲掉。`flash()` 负责设置和清除它。
- 每个来源的结构：标题 + 月份轴 + 格子 + 额度行。额度在格子下方一行排开、
  **不换行**（用户明确要求保留这种方式）；没有色阶图例（用户明确不要）。
- 品牌体系：`BRAND` 表（brand.js 顶部）是唯一出处，19 家模型厂的
  `{ name, color, img, dark? }` 全量内嵌——色值取自 [artificialanalysis.ai 模型页](https://artificialanalysis.ai/models)
  JSON 的 `creator.color`，logo 下载自 AA `/img/logos/` 后 base64 内嵌，完全离线；
  `name` 是 AA creator 名，供 ccs 可用模型选择器显示（zai 另由 `BRAND_DISPLAY`
  覆盖成用户习惯的 GLM）。2026-09 同步新增 mbzuai；upstage / sk-telecom / lg /
  motif-technologies 已不在 AA 页，保留原样不动。
  `BRAND_MATCH` 按模型名前缀命中品牌（顺序即优先级，`mmx` 用 includes）；
  模型名没命中时按 `PROFILE_BRAND` 用来源兜底（cco→anthropic、kimi→kimi、
  codex→openai、grok→xai；ccs 是转发层、oc 是工具，都无兜底，回落行色
  `COLORS[key]`，modelLogo 回落本来源行首标 `LOGOS[key]`）。**以后出新模型不用改代码，
  AA 上新厂时 BRAND 补一行 + BRAND_MATCH 补一条前缀**。
  行色 `COLORS` 直接从 `brandColor(BRAND.x)` 派生（格子、排行条、饼图全联动）；
  **格子按当日增量最高的主力模型着色**（`applyData` 里从 `DATA.models` 派生
  `domModel`；周视图把周内各模型 incr 汇总取最大），tooltip 带 `mostly <model>`，
  单品牌行命中同一品牌、视觉不变，ccs 行能看出哪天在跑哪家。
  日/累计视图格子按 `shade()` 色阶分深浅；**周视图是迷你柱状图**（ChatGPT 桌面端式）：
  列内自下而上填格，格数 = 周总量占可见窗口最大周的比例（满柱 7 格、非零保底 1 格），
  统一色不分深浅（与排行条同源走 `soft()` 混面板底降硬度）。
  `dark` 是深色主题替代色（只有 openai 纯黑会糊进 `#191817` 底色需要，
  反为近白 `#E8E6E1`；定义时按 `matchMedia` 换值，`shade()` 只认 hex）。
  ccs 行色用星爆 logo 次主色青 `#50A0A0`（主色橙与 cco 撞，弃用），
  行首 logo 也是自有图；标题右侧「可用模型」是用户状态（localStorage
  `tdb-ccs-models-v1`，BRAND key 数组，默认 MiniMax + GLM + DeepSeek，kimi 已迁出），
  末尾 + 按钮点开浮层选择器（BRAND 全量，点选即存即渲，Reset 恢复默认，允许清空）。
  状态是 `let CCS_MODELS`，`applyData()` 末尾重算自愈。oc（OpenCode）是工具不是模型厂，
  AA 无条目：行首标用官方品牌资源（opencode.ai/brand 的资源包）的像素 O
  （官方浅底配色 `#211E1E` 环 + `#4B4646` 芯，外套官方米白 `#F1ECEC` 底砖
  保证明暗可读），行色用官方灰 `#4B4646`（深色主题换 `#CFCECD`）。
  注意 AA 的 anthropic 标是黑字 AI 字母砖、xai 标是黑底白色掠影（SpaceXAI），
  不是 Claude 星标和 grok 黑方标——对齐 AA 是用户明确要求。
- 服务模式检测：能 `fetch('/api/data')` 就是服务模式，否则刷新按钮降级为提示
  （复制命令到剪贴板），不报错。

## 运行 / 验证

额度模块有标准库 unittest，其余暂无测试套件或 lint 配置。改动后的验证方式：

```bash
python3 -m unittest -v test_coding_plans  # 虚构数据检查额度口径和凭据边界
python3 collect.py --json /tmp/d.json   # 只导出聚合数据，确认解析/聚合不报错
python3 collect.py                      # 生成 dashboard.html，浏览器打开目检
python3 collect.py --serve 8899         # 起服务，开 http://127.0.0.1:8899 目检
curl -s http://127.0.0.1:8899/healthz   # 服务模式冒烟
```

前端改动靠浏览器目检（README 的「布局」一节列了各分辨率下的预期格子尺寸/行数）。

提交信息沿用现有格式：`[AI开发]<type>(token-dashboard): 中文描述`（type 用
feat/fix/style/refactor）。

## 必须遵守的口径与陷阱

改动聚合逻辑前必读 README「口径」和「必须注意的处理」，核心点：

1. **主指标 = 增量 token** = 非缓存输入 + 输出 + 缓存写入。`cache_read` 单列，
   不参与着色（它占总量九成以上）。
2. **跨工具口径对齐**：Grok 的 `inputTokens` **包含** `cachedReadTokens`，Codex 的
   `input_tokens` **包含** `cached_input_tokens`，Claude 和 OpenCode 的不包含
   （opencode 已实测 86/86 条验证）——解析 grok/codex 时必须减掉，否则虚高一个数量级。
3. **去重方式不同**：Claude 按 `message.id`（流式中间态重复严重，且重复可跨文件，
   所以去重放在 `collect()` 上层而非解析器内）；Grok 按 `session+prompt_id+模型`；
   Kimi（每 turn 一条）、Codex（每次调用一条）和 OpenCode（每条 message 一行、
   整库全量重读）天然不重复。
4. **时间戳统一转本机时区**（`_local_date`）：Claude / Codex 是 UTC ISO 串，Grok
   是 unix 秒，Kimi 和 OpenCode 是 unix 毫秒（/1000）。不转的话本地晚上的会话
   被算到第二天，热力图错位。
5. usage 全为 0 的记录跳过（流式占位）。
6. claude 的 glob 是 `projects/**/*.jsonl`（两层），**不能改成一层**——subagent
   会话在 `subagents/agent-*.jsonl`，漏掉会整体丢失这部分用量。
7. **Codex 取 `last_token_usage`（增量），不取 `total_token_usage`（会话累计）**，
   否则重复计数。
8. **Codex 额度窗口按 `window_minutes` 判定，不要按 primary/secondary 字段名**：
   老 pro 是 primary=周窗；现在 team 是 primary=5h（300 分钟）、secondary=周窗
   （10080 分钟）。`>1440` 分钟才标 `week`，否则一律 `5h`。按字段名硬编码会把
   周额度显示成 5 小时，重置时间也会走短窗的「只显示时刻」。
9. 费用（hover 与两聚合卡统一口径，2026-09-17 起）：grok 用会话记录的
   `costUsdTicks`（**1 USD = 1e10 ticks**，xAI 官方口径——曾误按 1e-9 USD/tick
   折算虚高 10 倍；API key 调用是实际计费，OAuth 订阅会话是名义价值），
   hover 不带 ≈；**其余来源（含 opencode）统一按 `prices.js` 的 AA 价格表
   估价**：输入 + 输出（含 reasoning）+ 缓存读 + 缓存写（无写价按输入价），
   hover 带 ≈。`DAY_COST`（data.js）在 applyData 按 (来源, 日) 聚合，grok
   的官方计费覆盖估价。这是消耗看板，不是账单看板。

## VPS 流量：两个口径不能混

和 token 口径无关，是另一套数，但同样容易看错，单列在此。

| 口径 | 来源 | 用在哪 |
|---|---|---|
| `billing` | vnstat，网卡进出字节 | 顶部大数字 / 百分比 / 环形 |
| `user` | S-UI 的 `stats` 表，按账号 | 柱状图 / 图例 |

`billing` 约为 `user` 的 **2 倍**——一个字节进来再出去，网卡上算两遍。
**两者相加或互相比较都是错的。** 顶部数字必须用 `billing`（它决定会不会超额），
柱状图必须用 `user`（只有它能分到人）。历史上正是混用这两个数，才有过「面板显示
174GB、脚本算出 547GB」的争论。

vnstat 只能从装的那天起记，之前的日子按 `user × 倍数` 估算。倍数一开始是理论值
`2.0`，等 vnstat 攒够完整整天后自动改用实测值（同期两个口径的累计量相除），估算
部分会自己缩小到零。倍数**刻意不逐日匹配**：VPS 一般跑 UTC、S-UI 按 `tz_offset`
切天，逐日对齐会被 8 小时错位干扰，用整段累计量求比值就绕开了。

远端脚本 `vps_probe.py` 不部署到 VPS，由 `collect.py` 通过 SSH stdin 喂给
`python3 -`。**不要改成在 VPS 上常驻一个 HTTP 接口**——那台机器通常是代理节点，
多开一个对外端口就多一个被扫描的入口，而聚合本身只要 0.5 秒，SSH 完全够用。

## 安全与隐私

- **服务默认只绑 `127.0.0.1`，不要改成 `0.0.0.0`**。聚合结果含真实项目名和目录
  路径；WSL `networkingMode=mirrored` 下绑 0.0.0.0 等于暴露给整个局域网。
- `dashboard.html` 内嵌真实项目名与用量，已在 `.gitignore` 排除。分享前必须
  确认没有不该外传的内容。`.gitignore` 里 `*.json` 也是同理（`--json` 导出物）。
- 静态扫描完全离线；唯一联网行为是 `--serve` 模式的额度轮询（读各平台 token：
  `~/.claude-official/.credentials.json`、`~/.cc-switch/cc-switch.db`、
  `~/.grok/auth.json`、`~/.grok/xai-management-key.env`（Management Key 与
  team ID）、`~/.kimi-code/credentials/kimi-code.json`）。grok 和 kimi 的
  refresh 都会写回各自凭证文件（先重读合并、chmod 600），修改这段逻辑时注意
  不要顶掉 CLI 自己的登录态。
- grok 额度走未公开的 CLI 内部 billing 接口，字段可能随时变化，解析必须容错。

## 部署

systemd 用户服务（见 `cc-token-dashboard.service.example` 和 README「开机自启」）：
`Restart=always` + `RestartSec=5` 崩溃自动拉起，`Nice=10` 不抢前台 CPU。WSL 下
需要 `sudo loginctl enable-linger $USER` 才能无登录会话自启。改完代码用
`systemctl --user restart cc-token-dashboard` 生效。
