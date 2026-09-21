# ai-cli-token-dashboard

本地 AI 编码 CLI 的 token 消耗看板。**零埋点**——数据全部来自这些工具自己写的
会话记录，不用改代理、不用装 hook、不用注册任何服务。

一屏看完：日历热力图 + 按模型 / 按项目的用量排行，随时间窗口联动。

目前覆盖六个来源，都可以在 `collect.py` 顶部的 `PROFILES` 里增删：

| key | 看板显示名 | 默认目录 | 说明 |
|---|---|---|---|
| `cco` | Claude Official | `~/.claude-official` | Claude Code 官方账号 |
| `kimi` | Kimi | `~/.kimi-code` | Kimi Code CLI 官方接入 |
| `codex` | ChatGPT | `~/.codex` | Codex CLI |
| `ccs` | CC-Switch | `~/.claude` | Claude Code 经 CC-Switch 走第三方 |
| `grok` | Grok | `~/.grok,~/.grok-team` | Grok CLI（个人 + 团队两套目录合并成一行） |
| `oc` | OpenCode | `~/.local/share/opencode` | OpenCode（SQLite 存储） |

> **不消耗 token**：只读本地 jsonl 文件，不联网、不调 API。刷新多少次都是零费用。
> 单次全量扫描约 1.2 秒 / 28MB 内存；之后走文件级增量缓存，只重读 mtime 变过的
> 文件，常态刷新 **0.03 秒**。

数字一律用 K / M / B——模型计价本来就按 per-million 报价，M 和成本直觉同刻度。
卡片上悬停可看不带缩写的精确值。

## 快速开始

```bash
python3 collect.py --serve     # 起服务，默认 8899
```

打开 http://127.0.0.1:8899 即可。**无第三方依赖**，Python 3.8+ 只用标准库。

## 数据隐私

所有数据留在本地，不上传任何地方。但有两点值得注意：

1. **`dashboard.html` 是生成物，内嵌你的真实项目名和用量**，已在 `.gitignore` 里
   排除。要把这个文件分享给别人前，先确认里面没有不该外传的项目名——项目名来自
   会话记录里的工作目录。
2. **服务默认只绑 `127.0.0.1`，不要改成 `0.0.0.0`**。聚合结果含项目名和目录路径，
   不该暴露到局域网。尤其在 WSL `networkingMode=mirrored` 下，WSL 直接持有和宿主
   同网段的真实 IP，绑 `0.0.0.0` 就等于把这些信息发给整个网段。

## 开机自启（systemd 用户服务）

```bash
mkdir -p ~/.config/systemd/user
cp cc-token-dashboard.service.example ~/.config/systemd/user/cc-token-dashboard.service
# 编辑其中两处 %h/path/to/... 改成实际路径
systemctl --user daemon-reload
systemctl --user enable --now cc-token-dashboard
```

常用命令：

```bash
systemctl --user status cc-token-dashboard    # 看状态
systemctl --user restart cc-token-dashboard   # 改完代码重启
journalctl --user -u cc-token-dashboard -f    # 看日志
systemctl --user disable --now cc-token-dashboard   # 不想要了
```

崩溃 5 秒后自动拉起，`Nice=10` 不与前台程序抢 CPU。

若希望没有登录会话时也随系统启动：`sudo loginctl enable-linger $USER`。

### 在 WSL 里跑、从 Windows 访问

`networkingMode=mirrored` 下 Windows 与 WSL 共享 loopback，Windows 浏览器直接开
`http://localhost:8899` 就能到，不用配端口转发，服务也仍然只绑回环。

WSL 不会随 Windows 开机自启，需要开一次 WSL（任意终端窗口即可，或在 Windows 任务
计划里加一条 `wsl.exe -d <发行版> -- true`）。起来之后服务就随之常驻——若
`.wslconfig` 里设了 `vmIdleTimeout=-1`，它也不会空闲退出。

## 手动用法

两种模式，按需要选：

