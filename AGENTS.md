# AGENTS.md — ai-cli-token-dashboard

面向 AI 编码代理的项目说明。项目全部文档与代码注释使用**简体中文**，修改时请保持一致。

## 项目概览

本地 AI 编码 CLI 的 token 消耗看板。**零埋点**：数据全部来自各 CLI 工具自己写的
会话记录（jsonl），不改代理、不装 hook。一屏展示日历热力图 + 按模型 / 按项目
用量排行，随时间窗口联动。

**技术栈**：零第三方依赖。

- 后端：Python 3.8+，只用标准库（`http.server` / `sqlite3` / `urllib` / `json`）
- 前端：原生 HTML/CSS/JS 按职责拆分（`template.html` 骨架 + `style.css` + 6 个 JS），
  无构建步骤、无 npm——`collect.py` 输出时内联拼装回单文件

## 目录结构

| 文件 | 角色 |
|---|---|
| `collect.py` | 看板唯一的 Python 文件：扫描、聚合、渲染、HTTP 服务全部在此 |
| `cache_compare.py` | 独立小脚本：对比 K3 在 kimi 官方接入与 cc-switch 转发下的缓存命中率，复用 `collect.py` 的解析器 |
| `template.html` | 前端骨架模板（约 110 行）：HTML + 三个占位符 `/*__STYLE__*/`、`/*__APP__*/`、`/*__DATA__*/null` |
| `style.css` | 全部样式（含末尾的编辑态样式一节） |
| `brand.js` | BRAND 品牌表 + 配色派生（brandOf/modelColor/LOGOS），纯数据为主 |
| `data.js` | 工具函数 + `applyData` 数据整形 + 时间窗口（activeWindow/windowTotals/winSub/renderMeta） |
| `layout.js` | 布局状态（localStorage 读写/自愈/新来源落位）+ 编辑态全部交互（候补池/增删挪/调占比） |
| `calendar.js` | 额度区渲染 + `renderCalendar` 日历槽位 |
| `charts.js` | 按模型/按项目排行 + 占比饼图 |
| `app.js` | 入口：tooltip、render/renderAll、分段控件、长区间遮罩、口径说明、自动同步、刷新 |
| `dashboard.html` | **生成物**，由 `collect.py` 渲染产出，内嵌真实项目名，已在 `.gitignore` 排除，**绝不提交** |
| `cc-token-dashboard.service.example` | systemd 用户服务模板，两处 `%h/path/to/...` 需改成实际路径 |
| `README.md` | 面向用户的完整文档（口径、布局、隐私、部署），改行为时同步更新 |

JS 是朴素全局脚本、无 module 系统，**加载顺序即依赖顺序**（`collect.py` 顶部
`JS_FILES` 常量）：brand → data → layout → calendar → charts → app。跨文件调用
的都是全局函数；新增文件要同步加进 `JS_FILES`。

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
  顺手读最新文件尾部，挂进 `meta["codex"]["quota"]`，纯本地不联网。
- 额度轮询（仅 `--serve` 模式联网）：`QuotaPoller` 后台线程每 180 秒轮询
  cco（Anthropic OAuth `/api/oauth/usage`）、ccs（cc-switch 库里的 MiniMax
  供应商，读 `~/.cc-switch/cc-switch.db` 拿 token）、grok（CLI 内部 billing 接口，
  OIDC token 从 `~/.grok/auth.json` 复用/refresh）。单家失败沿用旧数据。
  codex 不走这里（见上）。
- kimi 的额度挂 kimi code 行：与 cc-switch 的 Kimi 供应商同一个
  `https://api.kimi.com/coding/v1/usages` 接口（同一账号），认证用
  `~/.kimi-code/credentials/kimi-code.json` 的 OAuth token。access_token 只有
  15 分钟，基本每次都要 refresh（`POST https://auth.kimi.com/api/oauth/token`，
  client_id 是 CLI 二进制内置的公共值）；**refresh_token 会轮换，refresh 后必须
  先重读文件再合并写回（原子写、chmod 600）**，否则会顶掉 kimi CLI 的登录态。
- `Snapshot`：后台线程按 `--interval`（默认 60s）定时重扫，`/api/data?force=1`
  立即重扫（页面刷新按钮）。
- `serve()`：`http.server.ThreadingHTTPServer`，路由只有 `/api/data`、`/`、
  `/healthz`，其余 404。

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
- 每个来源的结构：标题 + 月份轴 + 格子 + 额度行。额度在格子下方一行排开、
  **不换行**（用户明确要求保留这种方式）；没有色阶图例（用户明确不要）。
