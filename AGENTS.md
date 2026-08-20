# AGENTS.md — ai-cli-token-dashboard

面向 AI 编码代理的项目说明。项目全部文档与代码注释使用**简体中文**，修改时请保持一致。

## 项目概览

本地 AI 编码 CLI 的 token 消耗看板。**零埋点**：数据全部来自各 CLI 工具自己写的
会话记录（jsonl），不改代理、不装 hook。一屏展示日历热力图 + 按模型 / 按项目
用量排行，随时间窗口联动。

**技术栈**：零第三方依赖。

- 后端：Python 3.8+，只用标准库（`http.server` / `sqlite3` / `urllib` / `json`）
- 前端：单文件 `template.html`，原生 HTML/CSS/JS，无构建步骤、无 npm

## 目录结构

| 文件 | 角色 |
|---|---|
| `collect.py` | 唯一的 Python 文件（约 700 行）：扫描、聚合、渲染、HTTP 服务全部在此 |
| `template.html` | 前端模板（约 1100 行），CSS + JS 全部内联，`/*__DATA__*/null` 是数据占位符 |
| `dashboard.html` | **生成物**，由 `collect.py` 渲染产出，内嵌真实项目名，已在 `.gitignore` 排除，**绝不提交** |
| `cc-token-dashboard.service.example` | systemd 用户服务模板，两处 `%h/path/to/...` 需改成实际路径 |
| `README.md` | 面向用户的完整文档（口径、布局、隐私、部署），改行为时同步更新 |

## collect.py 内部结构

- `PROFILES`（文件顶部）：扫描来源配置。key 是看板标识，`format` 决定解析器。增删来源改这里。
- 解析器：`parse_claude_file`（`<dir>/projects/**/*.jsonl`，含 subagents 子目录）和
  `parse_grok_file`（`<dir>/sessions/*/*/updates.jsonl`），两者都产出统一的 `Row`
  namedtuple，下游聚合只认 `Row`。
- 文件级增量缓存 `_CACHE`：按 `(mtime_ns, size)` 签名判断，只重读变过的文件。
  会话记录 append-only，这是安全的。
- `collect()`：聚合产出 `daily` / `models` / `projects`（都带 date 维度，供前端
  按时间窗口重新聚合）和 `profiles` 总量。
- 额度轮询（仅 `--serve` 模式联网）：`QuotaPoller` 后台线程每 180 秒轮询
  cco（Anthropic OAuth `/api/oauth/usage`）、ccs（cc-switch 库里的 Kimi/MiniMax
  供应商，读 `~/.cc-switch/cc-switch.db` 拿 token）、grok（CLI 内部 billing 接口，
  OIDC token 从 `~/.grok/auth.json` 复用/refresh）。单家失败沿用旧数据。
- `Snapshot`：后台线程按 `--interval`（默认 60s）定时重扫，`/api/data?force=1`
  立即重扫（页面刷新按钮）。
- `serve()`：`http.server.ThreadingHTTPServer`，路由只有 `/api/data`、`/`、
  `/healthz`，其余 404。

## template.html 前端约定

- `let DATA = /*__DATA__*/null;` —— `render()`/`serve()` 把聚合 JSON 替换进这个
  占位符；转义 `</` 防止提前闭合 script 块。
- 刷新会整体换掉 `DATA`，所以派生结构在 `applyData()` 里重算，**不能做成顶层 const**。
- 布局是一屏到底不滚动：格子尺寸同时受宽高约束取较小值；窄于 940px 退回单栏滚动。
- 品牌色：`COLORS`（cco 橙 / ccs 紫 / grok 灰）、`MODEL_COLORS`、`LOGOS` 都在
  script 顶部集中定义。
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

改动聚合逻辑前必读 README「口径」和「四个必须注意的处理」，核心点：

1. **主指标 = 增量 token** = 非缓存输入 + 输出 + 缓存写入。`cache_read` 单列，
   不参与着色（它占总量九成以上）。
2. **跨工具口径对齐**：Grok 的 `inputTokens` **包含** `cachedReadTokens`，Claude 的
   `input_tokens` 不包含——解析 grok 时必须减掉，否则虚高一个数量级。
3. **去重方式不同**：Claude 按 `message.id`（流式中间态重复严重，且重复可跨文件，
   所以去重放在 `collect()` 上层而非解析器内）；Grok 按 `session+prompt_id+模型`。
4. **时间戳统一转本机时区**（`_local_date`）：Claude 是 UTC ISO 串，Grok 是 unix 秒。
   不转的话本地晚上的会话被算到第二天，热力图错位。
5. usage 全为 0 的记录跳过（流式占位）。
6. claude 的 glob 是 `projects/**/*.jsonl`（两层），**不能改成一层**——subagent
   会话在 `subagents/agent-*.jsonl`，漏掉会整体丢失这部分用量。
7. 费用：只有 grok 记 `costUsdTicks`，按 1e-9 USD/tick 推定，是**名义**值。
   这是消耗看板，不是账单看板。

## 安全与隐私

- **服务默认只绑 `127.0.0.1`，不要改成 `0.0.0.0`**。聚合结果含真实项目名和目录
  路径；WSL `networkingMode=mirrored` 下绑 0.0.0.0 等于暴露给整个局域网。
- `dashboard.html` 内嵌真实项目名与用量，已在 `.gitignore` 排除。分享前必须
  确认没有不该外传的内容。`.gitignore` 里 `*.json` 也是同理（`--json` 导出物）。
- 静态扫描完全离线；唯一联网行为是 `--serve` 模式的额度轮询（读各平台 token：
  `~/.claude-official/.credentials.json`、`~/.cc-switch/cc-switch.db`、
  `~/.grok/auth.json`）。grok 的 OIDC refresh 会写回 `auth.json`（先重读合并、
  chmod 600），修改这段逻辑时注意不要顶掉 grok CLI 自己的登录态。
- grok 额度走未公开的 CLI 内部 billing 接口，字段可能随时变化，解析必须容错。

## 部署

systemd 用户服务（见 `cc-token-dashboard.service.example` 和 README「开机自启」）：
`Restart=always` + `RestartSec=5` 崩溃自动拉起，`Nice=10` 不抢前台 CPU。WSL 下
需要 `sudo loginctl enable-linger $USER` 才能无登录会话自启。改完代码用
`systemctl --user restart cc-token-dashboard` 生效。