```bash
# A. 服务模式——页面上的「刷新」按钮点一下就重新扫描（推荐日常用）
python3 collect.py --serve         # 默认 8899，然后开 http://127.0.0.1:8899
python3 collect.py --serve 9000    # 换端口

# B. 静态模式——生成自包含单文件，可离线打开、可拷到别的机器
python3 collect.py

python3 collect.py --json d.json   # 只导出聚合数据，喂给别的工具
```

**为什么刷新按钮需要服务模式**：浏览器沙箱不允许静态页面读取本地目录或执行
Python，所以按钮必须有个后端替它去扫描。服务只绑 `127.0.0.1`（聚合结果含项目名
和目录路径，不该暴露到局域网），每次请求 `/api/data` 都重新扫描一遍。

静态模式下按钮不会失效或报错，而是降级成一行提示，告诉你跑哪条命令，并把命令
复制到剪贴板。页面上按 `r` 键等同点击刷新。

目录不存在的来源会自动跳过，不会画出一整行空白日历（布局里留着它的位置，
数据回来了自动恢复上屏）。

要增删来源，改 `collect.py` 顶部的 `PROFILES`，`format` 决定用哪个 reader。

## 性能卡（本机实时）

右侧三块卡中间的 **Performance**：CPU / 内存 / GPU / 磁盘四个环形仪表，
2×2 排布，进度色与额度条同款四档（绿→橙→金→红）。

- **实时的**：和 token 数据 60 秒一轮不同，后端 `SysPoller` 线程每 2 秒采样一次，
  前端单独 2 秒轮询 `/api/sys`，只重画这一块、不动整屏。
- **全部是 Windows 宿主机视角**（与任务管理器同口径）：CPU 占用与逻辑核数、
  物理内存总量/可用（63 GB 那个数）由常驻 PowerShell 进程经 .NET 性能计数器
  采集——WSL 的 `/proc` 只有 VM 配额，不是真内存；GPU 走 WSL 直通的
  `nvidia-smi`（利用率大环，显存 + 温度在副行，悬停看显卡名）；磁盘只看
  **读写负载**（活动时间，容量不看）——C: + D: 合并成一个环，样式与其他环一致。
- **CPU 温度拿不到**：WSL2 是虚拟机，没有硬件传感器入口；GPU 温度跟着
  `nvidia-smi` 有了。CPU 环副行显示逻辑核数。
- 仅服务模式有数据；静态打开时这块停留在灰色骨架，不打扰其他功能。

## 部门 Coding Plan 额度（可选）

标题右侧的 **Coding Plan** 按钮打开独立「部门额度」弹窗。个人套餐按账号展示，
不与主屏 CLI 会话消耗混算。沿用主页面明暗主题；桌面两列卡片，手机单列滚动。

```bash
cp coding-plans.local.json.example coding-plans.local.json
chmod 600 coding-plans.local.json
# 编辑各账号的 api_key，不添加 Bearer 前缀；多账号复制对应条目并修改 label
python3 collect.py --serve
```

已有配置文件时直接编辑，勿再次复制覆盖。配置每轮自动重读，填好后点击「刷新额度」
即可生效。`coding-plans.local.json` 已被 Git 忽略，Key 只在后端用于官方接口认证；
接口返回只包含归一化的额度和账号简称，不含 Key、用户资料或原始响应。

| 平台 | Key 类型 | 展示内容 |
|---|---|---|
| MiniMax | 个人 Token Plan 订阅 Key | 5 小时、周窗口剩余比例及恢复时间 |
| Kimi | 个人 Kimi Code Key | 接口返回的短窗口、周额度及恢复时间 |
| GLM | 个人 Coding Plan Key | `CREDIT_LIMIT` / `TOKENS_LIMIT` 窗口；返回时也显示 MCP 额度 |
| DeepSeek | 开放平台 API Key | 各币种可用余额、充值余额、赠金 |

进度条统一表示**剩余**，少于 20% 提醒、少于 10% 告急；任一窗口偏低都会影响卡片
状态和标题旁提示点。DeepSeek 不画没有分母的百分比条，默认低余额提醒线为 ¥50
（美元余额为 $10），可在卡片里修改；提醒线仅保存在当前浏览器。