- 品牌体系：`BRAND` 表（brand.js 顶部）是唯一出处，18 家模型厂的
  `{ color, img, dark? }` 全量内嵌——色值取自 [artificialanalysis.ai 模型页](https://artificialanalysis.ai/models)
  JSON 的 `creator.color`，logo 下载自 AA `/img/logos/` 后 base64 内嵌，完全离线。
  `BRAND_MATCH` 按模型名前缀命中品牌（顺序即优先级，`mmx` 用 includes）；
  模型名没命中时按 `PROFILE_BRAND` 用来源兜底（cco→anthropic、kimi→kimi、
  codex→openai、grok→xai；ccs 是转发层、oc 是工具，都无兜底，回落行色
  `COLORS[key]`，modelLogo 回落本来源行首标 `LOGOS[key]`）。**以后出新模型不用改代码，
  AA 上新厂时 BRAND 补一行 + BRAND_MATCH 补一条前缀**。
  行色 `COLORS` 直接从 `brandColor(BRAND.x)` 派生（格子、排行条、饼图全联动）；
  **格子按当日增量最高的主力模型着色**（`applyData` 里从 `DATA.models` 派生
  `domModel`；周视图把周内各模型 incr 汇总取最大），tooltip 带 `mostly <model>`，
  单品牌行命中同一品牌、视觉不变，ccs 行能看出哪天在跑哪家。
  `dark` 是深色主题替代色（只有 openai 纯黑会糊进 `#191817` 底色需要，
  反为近白 `#E8E6E1`；定义时按 `matchMedia` 换值，`shade()` 只认 hex）。
  ccs 行色用星爆 logo 次主色青 `#50A0A0`（主色橙与 cco 撞，弃用），
  行首 logo 也是自有图，标题右侧只列当前可用模型
  （MiniMax + GLM + DeepSeek，kimi 已迁出）。oc 同理是自有绿 `#43A047` +
  绿底白 `<>` 自有标（OpenCode 是工具不是模型厂，AA 无条目）。
  注意 AA 的 anthropic 标是黑字 AI 字母砖、xai 标是黑底白色掠影（SpaceXAI），
  不是 Claude 星标和 grok 黑方标——对齐 AA 是用户明确要求。
- 服务模式检测：能 `fetch('/api/data')` 就是服务模式，否则刷新按钮降级为提示
  （复制命令到剪贴板），不报错。

## 运行 / 验证

没有测试套件、没有 lint 配置。改动后的验证方式：

```bash
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
8. 费用：grok 记 `costUsdTicks`（按 1e-9 USD/tick 推定，**名义**值）；opencode 的
   `cost` 字段是它按 provider 报价算的 USD 实估值，同样只作参考。其余来源没有
   费用字段。这是消耗看板，不是账单看板。

## 安全与隐私

- **服务默认只绑 `127.0.0.1`，不要改成 `0.0.0.0`**。聚合结果含真实项目名和目录
  路径；WSL `networkingMode=mirrored` 下绑 0.0.0.0 等于暴露给整个局域网。
- `dashboard.html` 内嵌真实项目名与用量，已在 `.gitignore` 排除。分享前必须
  确认没有不该外传的内容。`.gitignore` 里 `*.json` 也是同理（`--json` 导出物）。
- 静态扫描完全离线；唯一联网行为是 `--serve` 模式的额度轮询（读各平台 token：
  `~/.claude-official/.credentials.json`、`~/.cc-switch/cc-switch.db`、
  `~/.grok/auth.json`、`~/.kimi-code/credentials/kimi-code.json`）。grok 和 kimi 的
  refresh 都会写回各自凭证文件（先重读合并、chmod 600），修改这段逻辑时注意
  不要顶掉 CLI 自己的登录态。
- grok 额度走未公开的 CLI 内部 billing 接口，字段可能随时变化，解析必须容错。

## 部署

systemd 用户服务（见 `cc-token-dashboard.service.example` 和 README「开机自启」）：
`Restart=always` + `RestartSec=5` 崩溃自动拉起，`Nice=10` 不抢前台 CPU。WSL 下
需要 `sudo loginctl enable-linger $USER` 才能无登录会话自启。改完代码用
`systemctl --user restart cc-token-dashboard` 生效。
