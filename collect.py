#!/usr/bin/env python3
"""扫描本机各 AI 编码 CLI 的会话记录，聚合 token 消耗并生成看板。

静态生成只读本地文件、不联网、不调任何 API——刷新多少次都不消耗 token。
服务模式（--serve）额外开一个额度轮询线程，查询各平台套餐额度并合并进
/api/data（见 QuotaPoller 一节）；这是唯一的联网行为，失败自动沿用旧数据。

支持两种记录格式：
  claude  <config_dir>/projects/<项目>/<会话>.jsonl 及其 subagents/ 子目录
          每行 assistant 消息带 message.usage
  grok    <config_dir>/sessions/<urlencode(cwd)>/<会话>/updates.jsonl
          turn_completed 事件带 params.update.usage

用法：
    python3 collect.py                 # 生成 dashboard.html
    python3 collect.py --serve         # 起本地服务，后台定时刷新
    python3 collect.py --json out.json # 只导出聚合数据
"""

import argparse
import collections
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 要扫描的配置目录。key 是看板标识，dir 支持 ~ 展开，format 决定用哪个解析器。
PROFILES = [
    {"key": "cco",   "label": "Claude Official", "dir": "~/.claude-official", "format": "claude"},
    {"key": "kimi",  "label": "Kimi",            "dir": "~/.kimi-code",       "format": "kimi"},
    {"key": "codex", "label": "ChatGPT",         "dir": "~/.codex",           "format": "codex"},
    {"key": "ccs",   "label": "CC-Switch",       "dir": "~/.claude",          "format": "claude"},
    {"key": "grok",  "label": "Grok",            "dir": "~/.grok",            "format": "grok"},
    {"key": "oc",    "label": "OpenCode",        "dir": "~/.local/share/opencode", "format": "opencode"},
]

HERE = Path(__file__).resolve().parent

# 一条标准化记录：两种解析器都产出这个形状，下游只认它
Row = collections.namedtuple(
    "Row", "dedup_key date model project input output cache_write cache_read reasoning calls cost_ticks")