后端每 180 秒并发查询一次，前端每 30 秒同步快照。弹窗刷新只触发额度查询，不重扫
会话记录；手动查询有 15 秒最短间隔。每张卡显示最后成功时间，失败保留同一凭据的
旧值并标「数据已过期」；没有有效数据时显示未知，不以 0 或 100% 代替。
清空或更换 Key 不会继承旧账号数据。到重置时间仅提示等待更新，不自行将额度回满。

该功能仅在服务模式可用，数据快照只保存在内存，未采集历史趋势或实现续费操作。
静态导出的 HTML 不包含部门额度数据。额度接口为 `/api/coding-plans`；前端不读取
凭据文件，也没有写配置接口。供应商的控制台链接可从各卡片直接打开。

回归检查：`python3 -m unittest -v test_coding_plans`（虚构数据，无外部调用）。

## 记录格式

**claude** —— `<dir>/projects/<项目>/<会话>.jsonl`
每行一条 assistant 消息，用量在 `message.usage`。

**grok / grok-team** —— `<dir>/sessions/<urlencode(cwd)>/<会话>/updates.jsonl`
`turn_completed` 事件的 `params.update.usage`，一条 = 一次提问的整个 agent loop，
还带 `modelUsage` 分模型拆分、`reasoningTokens` 和 `costUsdTicks`。

**kimi** —— `<dir>/sessions/<wd_*>/session_*/agents/*/wire.jsonl` 的
`usage.record` 事件（Kimi Code CLI），time 是 unix 毫秒。

**codex** —— `<dir>/sessions/YYYY/MM/DD/rollout-*.jsonl` 的 `token_count` 事件，
取 `info.last_token_usage`（每次 LLM 调用的增量；`total_token_usage` 是累计值不取）。
模型和 cwd 在同文件的 session_meta / turn_context 行。事件里还自带 `rate_limits`
（账号额度快照），codex 行的额度显示就从这里白捡，不联网。

**opencode** —— 整个存储是一个 SQLite 库 `<dir>/opencode.db`（WAL 模式）。
`message` 表的 `data` JSON 里 `role=assistant` 的条带
`tokens{input,output,reasoning,cache{read,write}}`、`modelID` 和 unix 毫秒时间戳，
项目取 `session.directory`。实测 `input` 不含缓存读（同 Claude 口径不用减）。
`cost` 字段是 USD 实估值，和 grok 一样以 ≈$ 名义值展示。

`cache_compare.py` 复用 kimi/claude 两个解析器：对比 K3 在 kimi code cli 官方接入与
claude code + cc-switch 转发下的 prompt 缓存命中率，
`python3 cache_compare.py [--since 日期]`，同样只读本地记录、不联网。

## 布局：一屏到底，不滚动

主从分栏。左侧热力图占 62%，右侧是卡片和两张排行榜：

```
┌────────────────────────────────────────────────────┐
│ Token 活动  增量 2.17亿 …      数据 12 秒前 [↻] [ⓘ] │
├─────────────────────────────────┬──────────────────┤
│ [每日|每周|累计]    [6 mo/1 yr▸] │ ┌──────┬───────┐ │
│  6月    7月    8月  │  6月 7月 8月│ │ cco  │ ccs   │ │
│ ● cco    ░░░░░░▓█▓░             │ ├──────┼───────┤ │
│ ● codex  ░▓███▓░  │ ● grok ░▓██░│ │ grok │ 合计  │ │
│ ● ccs    ░░▓██▓░  │ ● kimi ░░░▓░│ └──────┴───────┘ │
│                                 │ 按模型 / 性能    │
│                                 │ 按项目           │
└─────────────────────────────────┴──────────────────┘
```

