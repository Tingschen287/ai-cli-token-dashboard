# ai-cli-token-dashboard

本地 AI 编码 CLI 的 token 消耗看板。**零埋点**——数据全部来自这些工具自己写的
会话记录，不用改代理、不用装 hook、不用注册任何服务。

一屏看完：日历热力图 + 按模型 / 按项目的用量排行，随时间窗口联动。

目前覆盖三个来源，都可以在 `collect.py` 顶部的 `PROFILES` 里增删：

| key | 看板显示名 | 默认目录 | 说明 |
|---|---|---|---|
| `cco` | Claude Official | `~/.claude-official` | Claude Code 官方账号 |
| `ccs` | CC-Switch | `~/.claude` | Claude Code 经 CC-Switch 走第三方 |
| `grok` | Grok | `~/.grok` | Grok CLI |

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

目录不存在的来源会自动跳过，不会画出一整行空白日历。

要增删来源，改 `collect.py` 顶部的 `PROFILES`，`format` 决定用哪个 reader。

## 两种记录格式

**claude** —— `<dir>/projects/<项目>/<会话>.jsonl`
每行一条 assistant 消息，用量在 `message.usage`。

**grok** —— `<dir>/sessions/<urlencode(cwd)>/<会话>/updates.jsonl`
`turn_completed` 事件的 `params.update.usage`，一条 = 一次提问的整个 agent loop，
还带 `modelUsage` 分模型拆分、`reasoningTokens` 和 `costUsdTicks`。

## 布局：一屏到底，不滚动

主从分栏。左侧热力图占 62%，右侧是卡片和两张排行榜：

```
┌────────────────────────────────────────────────────┐
│ Token 活动  增量 2.17亿 …      数据 12 秒前 [↻] [ⓘ] │
├─────────────────────────────────┬──────────────────┤
│ [每日|每周|累计]     [3月6月1年] │ ┌──────┬───────┐ │
│  5月    6月    7月    8月        │ │ cco  │ ccs   │ │
│ ● cco   ░░░░░░░░░░▓█▓░░░░░      │ ├──────┼───────┤ │
│ ● ccs   ░░░▓███▓░░░░░░░░░░      │ │ grok │ 合计  │ │
│ ● grok  ░▓██▓░░░░░░░░░░░░░      │ └──────┴───────┘ │
│ ●●●  少 ░▒▓█ 多                 │ 按模型           │
│                                 │ 按项目           │
└─────────────────────────────────┴──────────────────┘
```

**三行日历共享一条月份轴。** 它们时间轴本来就相同，合并后三个来源在同一天的格子
严格垂直对齐——这样才能纵向比对「同一天我在哪个工具上花得多」。省空间只是副产品。

**自适应任意窗口尺寸。** 格子尺寸同时受宽高约束（横向要铺满列数，纵向三行必须塞进
可用高度），取较小值；排行榜按可用高度算能放几行，不留半行。实测：

| 窗口 | 格子 | 项目行数 |
|---|---|---|
| 2560×1440 | 25px | 19 |
| 1920×1080 | 17px | 12 |
| 1440×900 | 12px | 9 |
| 1280×720 | 10px | 6 |

窄于 940px 时自动退回单栏并允许滚动（一屏放不下就别硬塞）。

## 视图

| 视图 | 左侧形态 | 右侧统计窗口 |
|---|---|---|
| 每日 | 日历格子，一格 = 一天 | 只统计最后一天 |
| 每周 | 同一套格子，一列 = 一周，整列同色、hover 整列一起亮 | 只统计最后一周 |
| 累计 | 折线（累计值单调递增，画成热力图会整片最深色） | 当前范围全部 |

**右侧四张卡片和两张排行榜都跟随左侧的时间窗口**，卡片「合计」的副标题和面板标题
都会显示当前统计的是哪一段（`2026-08-12` / `2026-08-09 起那周` / `近 1 年`）。
范围按钮（3 个月 / 6 个月 / 1 年）同时限定数据池。所以「今天我在哪个项目烧得多」
和「这一年总账」是同一个控件切出来的。

只有顶栏那行是**全期**总账，固定不动，作为切 tab 时的对照锚点。

**按项目以项目为组**，一个项目一行，条形按模型分段堆叠——段色与按模型排行同源，
一眼看出这个项目烧在哪些模型上。悬停任意一段显示该模型的具体量和占比
（`ai · kimi-k2 10.6M · 占 61%`）。排行区放不下就滚动，不再按高度裁行数。

口径说明收在顶栏的 ⓘ 里，点击展开、Esc 关闭。

配色：`cco` 用 Claude 品牌橙，`ccs` 虽然也是 Claude Code 但跑第三方模型故用紫区分，
`grok` 用中性灰。

## 口径

**主指标 = 增量 token = 非缓存输入 + 输出 + 缓存写入**

为什么不用总量：`cache_read` 占九成以上（ccs 侧 8.29 亿 / 9.9 亿，grok 侧 92.3%）。
按总量着色的话，热力图反映的只是会话有多长、缓存命中多少，而不是实际干了多少活。
所以 `cache_read` 单列显示，不参与着色。

## 四个必须注意的处理

1. **跨工具口径对齐** —— Grok 的 `inputTokens` **包含** `cachedReadTokens`，
   Claude 的 `input_tokens` **不包含**。不减掉的话 grok 的增量会虚高一个数量级。
2. **去重方式不同** —— Claude 按 `message.id`（流式中间态重复严重，实测官方侧
   1516 行里 1024 行是重复的，不去重虚高约 2.5 倍）；Grok 按
   `session+prompt_id+模型`，实测 0 重复，数据天然干净。
3. **两种时间戳** —— Claude 是 UTC ISO 串（尾部 Z），Grok 是 unix 秒，都要转本机
   时区。不转的话本地晚上的会话会被算到第二天，热力图整体错位一格。
4. **零计量记录跳过** —— 部分记录 usage 全为 0（流式占位），计入会污染活动天数。

## 关于费用

只有 Grok 记了 `costUsdTicks`。看板按 **1e-9 USD/tick 推定**折算，标为"名义"值——
该单位未经官方文档确认，只是量级自洽（平均约 $4.6/M token，落在 frontier 模型
合理区间）。而且 `~/.grok/models_cache.json` 显示 `auth_method: session`，是订阅
登录，这个金额不等于实际扣费。

Claude 侧完全无法算钱：官方账号是订阅制；第三方经 CC-Switch 代理，真实计费在代理
侧，本地 jsonl 只有 token 数没有单价，且 MiniMax / GLM / k3 价格各不相同。

**这是消耗看板，不是账单看板。**

## 相关

- 双配置怎么来的：wiki `skills/claude-code-multi-profile-cco-ccs`
- 隔离原理：wiki `concepts/claude-config-dir-isolation`