def _local_date(value):
    """时间戳 → 本机时区日期。

    Claude 用 UTC ISO 串（尾部 Z），grok 用 unix 秒。两者都必须落到本地时区，
    否则本地晚上的会话会被算到第二天，热力图整体错位一格。
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).date().isoformat()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date().isoformat()


def parse_claude_file(path):
    """Claude Code：每行一条 assistant 消息。

    同一个 message.id 会重复出现（流式中间态多次落盘），去重放在上层做，
    因为重复也可能跨文件。input_tokens 本身不含缓存读，与 grok 口径相反。
    """
    rows, raw = [], 0
    try:
        handle = path.open(errors="ignore")
    except OSError:
        return rows, raw
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            raw += 1
            date = _local_date(rec.get("timestamp"))
            if not date:
                continue
            cwd = rec.get("cwd") or ""
            rows.append(Row(
                dedup_key=msg.get("id"),
                date=date,
                model=msg.get("model") or "unknown",
                project=(Path(cwd).name if cwd else path.parent.name) or "unknown",
                input=usage.get("input_tokens") or 0,
                output=usage.get("output_tokens") or 0,
                cache_write=usage.get("cache_creation_input_tokens") or 0,
                cache_read=usage.get("cache_read_input_tokens") or 0,
                reasoning=0,
                calls=1,
                cost_ticks=0,
            ))
    return rows, raw


def parse_grok_file(path):
    """Grok CLI：turn_completed 事件带整轮合计。

    一条记录 = 一次提问的整个 agent loop，天然是增量（prompt_id 各不相同、
    totalTokens 非单调），不像 Claude 那样需要剔除流式重复。

    注意 inputTokens **包含** cachedReadTokens，与 Claude 相反——不减掉的话
    缓存读会被重复计入增量，数字虚高一个数量级。
    """
    rows, raw = [], 0
    # 目录名是 urlencode 后的 cwd，解码后取末段作为项目名
    project = Path(urllib.parse.unquote(path.parts[-3])).name or "unknown"
    try:
        handle = path.open(errors="ignore")
    except OSError:
        return rows, raw
    with handle:
        for line in handle:
            if '"turn_completed"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            params = rec.get("params") or {}
            update = params.get("update") or {}
            if update.get("sessionUpdate") != "turn_completed":
                continue
            usage = update.get("usage")
            if not isinstance(usage, dict):
                continue
            raw += 1
            date = _local_date(rec.get("timestamp"))
            if not date:
                continue
            # 一轮可能跨多个模型，按模型拆开才能正确归因
            for name, mu in (usage.get("modelUsage") or {"unknown": usage}).items():
                cached = mu.get("cachedReadTokens") or 0
                rows.append(Row(
                    dedup_key=f"{params.get('sessionId')}|{update.get('prompt_id')}|{name}",
                    date=date,
                    model=name,
                    project=project,
                    input=max(0, (mu.get("inputTokens") or 0) - cached),
                    output=mu.get("outputTokens") or 0,
                    cache_write=mu.get("cacheCreationTokens") or 0,
                    cache_read=cached,
                    reasoning=mu.get("reasoningTokens") or 0,
                    calls=mu.get("modelCalls") or 0,
                    cost_ticks=mu.get("costUsdTicks") or 0,
                ))
    return rows, raw


def parse_kimi_file(path):
    """Kimi Code CLI：wire.jsonl 里的 usage.record 事件。

    每个 turn 一条 usage.record，是这次 LLM 调用的真实计量；紧随其后 step.end
    事件里带的是同一份 usage，只取 usage.record 避免重复。inputOther 不含缓存读
    （与 claude 口径一致：inputOther + inputCacheRead + output == 实测总 token，
    已验证），所以增量 = inputOther + output + inputCacheCreation，cache_read 单列。

    usage.record 没有 message.id，但每个 turn 一条、跨文件不重叠，不需要 dedup_key。
    time 是 unix **毫秒**，要 /1000 才落得到正确日期。
    """
    rows, raw = [], 0
    # 路径 sessions/<wd_>/session_<id>/agents/<agent>/wire.jsonl；workdir 名编在
    # wd_<basename>_<hash16> 里，去掉尾部 hash 段即项目名
    wd = next((p for p in path.parts if p.startswith("wd_")), "")
    project = (wd[3:].rsplit("_", 1)[0] if wd else "") or "unknown"
    try:
        handle = path.open(errors="ignore")
    except OSError:
        return rows, raw
    with handle:
        for line in handle:
            if '"usage.record"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "usage.record":
                continue
            usage = rec.get("usage")
            if not isinstance(usage, dict):
                continue
            raw += 1
            t = rec.get("time")
            date = _local_date(t / 1000) if isinstance(t, (int, float)) else None
            if not date:
                continue
            rows.append(Row(
                dedup_key=None,
                date=date,
                model=rec.get("model") or "unknown",
                project=project,
                input=usage.get("inputOther") or 0,
                output=usage.get("output") or 0,
                cache_write=usage.get("inputCacheCreation") or 0,
                cache_read=usage.get("inputCacheRead") or 0,
                reasoning=0,
                calls=1,
                cost_ticks=0,
            ))
    return rows, raw


def parse_codex_file(path):
    """Codex CLI：rollout-*.jsonl 里的 token_count 事件。

    取 info.last_token_usage（这次 LLM 调用的增量）；total_token_usage 是
    会话累计值，不取。注意 input_tokens **包含** cached_input_tokens
    （同 grok，要减掉，否则缓存读重复计入增量）；codex 不区分缓存写入，
    cache_write 记 0。timestamp 是 UTC ISO 串（同 claude，要转本机时区）。

    模型和 cwd 不在 token_count 里，在 session_meta / turn_context 行，
    逐行跟踪当前值。每个 token_count 事件一次调用、天然不重复，无需 dedup_key。
    """
    rows, raw = [], 0
    try:
        handle = path.open(errors="ignore")
    except OSError:
        return rows, raw
    model = project = None
    with handle:
        for line in handle:
            if '"session_meta"' in line or '"turn_context"' in line:
                try:
                    ctx = json.loads(line).get("payload") or {}
                except json.JSONDecodeError:
                    continue
                model = ctx.get("model") or model
                cwd = ctx.get("cwd")
                if cwd:
                    project = Path(cwd).name
                continue
            if '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            usage = (payload.get("info") or {}).get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            raw += 1
            date = _local_date(rec.get("timestamp"))
            if not date:
                continue
            cached = usage.get("cached_input_tokens") or 0
            rows.append(Row(
                dedup_key=None,
                date=date,
                model=model or "unknown",
                project=project or "unknown",
                input=max(0, (usage.get("input_tokens") or 0) - cached),
                output=usage.get("output_tokens") or 0,
                cache_write=0,
                cache_read=cached,
                reasoning=usage.get("reasoning_output_tokens") or 0,
                calls=1,
                cost_ticks=0,
            ))
    return rows, raw


def parse_opencode_file(path):
    """OpenCode：会话存储是单个 SQLite 库（opencode.db），message.data 是 JSON。

    一条 assistant message = 一次完整响应，message.id 唯一、天然不重复，无需
    dedup_key（整个库每次全量重读，也没有跨文件重复问题）。实测 86/86 条满足
    total = input + output + reasoning + cache.read + cache.write，即
    input **不含** cache read（与 claude 同口径，不用减）。
    time.created 是 unix **毫秒**（同 kimi，/1000）。项目取 session.directory 末段。
    cost 字段是 USD（按 provider 报价算的实估值），折成 cost_ticks 复用 ≈$ 显示。
    """
    import sqlite3

    rows, raw = [], 0
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return rows, raw
    try:
        query = (
            "SELECT m.data, s.directory FROM message m"
            " LEFT JOIN session s ON s.id = m.session_id"
            " WHERE json_extract(m.data, '$.role') = 'assistant'"
        )
        for data, directory in db.execute(query):
            try:
                d = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = d.get("tokens")
            if not isinstance(usage, dict):
                continue
            raw += 1
            t = (d.get("time") or {}).get("created")
            date = _local_date(t / 1000) if isinstance(t, (int, float)) else None
            if not date:
                continue
            cache = usage.get("cache") or {}
            rows.append(Row(
                dedup_key=None,
                date=date,
                model=d.get("modelID") or "unknown",
                project=(Path(directory).name if directory else "") or "unknown",
                input=usage.get("input") or 0,
                output=usage.get("output") or 0,
                cache_write=cache.get("write") or 0,
                cache_read=cache.get("read") or 0,
                reasoning=usage.get("reasoning") or 0,
                calls=1,
                cost_ticks=round((d.get("cost") or 0) * 1e9),
            ))
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return rows, raw


FORMATS = {
    # 主会话在 projects/<项目>/<会话>.jsonl（2 层），subagent 在
    # projects/<项目>/<会话>/subagents/agent-*.jsonl（4 层）。用 ** 递归把两层都
    # 收进来，否则 subagent 的用量会整体漏掉。subagent 的 message.id 与主会话
    # 不重叠（已验证），靠既有跨文件去重即可，不会重复计数。
    "claude": (parse_claude_file, "projects/**/*.jsonl"),
    "grok":   (parse_grok_file,   "sessions/*/*/updates.jsonl"),
    "kimi":   (parse_kimi_file,   "sessions/*/session_*/agents/*/wire.jsonl"),
    # codex：sessions/YYYY/MM/DD/rollout-*.jsonl（4 层）
    "codex":  (parse_codex_file,  "sessions/*/*/*/rollout-*.jsonl"),
    # opencode：整个存储就是一个 SQLite 库，glob 直接命中库文件本身
    "opencode": (parse_opencode_file, "opencode.db"),
}

# 文件级缓存：path -> (签名, rows, raw_count)
# 会话记录是 append-only 的历史，改过的文件才需要重读。常态下每分钟只有
# 正在写入的那一两个文件变化，其余全部命中缓存。
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _sig_of(path):
    st = path.stat()
    # SQLite WAL 模式下新数据先进 -wal 文件，主库 mtime 可能滞后到 checkpoint；
    # 签名不把 wal 算上的话，正在进行的 opencode 会话会漏刷新
    if path.suffix == ".db":
        try:
            wst = path.with_name(path.name + "-wal").stat()
            return (max(st.st_mtime_ns, wst.st_mtime_ns), st.st_size + wst.st_size)
        except OSError:
            pass
    return (st.st_mtime_ns, st.st_size)


def _rows_of(path, parser):
    sig = _sig_of(path)
    with _CACHE_LOCK:
        hit = _CACHE.get(path)
        if hit and hit[0] == sig:
            return hit[1], hit[2], True
    rows, raw = parser(path)
    with _CACHE_LOCK:
        _CACHE[path] = (sig, rows, raw)
    return rows, raw, False


def codex_quota(root):
    """Codex 额度：rollout 文件的 token_count 事件自带 rate_limits，纯本地、不联网。

    额度是账号当前状态，只需读 mtime 最新的那个文件的尾部。产出对齐成
    QuotaPoller 相同的形状（{"windows": [...]}），前端 quotaInline 直接复用。
    """
    try:
        newest = max(root.glob(FORMATS["codex"][1]),
                     key=lambda p: p.stat().st_mtime)
    except (ValueError, OSError):
        return None
    try:
        with newest.open(errors="ignore") as f:
            lines = collections.deque(f, maxlen=100)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            rl = (json.loads(line).get("payload") or {}).get("rate_limits")
        except json.JSONDecodeError:
            continue
        if not isinstance(rl, dict):
            continue
        windows = []
        for src, key in ((rl.get("primary"), "week"), (rl.get("secondary"), "5h")):
            if not isinstance(src, dict) or src.get("used_percent") is None:
                continue
            reset = src.get("resets_at")
            windows.append({
                # 窗口长短不定（周窗 10080 分钟），>1 天按周窗样式展示
                "key": key if (src.get("window_minutes") or 0) > 1440 else "5h",
                "pct": round(src.get("used_percent") or 0),
                "reset": (datetime.fromtimestamp(reset).astimezone()
                          .isoformat(timespec="seconds") if reset else ""),
            })
        if windows:
            return {"windows": windows, "plan": rl.get("plan_type") or ""}
    return None


def blank():
    return {"incr": 0, "cache_read": 0, "output": 0, "cache_write": 0,
            "input": 0, "reasoning": 0, "msgs": 0, "cost_ticks": 0}


def accumulate(bucket, row):
    bucket["input"] += row.input
    bucket["output"] += row.output
    bucket["cache_write"] += row.cache_write
    bucket["cache_read"] += row.cache_read
    bucket["reasoning"] += row.reasoning
    bucket["cost_ticks"] += row.cost_ticks
    # 增量口径：真正新产生的 token（非缓存输入 + 输出 + 缓存写入）。
    # cache_read 单独看——它占总量九成以上，混进主指标的话热力图只反映
    # 会话长度和缓存命中，不反映实际干了多少活。
    bucket["incr"] += row.input + row.output + row.cache_write
    bucket["msgs"] += max(1, row.calls)


def collect():
    started = time.time()
    daily = collections.defaultdict(blank)       # (date, profile)
    # 带 date 维度：右侧排行要按当前时间窗口（当天/当周/整个范围）重新聚合，
    # 没有日期就只能给全时段总量。组合数有限（几百条），代价几十 KB。
    models = collections.defaultdict(blank)      # (date, profile, model)
    # 按项目面板的分段是模型（不是来源），所以 projects 也带 model 维度；
    # profile 仍保留，前端取模型品牌色（modelColor）要用
    projects = collections.defaultdict(blank)    # (date, profile, project, model)
    totals = collections.defaultdict(blank)      # profile
    meta = {}
    scanned = cached = 0

    for profile in PROFILES:
        key = profile["key"]
        root = Path(os.path.expanduser(profile["dir"]))
        parser, pattern = FORMATS[profile["format"]]
        seen = set()
        raw_rows = duplicates = 0

        if root.is_dir():
            for path in sorted(root.glob(pattern)):
                try:
                    rows, raw, was_cached = _rows_of(path, parser)
                except OSError:
                    continue
                scanned += 0 if was_cached else 1
                cached += 1 if was_cached else 0
                raw_rows += raw
                for row in rows:
                    # 去重必须跨文件：同一 message.id 会在续写的会话里再次出现
                    if row.dedup_key:
                        if row.dedup_key in seen:
                            duplicates += 1
                            continue
                        seen.add(row.dedup_key)
                    if row.input + row.output + row.cache_write + row.cache_read == 0:
                        continue  # 流式占位，没有实际计量
                    accumulate(daily[(row.date, key)], row)
                    accumulate(models[(row.date, key, row.model)], row)
                    accumulate(projects[(row.date, key, row.project, row.model)], row)
                    accumulate(totals[key], row)

        meta[key] = {
            "label": profile["label"],
            "dir": profile["dir"],
            "present": root.is_dir(),
            "raw_rows": raw_rows,
            "deduped": duplicates,
        }
        # codex 的额度在会话文件里白送（rate_limits），扫描时顺手取，不联网
        if key == "codex" and root.is_dir():
            q = codex_quota(root)
            if q:
                meta[key]["quota"] = q

    def flatten(source, fields):
        out = []
        for composite_key, bucket in source.items():
            row = dict(zip(fields, composite_key))
            row.update(bucket)
            out.append(row)
        return out

    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scan": {"seconds": round(time.time() - started, 2),
                 "files_read": scanned, "files_cached": cached},
        "profiles": [
            {**profile, **meta[profile["key"]], **totals[profile["key"]]}
            for profile in PROFILES
        ],
        "daily": sorted(flatten(daily, ("date", "profile")),
                        key=lambda r: (r["date"], r["profile"])),
        # 前端按时间窗口过滤后自行聚合排序，这里只要顺序稳定
        "models": sorted(flatten(models, ("date", "profile", "model")),
                         key=lambda r: (r["date"], r["profile"], r["model"])),
        "projects": sorted(flatten(projects, ("date", "profile", "project", "model")),
                           key=lambda r: (r["date"], r["profile"], r["project"], r["model"])),
    }


# 前端按职责拆成多个静态文件，输出时再内联拼装——零构建、无 module 系统。
# serve 模式每次请求现读现拼（改完刷新即生效）；render 产物仍是内联一切的单文件。
CSS_FILE = "style.css"
JS_FILES = ["brand.js", "data.js", "layout.js", "calendar.js", "charts.js", "app.js"]


def build_html():
    """读模板与拆分的静态资源，拼出未注数据的页面串（保留 /*__DATA__*/ 占位符）。"""
    html = (HERE / "template.html").read_text(encoding="utf-8")
    for marker in ("/*__STYLE__*/", "/*__APP__*/", "/*__DATA__*/null"):
        if marker not in html:
            sys.exit(f"模板缺少占位符 {marker}")
    css_path = HERE / CSS_FILE
    if not css_path.exists():
        sys.exit(f"缺少前端文件: {css_path}")
    parts = []
    for name in JS_FILES:
        f = HERE / name
        if not f.exists():
            sys.exit(f"缺少前端文件: {f}")
        # 文件间加分隔注释兼作语句屏障，防行尾注释/缺少分号粘连
        parts.append(f"/* ---- {name} ---- */\n" + f.read_text(encoding="utf-8"))
    html = html.replace("/*__STYLE__*/", css_path.read_text(encoding="utf-8"))
    html = html.replace("/*__APP__*/", "\n".join(parts))
    return html


def render(payload, target):
    html = build_html()
    marker = "/*__DATA__*/null"
    # </script> 会提前闭合内联脚本块，必须转义
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    target.write_text(html.replace(marker, blob), encoding="utf-8")


# ── 额度轮询（仅 --serve 模式联网）────────────────────────────────────
# 各平台套餐额度是账号实时状态，与会话记录无关、与时间窗口无关，单独慢轮询。
# grok 走未公开的 CLI billing 接口（与 /usage 同源），只有周额度一个窗口。
QUOTA_INTERVAL = 180   # 3 分钟；额度变化慢，频繁查询无意义还容易被限流
CCS_DB = Path.home() / ".cc-switch" / "cc-switch.db"
CCO_CREDENTIALS = Path.home() / ".claude-official" / ".credentials.json"


def _http_get_json(url, headers, timeout=8):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def quota_cco():
    """Claude 官方 OAuth usage：5 小时窗 + 每周窗的已用百分比。"""
    try:
        creds = json.loads(CCO_CREDENTIALS.read_text(encoding="utf-8"))
        token = (creds.get("claudeAiOauth") or {}).get("accessToken") or ""
    except Exception:
        return None
    if not token:
        return None
    d = _http_get_json("https://api.anthropic.com/api/oauth/usage", {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.150",
    })
    windows = []
    five = d.get("five_hour") or {}
    if five.get("utilization") is not None:
        windows.append({"key": "5h", "pct": round(five.get("utilization") or 0),
                        "reset": five.get("resets_at") or ""})
    seven = d.get("seven_day") or {}
    if seven.get("utilization") is not None:
        windows.append({"key": "week", "pct": round(seven.get("utilization") or 0),
                        "reset": seven.get("resets_at") or ""})
    return {"windows": windows} if windows else None


def _ccs_quota_providers():
    """cc-switch 库里有额度接口的 claude 供应商。

    Kimi 的额度条已挪到 kimi code 行（同一账号、同一个 /v1/usages 接口，
    只是认证换成 CLI 的 OAuth token），这里只剩 MiniMax；腾云智算这类没有
    额度接口的供应商不出现在列表里。
    """
    if not CCS_DB.is_file():
        return []
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{CCS_DB}?mode=ro", uri=True)
        rows = db.execute(
            "SELECT name, settings_config FROM providers WHERE app_type='claude'"
        ).fetchall()
        db.close()
    except Exception:
        return []
    out = []
    for name, cfg in rows:
        try:
            env = (json.loads(cfg).get("env") or {})
        except Exception:
            continue
        base = env.get("ANTHROPIC_BASE_URL") or ""
        token = env.get("ANTHROPIC_AUTH_TOKEN") or ""
        if "minimax" in base.lower() and token:
            out.append({"name": name, "kind": "minimax", "base": base, "token": token})
    return out


def _quota_kimi(base, token):
    d = _http_get_json(base.rstrip("/") + "/v1/usages", {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "claude-cli/2.1.150",
    })
    windows = []
    # limits[] 里是滚动短窗（300 分钟），usage 是周配额
    for w in d.get("limits") or []:
        det = w.get("detail") or {}
        lim, used = int(det.get("limit") or 0), int(det.get("used") or 0)
        if lim:
            windows.append({"key": "5h", "pct": used * 100 // lim,
                            "reset": det.get("resetTime") or ""})
            break
    u = d.get("usage") or {}
    lim, used = int(u.get("limit") or 0), int(u.get("used") or 0)
    if lim:
        windows.append({"key": "week", "pct": used * 100 // lim,
                        "reset": u.get("resetTime") or ""})
    return windows


def _quota_minimax(token):
    # 接口只给剩余百分比（部分套餐给原始计数），统一换算成已用百分比
    d = _http_get_json("https://www.minimaxi.com/v1/token_plan/remains", {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/2.1.150",
    })
    models = d.get("model_remains") or []
    m = next((x for x in models if x.get("model_name") == "general"),
             models[0] if models else None)
    if not m:
        return []

    def used_pct(rem_key, tot_key, used_key):
        rem = m.get(rem_key)
        if rem is not None:
            return max(0, 100 - int(rem))
        tot, used = m.get(tot_key) or 0, m.get(used_key) or 0
        return round(used * 100 / tot) if tot else None

    windows = []
    p5 = used_pct("current_interval_remaining_percent",
                  "current_interval_total_count", "current_interval_usage_count")
    if p5 is not None:
        windows.append({"key": "5h", "pct": p5, "reset_ms": m.get("end_time") or 0})
    pw = used_pct("current_weekly_remaining_percent",
                  "current_weekly_total_count", "current_weekly_usage_count")
    if pw is not None:
        windows.append({"key": "week", "pct": pw,
                        "reset_ms": m.get("weekly_end_time") or 0})
    return windows


def quota_ccs():
    """Kimi 和 MiniMax 并行语义（顺序调用但都独立容错），单家失败不拖垮整组。"""
    out = []
    for p in _ccs_quota_providers():
        try:
            windows = (_quota_kimi(p["base"], p["token"]) if p["kind"] == "kimi"
                       else _quota_minimax(p["token"]))
            out.append({"name": p["name"], "windows": windows, "error": None})
        except Exception as exc:
            out.append({"name": p["name"], "windows": [], "error": str(exc)[:80]})
    return {"providers": out} if out else None


KIMI_CRED = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
KIMI_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
# CLI 二进制内置的 OAuth client_id（公共客户端，无 secret）
KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"


def _kimi_access_token():
    """读 kimi code CLI 的 OAuth 凭证；过期就 refresh。

    access_token 只有 15 分钟（expires_in=900），基本每次都靠 refresh。
    refresh_token 会轮换且 CLI 自己也在读写 credentials 文件：refresh 后
    必须重新读文件合并、原子写回、chmod 600，否则会把 CLI 的登录态顶掉。
    """
    try:
        cred = json.loads(KIMI_CRED.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = cred.get("access_token") or ""
    try:
        if token and float(cred.get("expires_at") or 0) > time.time() + 60:
            return token
    except (TypeError, ValueError):
        pass
    rt = cred.get("refresh_token") or ""
    if not rt:
        return None
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": rt,
        "client_id": KIMI_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        KIMI_TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        tok = json.loads(resp.read().decode("utf-8"))
    # 重新读再合并：refresh 期间 CLI 可能自己也写过凭证文件
    try:
        fresh = json.loads(KIMI_CRED.read_text(encoding="utf-8"))
        fresh["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            fresh["refresh_token"] = tok["refresh_token"]
        expires_in = int(tok.get("expires_in") or 900)
        fresh["expires_in"] = expires_in
        fresh["expires_at"] = int(time.time()) + expires_in - 30
        tmp = KIMI_CRED.with_name(KIMI_CRED.name + ".tmp")
        tmp.write_text(json.dumps(fresh, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(KIMI_CRED)
    except OSError:
        pass  # 写回失败最多下次再 refresh，不影响本次查询
    return tok["access_token"]


def quota_kimi_code():
    """Kimi Code 官方账号额度：和 cc-switch 的 Kimi 供应商是同一个
    /v1/usages 接口（同一账号），只是认证换成 CLI 的 OAuth access_token。"""
    token = _kimi_access_token()
    if not token:
        return None
    windows = _quota_kimi("https://api.kimi.com/coding", token)
    return {"windows": windows} if windows else None


GROK_AUTH = Path.home() / ".grok" / "auth.json"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"


def _grok_entry():
    """读 grok CLI 的 OIDC 登录态（auth.json 是单键字典，值里才有各字段）"""
    try:
        auth = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
        key = next(iter(auth))
        entry = auth[key]
        if isinstance(entry, dict) and entry.get("auth_mode") == "oidc":
            return auth, key, entry
    except Exception:
        pass
    return None, None, None


def _grok_access_token():
    """优先复用未过期的 access token，过期才 refresh。

    refresh_token 会轮换且 grok CLI 自己也在读写 auth.json，所以只有不得不
    refresh 时才写回，写回前重新读文件合并，尽量不把 CLI 的登录态顶掉。
    """
    auth, key, entry = _grok_entry()
    if not entry:
        return None
    token = entry.get("key") or ""
    exp = entry.get("expires_at")
    if token and exp:
        try:
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < exp_dt - timedelta(minutes=5):
                return token
        except ValueError:
            pass
    refresh_token = entry.get("refresh_token") or ""
    if not refresh_token:
        return None
    disc = _http_get_json(
        f"{entry['oidc_issuer']}/.well-known/openid-configuration",
        {"Accept": "application/json"}, timeout=15)
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": entry["oidc_client_id"],
    }).encode()
    req = urllib.request.Request(
        disc["token_endpoint"], data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read().decode("utf-8"))
    # 重新读再合并：refresh 期间 CLI 可能自己也写过 auth.json
    try:
        fresh = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
        merged = fresh.get(key) if isinstance(fresh.get(key), dict) else entry
        merged["key"] = tok["access_token"]
        if tok.get("refresh_token"):
            merged["refresh_token"] = tok["refresh_token"]
        expires_in = int(tok.get("expires_in") or 21600)
        merged["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat().replace("+00:00", "Z")
        fresh[key] = merged
        GROK_AUTH.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
        GROK_AUTH.chmod(0o600)
    except OSError:
        pass  # 写回失败最多下次再 refresh，不影响本次查询
    return tok["access_token"]


def quota_grok():
    """Grok Build 周额度（CLI 内部 billing 接口，与 /usage 同源；未公开，字段可能变）。"""
    token = _grok_access_token()
    if not token:
        return None
    d = _http_get_json(GROK_BILLING_URL, {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "grok-usage-script/1.0",
        "x-grok-client-mode": "cli",
        "x-grok-client-identifier": "grok-shell",
        "x-grok-client-version": "1.0.3",
    }, timeout=15)
    cfg = d.get("config") or {}
    pct = cfg.get("creditUsagePercent")
    if pct is None:
        return None
    period = cfg.get("currentPeriod") or {}
    return {"windows": [{"key": "week", "pct": round(float(pct)),
                         "reset": period.get("end") or ""}]}


class QuotaPoller:
    """慢轮询各家额度，单家失败沿用该家的旧数据，页面永远有东西显示。"""

    def __init__(self, interval=QUOTA_INTERVAL):
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = {}

    def get(self):
        with self.lock:
            return self.latest

    def poll_once(self):
        fresh = {}
        for key, fn in (("cco", quota_cco), ("kimi", quota_kimi_code),
                        ("ccs", quota_ccs), ("grok", quota_grok)):
            try:
                fresh[key] = fn()
            except Exception:
                fresh[key] = None
        with self.lock:
            old = self.latest
            merged = {}
            merged["cco"] = fresh["cco"] or old.get("cco")
            merged["kimi"] = fresh["kimi"] or old.get("kimi")
            merged["grok"] = fresh["grok"] or old.get("grok")
            if fresh["ccs"] is None:
                merged["ccs"] = old.get("ccs")
            else:
                # 按供应商粒度合并：哪家这轮挂了，用哪家的旧数据顶上并标记 stale
                old_provs = {p["name"]: p
                             for p in (old.get("ccs") or {}).get("providers", [])}
                provs = []
                for p in fresh["ccs"]["providers"]:
                    if p["error"] and not p["windows"] and p["name"] in old_provs:
                        provs.append({**old_provs[p["name"]], "stale": True})
                    else:
                        provs.append(p)
                merged["ccs"] = {"providers": provs}
            merged["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self.latest = merged

    def loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                print(f"[warn] 额度轮询失败: {exc}", file=sys.stderr)
            time.sleep(self.interval)


class Snapshot:
    """后台线程定时扫描的结果。页面请求直接读这里，不各自触发扫描。"""

    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.payload = collect()

    def get(self):
        with self.lock:
            return self.payload

    def refresh(self):
        payload = collect()          # 扫描在锁外做，不阻塞正在读的请求
        with self.lock:
            self.payload = payload
        return payload

    def loop(self):
        while True:
            time.sleep(self.interval)
            try:
                self.refresh()
            except Exception as exc:                     # 单次失败不该拖垮服务
                print(f"[warn] 刷新失败: {exc}", file=sys.stderr)


def serve(port, interval, host="127.0.0.1"):
    """起本地服务。

    默认只绑 127.0.0.1。本机 WSL 是 mirrored 网络模式，直接持有公司网段
    IP，绑 0.0.0.0 等于把项目名暴露给整个局域网；而 mirrored 模式下
    Windows 访问 localhost 就能直达这里，无需对外监听。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    snapshot = Snapshot(interval)
    threading.Thread(target=snapshot.loop, daemon=True).start()
    quota = QuotaPoller()
    threading.Thread(target=quota.loop, daemon=True).start()

    def with_quota(payload):
        # 浅拷贝后挂额度，不污染 Snapshot 里的共享对象
        return {**payload, "quota": quota.get()}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/api/data":
                # force=1 是刷新按钮：不等定时器，立刻重扫
                payload = snapshot.refresh() if "force=1" in query else snapshot.get()
                self._send(json.dumps(with_quota(payload), ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path in ("/", "/index.html", "/dashboard.html"):
                # 每次请求现读现拼，开发时改完静态文件刷新即生效
                blob = json.dumps(with_quota(snapshot.get()), ensure_ascii=False).replace("</", "<\\/")
                self._send(build_html().replace("/*__DATA__*/null", blob).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/healthz":
                self._send(b"ok", "text/plain")
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass  # 静音访问日志，每分钟轮询会刷屏

    server = ThreadingHTTPServer((host, port), Handler)
    first = snapshot.get()
    print(f"看板服务已启动： http://{host}:{port}")
    print(f"自动刷新间隔 {interval} 秒；首次扫描 {first['scan']['seconds']} 秒")
    print("只读本地会话记录，不消耗 token。Ctrl-C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main():
    parser = argparse.ArgumentParser(description="AI CLI token 消耗看板")
    parser.add_argument("--json", metavar="PATH",
                        help="只导出聚合 JSON，不生成 HTML")
    parser.add_argument("--out", default=str(HERE / "dashboard.html"),
                        help="输出的 HTML 路径")
    parser.add_argument("--serve", nargs="?", const=8899, type=int, metavar="PORT",
                        help="起本地服务（默认 8899）")
    parser.add_argument("--interval", type=int, default=60, metavar="SEC",
                        help="服务模式下的自动刷新间隔，默认 60 秒")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（不要改成 0.0.0.0）")
    args = parser.parse_args()

    if args.serve:
        serve(args.serve, max(5, args.interval), args.host)
        return

    payload = collect()

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已导出 {args.json}")
        return

    render(payload, Path(args.out))

    print(f"已生成 {args.out}  (扫描 {payload['scan']['seconds']} 秒)")
    for profile in payload["profiles"]:
        if not profile["present"]:
            print(f"  {profile['label']:<12} 目录不存在，跳过 ({profile['dir']})")
            continue
        line = (f"  {profile['label']:<12} "
                f"增量 {profile['incr']:>13,}  "
                f"缓存读 {profile['cache_read']:>13,}  "
                f"调用 {profile['msgs']:>6,}")
        if profile["deduped"]:
            line += f"  (去重 {profile['deduped']:,})"
        print(line)


if __name__ == "__main__":
    main()