**右侧两块：Performance（1/3 高）+ 排行板（2/3 高）**。Performance 是本机
性能实时镜像（CPU / 内存 / GPU / 磁盘四环），下面细说。排行板顶部
**Model | Project** tab 切换两张榜（同构合并，2/3 高度能一屏放下全部行）；
条形只显示前 7 名 + 一条 **Other** 汇总（长尾不再滚动，hover 看构成），
下方剩余空间是占比**空心环**——扇区标 logo（≥5% 才有位置），环心是
当前窗口的总量。

**默认三行分组：cco 独占一行；codex + grok 一行；ccs + kimi 一行。**
布局不是写死的——点顶栏 **Layout** 进编辑态（iOS 组件式）：

- 槽位右上角 **−** 把来源移除到底部**候补池**；池里组件点击或拖入即上屏；
- 按住槽位拖动换位置（行内换序、跨行、拖到行间虚线条上开新行）；
- 同一行相邻槽位之间有**分隔条**，左右拖动调占比（一行可以放 3 个甚至更多来源）；
- **Reset** 恢复默认布局，Esc 或 Done 退出。

布局存浏览器 `localStorage`（`tdb-layout-v1`），两种模式共用；`PROFILES` 里新增
来源时不用管布局——第一次打开会自动作为新行追加到末尾，再进编辑态挪位置。

**格子是固定 17px 的正方形，绝不拉伸；列数随槽位宽度能放几列放几列**
（一列 = 一周）——槽位越宽列数越多、显示的周数越多。月份轴在每个槽位内部、
标题之下，按各自的列几何对齐。

**主屏固定 3 个月**（指右侧排行的统计窗口；日历格子本身按宽度尽量多显示）。
半年 / 一年收进右上角的「6 mo / 1 yr ▸」全屏遮罩：点开后用整个屏幕渲染，
Esc 或 ✕ 关掉。遮罩里有独立的视图和范围切换，不影响主屏状态。

**每个来源：标题 + 月份轴 + 格子 + 额度行。** 额度在格子下方一行排开、不换行
（窄屏截断不挤压格子），是账号实时状态，不随时间窗口变：
cco / ccs / grok / kimi 走后端慢轮询（仅服务模式；kimi 用 CLI 的 OAuth 凭证访问
官方 `/v1/usages`），codex 直接从会话文件里读。grok 行在配好 xAI Management
Key（`~/.grok/xai-management-key.env`）后，还会额外显示本机团队 API key 的
本月实付（悬停看全队合计）。cco / codex / grok 行悬停额度条时会附「窗口上限 ~X」：撞墙时刻
往前一个窗口的用量均值（cco 周窗无报错样本时用当前 ≥90% 的高水位窗口补），
是套餐窗口 token 上限的估计值，不是官方数字。仅统计当前套餐（换过套餐的
从切换日起算）。
没有色阶图例——「越深越多」看格子本身就够直观。

排行榜不按高度裁行数——放不下就滚动（裁掉的长尾根本看不到）。

窄于 940px 时自动退回单栏并允许滚动（一屏放不下就别硬塞）。

## 视图

| 视图 | 左侧形态 | 右侧统计窗口 |
|---|---|---|
| 每日 | 日历格子，一格 = 一天 | 只统计最后一天 |
| 每周 | 同一套格子，一列 = 一周的迷你柱状图：自下而上填格、格数 ∝ 周总量（可见窗口内最大周满 7 格），统一色不分深浅（混面板底降硬度）；hover 整列一起亮 | 只统计最后一周 |
| 累计 | 折线（累计值单调递增，画成热力图会整片最深色） | 当前范围全部 |

**右侧四张卡片和两张排行榜都跟随左侧的时间窗口**，卡片「合计」的副标题和面板标题
都会显示当前统计的是哪一段（`2026-08-12` / `2026-08-09 起那周` / `近 3 个月`）。
主屏数据池固定为近 3 个月；要看半年/一年点「6 mo / 1 yr ▸」进全屏遮罩。
所以「今天我在哪个项目烧得多」和「这一年总账」是同一个控件切出来的。

只有顶栏那行是**全期**总账，固定不动，作为切 tab 时的对照锚点。

**按项目以项目为组**，一个项目一行，条形按模型分段堆叠——段色与按模型排行同源，
一眼看出这个项目烧在哪些模型上。悬停任意一段显示该模型的具体量和占比
（`ai · kimi-k2 10.6M · 占 61%`）。

两张排行面板右上角都有 **List / Share** 切换。List 就是上面的条形（按模型是
单色条 + logo，按项目是分段堆叠条）；Share 是占比饼图——单个大饼，名字直接标在
扇区里，放不下就截断（项目名的区分度在结尾所以留结尾，模型名留开头），具体用量
和占比看悬停。扇区颜色与列表段同源：取该项里用量最大那一段的品牌色。排行面板的
大块填充（条、堆叠段、扇区）都向面板底色混 15% 柔化，logo 等小面积点缀仍用原色。

口径说明收在顶栏的 ⓘ 里，点击展开、Esc 关闭。

配色：全部对齐 [artificialanalysis.ai 模型页](https://artificialanalysis.ai/models)——模板里内嵌了
AA 全部 19 家模型厂的品牌色和 logo（`BRAND` 表），格子、行首图标、排行条、饼图
都从这里取色；新模型按名字前缀自动命中（deepseek / qwen / gemini / llama /
mistral 等开箱即有），`ccs` 是转发层、AA 无条目，用自有 logo 的青色区分。
ccs 槽位标题右侧的「可用模型」可以自己改：点 logo 区末尾的 + 按钮，
在浮层里点选全部品牌（默认 MiniMax + GLM + DeepSeek），改完立即生效，
存在浏览器本地，下次打开还在；Reset 恢复默认。
格子按当天增量最高的主力模型着色（悬停显示 `mostly <模型名>`），ccs 行能直接
看出哪天在跑 MiniMax、哪天在跑 GLM / Kimi。

## 口径

**主指标 = 增量 token = 非缓存输入 + 输出 + 缓存写入**

为什么不用总量：`cache_read` 占九成以上（ccs 侧 8.29 亿 / 9.9 亿，grok 侧 92.3%）。
按总量着色的话，热力图反映的只是会话有多长、缓存命中多少，而不是实际干了多少活。
所以 `cache_read` 单列显示，不参与着色。

## 必须注意的处理

1. **跨工具口径对齐** —— Grok 的 `inputTokens` **包含** `cachedReadTokens`，
   Codex 的 `input_tokens` **包含** `cached_input_tokens`，Claude 的
   `input_tokens` **不包含**。不减掉的话 grok/codex 的增量会虚高一个数量级。
2. **去重方式不同** —— Claude 按 `message.id`（流式中间态重复严重，实测官方侧
   1516 行里 1024 行是重复的，不去重虚高约 2.5 倍）；Grok 按
   `session+prompt_id+模型`，实测 0 重复，数据天然干净；Kimi 每 turn 一条
   `usage.record`、Codex 每次调用一条 `token_count`，都天然不重复，不用去重。
3. **三种时间戳** —— Claude / Codex 是 UTC ISO 串（尾部 Z），Grok 是 unix 秒，
   Kimi 和 OpenCode 是 unix **毫秒**，都要转本机时区（毫秒还要 /1000）。不转的话
   本地晚上的会话会被算到第二天，热力图整体错位一格。
4. **零计量记录跳过** —— 部分记录 usage 全为 0（流式占位），计入会污染活动天数。
5. **Codex 取增量不取累计** —— `token_count` 里的 `total_token_usage` 是会话
   累计值，直接用会重复计数；必须取 `last_token_usage`（每次调用的增量）。

## VPS 带宽（可选）

顶栏多一枚胶囊显示自己 VPS 这个计费周期用掉多少流量，点开是每日分账号的堆叠
柱状图。**不配置就完全不出现**，其余功能不受影响。

```bash
cp vps.local.json.example vps.local.json   # 改成自己的地址
python3 collect.py --vps-once              # 先单跑一次，确认能连上
```

| 字段 | 含义 |
|---|---|
| `host` | ssh 目标，如 `root@1.2.3.4`。环境变量 `VPS_SSH_HOST` 可覆盖 |
| `quota_gb` | 套餐额度，默认 1000 |
| `reset_day` | 每月流量重置日，默认 25 |
| `tz_offset` | 看板使用者的时区偏移，按它切天，默认 8 |

前提：本机能**免密 ssh** 到那台机器，远端装了 [S-UI](https://github.com/alireza0/s-ui)
和 `vnstat`（`apt install vnstat`，约 4MB 内存）。

### 为什么走 SSH 而不是在 VPS 上开接口

不是性能问题——实测 152 万行 stats 表聚合只要 0.5 秒，1 核 2G 的机器负载 0.00。
是**暴露面**问题：VPS 通常是代理节点，多开一个对外 HTTP 端口就多一个被扫描、
被探测的入口，而 SSH 本来就是现成的加密通道。

所以 `collect.py` 每 10 分钟把 `vps_probe.py` 的**源码**通过 SSH stdin 喂给远端
`python3 -` 执行，聚合在远端做完，回来的只有 2~3 KB JSON。**远端不落盘、不常驻、
不开端口**，改完本地脚本立刻生效，不用重新部署。单次失败沿用上一次的数据并标记。

### 两个口径不能混

这是最容易看错的地方，页面脚注里也写死了：

| 口径 | 来源 | 用在哪 | 说明 |
|---|---|---|---|
| **billing** | vnstat（网卡进出字节） | 顶部大数字、百分比、环形 | 和服务商账单一致，决定会不会超额 |
| **user** | S-UI 的 `stats` 表 | 柱状图、按账号图例 | 能分到人，但天生约为账单的**一半** |

差一倍是因为**一个字节进来再出去，网卡上算两遍**。两者相加或直接比较都是错的。

百分比像电量一样分四档变色，用得越多越往红走：**<50% 绿 / 50~75% 金 /
75~90% 橙 / ≥90% 红**。胶囊、环形、大数字三处同时变，余额告急时一眼看得见。

vnstat 只能从装的那天起记，装之前的日子按 `S-UI × 倍数` 估算。倍数一开始用理论值
`2.0`，等 vnstat 攒够完整的整天后自动改用实测值（拿同期两个口径的累计量相除），
估算的部分会自己缩小到零。页面脚注会显示当前用的是哪种。

倍数刻意**不逐日匹配**：VPS 一般跑 UTC，S-UI 按 `tz_offset` 切天，逐日对齐会被
8 小时错位干扰，用整段累计量求比值就没这个问题。

## 关于费用

格子 / 行标题 / 两张合计卡的悬停统一显示：**输入**（非缓存）、**输出**（含
reasoning）、**成本**、缓存读、命中率。成本口径：Grok 用会话记录的
`costUsdTicks`，按 **xAI 官方口径 1 USD = 1e10 ticks** 折算（2026-09 经
Management API 账单核对确认，此前误按 1e-9 USD/tick 估算、虚高 10 倍）——API
key 直连是实际计费，OAuth 订阅会话是名义价值；**其余来源（含 OpenCode）**按
[Artificial Analysis](https://artificialanalysis.ai/models) 价格表估价（输入 +
输出 + 缓存读 + 缓存写，无缓存写价的按输入价计），悬停带 `≈` 标记。你的实际
套餐 / 免费额度 / 折扣估价并不知道——这是消耗看板，不是账单看板。

Claude 侧完全无法算钱：官方账号是订阅制；第三方经 CC-Switch 代理，真实计费在代理
侧，本地 jsonl 只有 token 数没有单价，且 MiniMax / GLM / k3 价格各不相同。
Kimi / Codex 同样是订阅制，本地记录没有费用字段。

**这是消耗看板，不是账单看板。**

## 相关

- 双配置怎么来的：wiki `skills/claude-code-multi-profile-cco-ccs`
- 隔离原理：wiki `concepts/claude-config-dir-isolation`
