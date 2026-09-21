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
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from coding_plans import CodingPlanPoller

# 要扫描的配置目录。key 是看板标识，dir 支持 ~ 展开，format 决定用哪个解析器。
PROFILES = [
    {"key": "cco",   "label": "Claude Official", "dir": "~/.claude-official", "format": "claude"},
    {"key": "kimi",  "label": "Kimi",            "dir": "~/.kimi-code",       "format": "kimi"},
    {"key": "codex", "label": "ChatGPT",         "dir": "~/.codex",           "format": "codex"},
    {"key": "ccs",   "label": "CC-Switch",       "dir": "~/.claude",          "format": "claude"},
    # grok 一个看板行并两套 CLI 配置：个人 SuperGrok + 团队（dir 逗号分隔）
    {"key": "grok",  "label": "Grok",            "dir": "~/.grok,~/.grok-team", "format": "grok"},
    {"key": "oc",    "label": "OpenCode",        "dir": "~/.local/share/opencode", "format": "opencode"},
]

HERE = Path(__file__).resolve().parent

# 一条标准化记录：两种解析器都产出这个形状，下游只认它。
# ts 是 epoch 秒（0=未知）：撞墙窗口估计需要小时级滚动聚合，日粒度 date 不够用
Row = collections.namedtuple(
    "Row", "dedup_key date model project input output cache_write cache_read reasoning calls cost_ticks ts",
    defaults=(0,))


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


def _epoch(value):
    """ISO 串或 unix 秒/毫秒 → epoch 秒；无法解析返回 0（撞墙窗口聚合会跳过 0）。"""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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
                ts=_epoch(rec.get("timestamp")),
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
                    ts=_epoch(rec.get("timestamp")),
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
                ts=t / 1000 if isinstance(t, (int, float)) else 0,
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
                ts=_epoch(rec.get("timestamp")),
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
                # opencode 的 cost 是 USD 实估值；cost_ticks 统一 1 USD=1e10 ticks
                # （xAI 官方口径），前端 /1e10 还原。
                cost_ticks=round((d.get("cost") or 0) * 1e10),
                ts=t / 1000 if isinstance(t, (int, float)) else 0,
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
        # primary / secondary 谁短谁长随套餐变：老 pro 是 primary=周窗
        # （10080 分钟）、secondary 空；现在 team 是 primary=5h（300 分钟）、
        # secondary=周窗。不能按字段名硬编码，一律看 window_minutes。
        for src in (rl.get("primary"), rl.get("secondary")):
            if not isinstance(src, dict) or src.get("used_percent") is None:
                continue
            reset = src.get("resets_at")
            windows.append({
                # >1 天按周窗样式展示（前端 key!=='5h' 走日期+时刻）
                "key": "week" if (src.get("window_minutes") or 0) > 1440 else "5h",
                "pct": round(src.get("used_percent") or 0),
                "reset": (datetime.fromtimestamp(reset).astimezone()
                          .isoformat(timespec="seconds") if reset else ""),
            })
        windows.sort(key=lambda w: 0 if w["key"] == "5h" else 1)
        if windows:
            return {"windows": windows, "plan": rl.get("plan_type") or ""}
    return None


def blank():
    return {"incr": 0, "cache_read": 0, "output": 0, "cache_write": 0,
            "input": 0, "reasoning": 0, "msgs": 0, "cost_ticks": 0}


# OpenCode Zen 免费模型（muse-spark）没有公开限额接口，超限报
# rate_limit_exceeded（服务端日志可查）。撞墙日已用量就是限额的下界样本，
# 多日取均值当估计分母；前端拿它算「今日已用百分比」。
ZEN_FREE_MODEL = "muse-spark-1.3-contributor-free"
_ZEN_CACHE = {"sig": None, "days": frozenset()}


def zen_hit_days(root):
    """opencode.log 里 rate_limit_exceeded 出现过的本地日期集合。

    日志是追加式单文件，按 (mtime, size) 缓存解析结果；时间戳是 UTC，
    转本地日期才能对齐会话扫描的日粒度。
    """
    log = root / "log" / "opencode.log"
    try:
        st = log.stat()
    except OSError:
        return frozenset()
    sig = (st.st_mtime_ns, st.st_size)
    if _ZEN_CACHE["sig"] == sig:
        return _ZEN_CACHE["days"]
    days = set()
    try:
        with log.open(errors="ignore") as f:
            for line in f:
                # 服务端有过两种文案：带 [rate_limit_exceeded] code 的和纯文本的
                low = line.lower()
                if "rate_limit_exceeded" not in low and "rate limit exceeded" not in low:
                    continue
                try:
                    t = datetime.strptime(line[10:29], "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    continue
                days.add(t.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d"))
    except OSError:
        return frozenset()
    _ZEN_CACHE.update(sig=sig, days=frozenset(days))
    return _ZEN_CACHE["days"]


# ---------- 撞墙窗口估计（口径 A：撞墙时刻往前的滚动窗口） ----------
# 个人套餐（cco 5h/周、codex credits、grok 周额度）没有公开的窗口 token 上限，
# 撞墙时刻往前一个窗口的用量就是该窗口实际消耗的极限样本，多次撞墙取均值。
# 用量口径与 zen 一致：全 token（增量 + 缓存读 + reasoning），因为各家的窗口
# 计量都把缓存读算进去。

_TAIL_CACHE = {}  # path -> (consumed_bytes, [hit, ...])


def _tail_hits(path, match):
    """append-only 文件的增量撞墙扫描：记住已消费字节，只解析新增行。"""
    try:
        st = path.stat()
        consumed, hits = _TAIL_CACHE.get(str(path), (0, []))
    except OSError:
        return []
    if consumed == st.st_size:
        return hits
    try:
        with path.open("rb") as f:
            if st.st_size < consumed:      # 文件被截断/轮转，重头扫
                f.seek(0)
                consumed, hits = 0, []
            else:
                f.seek(consumed)
            data = f.read()
    except OSError:
        return hits
    if data and not data.endswith(b"\n"):  # 尾部半行留到下一轮
        nl = data.rfind(b"\n")
        data = data[:nl + 1] if nl >= 0 else b""
    fresh = list(hits)
    for line in data.decode("utf-8", "ignore").splitlines():
        hit = match(line)
        if hit is not None:
            fresh.append(hit)
    _TAIL_CACHE[str(path)] = (consumed + len(data), fresh)
    return fresh


def claude_limit_hits(root):
    """cco：assistant 报错行 'You've hit your (session|weekly) limit'。
    同一窗口的连环报错按文案里的 resets 时刻去重，返回 [(epoch, is_weekly)]。"""
    out = {}

    def match(line):
        if "isApiErrorMessage" not in line or "hit your" not in line:
            return None
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        text = d.get("message", {}).get("content") or ""
        if isinstance(text, list):
            text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
        if "limit" not in text:
            return None
        ts = _epoch(d.get("timestamp"))
        if not ts:
            return None
        reset = next((w for w in text.replace("·", " ").split() if ":" in w and "resets" not in w), "")
        return (ts, "week" if "weekly" in text.lower() else "5h",
                f"{reset}|{ts // 3600 // 6}")

    for path in root.glob("projects/**/*.jsonl"):
        for ts, weekly, key in _tail_hits(path, match):
            out.setdefault(key, (ts, weekly))
    return list(out.values())


def codex_limit_hits(root):
    """codex：三类撞墙信号归一化为 (epoch, '5h'|'week')。

    1. rate_limits 的 primary/secondary used_percent ≥99（5h/周窗打满），
       按 resets_at 去重——同一窗口反复采样只算一次；
    2. rate_limit_reached_type 非空（Team 时期 credits 耗尽），归入触发时
       已打满的那个窗，按小时去重。
    """
    out = {}

    def match(line):
        if '"rate_limits"' not in line:
            return None
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        rl = (d.get("payload") or {}).get("rate_limits") or {}
        ts = _epoch(d.get("timestamp"))
        if not ts:
            return None
        found = []
        for w, kind in ((rl.get("primary"), "5h"), (rl.get("secondary"), "week")):
            if not w or w.get("window_minutes") not in (300, 10080):
                continue
            expect = 300 if kind == "5h" else 10080
            if w.get("window_minutes") != expect:
                kind = "week" if w.get("window_minutes") == 10080 else "5h"
            if (w.get("used_percent") or 0) >= 99:
                # resets_at 相同 = 同一个窗口实例，去重
                found.append((ts, kind, f"{kind}:{w.get('resets_at')}"))
        if rl.get("rate_limit_reached_type"):
            found.append((ts, "week", f"credits:{ts // 3600}"))
        return found or None

    for path in root.glob("sessions/*/*/*/rollout-*.jsonl"):
        for group in _tail_hits(path, match):
            for ts, kind, key in group:
                out.setdefault(key, (ts, kind))
    return sorted(out.values())


def grok_limit_hits(root):
    """grok：retry_state failed 且报错文案是限额类（余额耗尽 / 消费上限 /
    限流）。同一小时去重，返回 [(epoch, False)]。"""
    out = set()

    def match(line):
        low = line.lower()
        if '"retry_state"' not in low or '"failed"' not in low:
            return None
        if not any(s in low for s in ("balance exhausted", "spending-limit", "rate limit", "quota")):
            return None
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        ts = _epoch(d.get("timestamp"))
        return ts or None

    for path in root.glob("sessions/*/*/updates.jsonl"):
        for ts in _tail_hits(path, match):
            out.add(ts // 3600)
    return [(h * 3600, "week") for h in sorted(out)]


# 来源 → [(窗口标签, 窗口秒数, 扫描函数, 只取周窗样本)]；cco 两窗都估
# 第 4 项是样本过滤 kind：与扫描器返回的 (epoch, kind) 匹配，None = 全收
LIMIT_SPECS = {
    "cco": [("5h", 5 * 3600, claude_limit_hits, "5h"),
            ("week", 7 * 86400, claude_limit_hits, "week")],
    "codex": [("5h", 5 * 3600, codex_limit_hits, "5h"),
              ("week", 7 * 86400, codex_limit_hits, "week")],
    "grok": [("week", 7 * 86400, grok_limit_hits, "week")],
}
# 套餐切换日：之前的撞墙属于旧套餐（额度不同），不当样本。换套餐时改这里。
LIMIT_SINCE = {
    "cco": "2026-09-10",    # 09-10 起从个人 Pro 切到 Team
    "codex": "2026-09-03",  # 之前是 Pro，现在是 Plus（credits 口径不同）
}


def est_limit(key, roots, rows_ts):
    """滚动窗口均值：对每次撞墙 t 取 [t-窗口, t] 的全 token 合计，多样本取均值。
    rows_ts 是该来源 (epoch, 全token) 的有序列表，前缀和 + bisect 求区间和。"""
    import bisect
    result = {}
    if not rows_ts:
        return result
    since = LIMIT_SINCE.get(key)
    since_ts = _epoch(since + "T00:00:00") if since else 0
    times = [t for t, _ in rows_ts]
    prefix = [0]
    for _, v in rows_ts:
        prefix.append(prefix[-1] + v)
    for label, window, scanner, weekly in LIMIT_SPECS[key]:
        hits = []
        for root in roots:
            try:
                hits.extend(scanner(root))
            except Exception:
                pass
        picked = [t for t, w in hits
                  if (weekly is None or w == weekly) and t >= since_ts]
        picked = sorted(set(round(t / 3600) * 3600 for t in picked))  # 同小时去重
        if not picked:
            continue
        vals = []
        for t in picked:
            lo = bisect.bisect_left(times, t - window)
            hi = bisect.bisect_right(times, t)
            if hi > lo:
                vals.append(prefix[hi] - prefix[lo])
        vals = [v for v in vals if v > 0]
        if vals:
            result[label] = {"avg_tokens": round(sum(vals) / len(vals)),
                             "samples": len(vals)}
    return result


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
    # 撞墙窗口估计要滚动聚合：按来源攒 (epoch, 全token) 有序对，ts=0 的跳过
    rows_ts = {p["key"]: [] for p in PROFILES}
    scanned = cached = 0

    for profile in PROFILES:
        key = profile["key"]
        # dir 支持逗号分隔的多目录：同名来源合并成一行的场景（grok 个人+团队）
        roots = [Path(os.path.expanduser(d)) for d in profile["dir"].split(",")]
        parser, pattern = FORMATS[profile["format"]]
        seen = set()
        raw_rows = duplicates = 0

        for root in [r for r in roots if r.is_dir()]:
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
                    if row.ts and key in LIMIT_SPECS:
                        rows_ts[key].append((row.ts, row.input + row.output
                                             + row.cache_write + row.cache_read + row.reasoning))

        meta[key] = {
            "label": profile["label"],
            "dir": profile["dir"],
            "present": any(r.is_dir() for r in roots),
            "raw_rows": raw_rows,
            "deduped": duplicates,
        }
        # codex 的额度在会话文件里白送（rate_limits），扫描时顺手取，不联网
        if key == "codex" and roots[0].is_dir():
            q = codex_quota(root)
            if q:
                meta[key]["quota"] = q
        # oc 的免费模型限额无接口，用日志撞墙日的已用量均值当估计分母。
        # 口径用全部 token（含缓存读）：Zen 服务端按全 token 计量限额，
        # 与看板主指标 incr（不含缓存读）不同，前端分子也按同一口径加总。
        # 每次扫描都重算（日志按 (mtime,size) 增量、用量桶现聚合）；实测
        # 滚动 24h 口径不成立（前 24h 已用 83M 未撞墙 > 撞墙日均值），zen
        # 更像请求节流/共享池，自然日均值只作粗略水位参考。
        if key == "oc" and roots[0].is_dir():
            days = zen_hit_days(root)
            vals = []
            for day in sorted(days):
                b = models.get((day, key, ZEN_FREE_MODEL))
                if b and b["msgs"]:
                    vals.append(b["incr"] + b["cache_read"] + b["reasoning"])
            if vals:
                meta[key]["zen_limit"] = {
                    "model": ZEN_FREE_MODEL,
                    "avg_tokens": round(sum(vals) / len(vals)),
                    "samples": len(vals),
                    "hit_days": sorted(days),
                }
        # 个人套餐的窗口 token 上限估计：撞墙时刻往前滚动窗口的全 token 均值。
        # grok 是两套目录（个人+团队），撞墙扫描对每个目录都跑再合并
        if key in LIMIT_SPECS and any(r.is_dir() for r in roots):
            rows_ts[key].sort()
            est = est_limit(key, [r for r in roots if r.is_dir()], rows_ts[key])
            if est:
                meta[key]["est_limit"] = est

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
# 顺序有意义：全部拼成一个 script 块，app.js 末尾会立即执行 renderAll()，
# 所以它依赖的 vps.js 顶层常量必须先初始化完（函数声明会提升，const 不会）。
JS_FILES = ["brand.js", "prices.js", "data.js", "layout.js", "calendar.js",
            "charts.js", "vps.js", "coding-plans.js", "app.js"]


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
# new-api 网关的面板「系统访问令牌」：base_url -> token，与 Key 同样不进 git。
NEWAPI_CONF = Path(__file__).resolve().parent / "newapi.local.json"
_NEWAPI_CACHE = {"sig": None, "tokens": {}}


def _newapi_access_tokens():
    """有令牌才能查 new-api 账号层（订阅余额）；按 (mtime, size) 缓存热读。"""
    try:
        st = NEWAPI_CONF.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    if _NEWAPI_CACHE["sig"] == sig:
        return _NEWAPI_CACHE["tokens"]
    tokens = {}
    try:
        data = json.loads(NEWAPI_CONF.read_text(encoding="utf-8"))
        for base, tok in (data.get("tokens") or {}).items():
            if isinstance(tok, str) and tok.strip():
                tokens[str(base).rstrip("/")] = tok.strip()
    except (ValueError, OSError, AttributeError):
        tokens = {}
    _NEWAPI_CACHE.update(sig=sig, tokens=tokens)
    return tokens


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


def _is_private_host(base):
    """内网自建网关（new-api 部署）按私网地址识别；云厂商 URL 不在其中。"""
    from urllib.parse import urlparse
    host = (urlparse(base).hostname or "")
    if host.count(".") != 3:
        return False
    a, b = (int(x) if x.isdigit() else -1 for x in host.split(".")[:2])
    return (a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)) and 0 <= b <= 255


def _ccs_quota_providers():
    """cc-switch 库里有额度接口的 claude 供应商。

    Kimi 的额度条已挪到 kimi code 行（同一账号、同一个 /v1/usages 接口，
    只是认证换成 CLI 的 OAuth token）；MiniMax 是部门套餐、在弹窗里，与
    cc-switch 个人供应商无关。私网地址的供应商按 new-api 处理。
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
        if base and token and _is_private_host(base):
            out.append({"name": name, "base": base, "token": token})
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


def _quota_newapi(base, token, access_token=None):
    """new-api 网关：sk- key 只能到令牌层，billing/usage 的 total_usage 是
    美分、只有累计已用。面板「系统访问令牌」才到账号层：订阅制站点的余额在
    /api/subscription/self（钱包 quota 恒为 0），无订阅的充值站回退钱包
    quota；展示单位跟 /api/status 站点配置走（quota_per_unit /
    quota_display_type），不硬编码人民币。"""
    root = base.rstrip("/")
    out = {}
    d = _http_get_json(root + "/v1/dashboard/billing/usage", {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Token-Dashboard/1.0",
    })
    cents = d.get("total_usage")
    if isinstance(cents, (int, float)) and not isinstance(cents, bool):
        out["spend"] = round(cents / 100, 2)
    if not access_token:
        return out or None
    auth = {"Authorization": access_token, "Accept": "application/json",
            "User-Agent": "Token-Dashboard/1.0"}
    user = (_http_get_json(root + "/api/user/self", auth).get("data") or {})
    sub = None
    try:
        subs = (_http_get_json(root + "/api/subscription/self", auth)
                .get("data") or {}).get("subscriptions") or []
        sub = next((s["subscription"] for s in subs
                    if isinstance(s.get("subscription"), dict)
                    and s["subscription"].get("status") == "active"), None)
    except Exception:
        sub = None
    quota = user.get("quota") if isinstance(user.get("quota"), (int, float)) else 0
    used_q = user.get("used_quota") if isinstance(user.get("used_quota"), (int, float)) else 0
    if sub is not None:
        total_q, sub_used = sub.get("amount_total") or 0, sub.get("amount_used") or 0
        remain_q, expire = total_q - sub_used, sub.get("end_time") or 0
    else:
        remain_q, total_q, expire = quota, quota + used_q, 0
    conf = {}
    try:
        conf = _http_get_json(root + "/api/status", auth).get("data") or {}
    except Exception:
        conf = {}
    per_unit = conf.get("quota_per_unit") or 500000
    rate = (conf.get("usd_exchange_rate") or 1) if conf.get("quota_display_type") == "CNY" else 1
    scale = lambda q: round(q / per_unit * rate, 2)
    balance = {"total": scale(remain_q), "plan_total": scale(total_q),
               "used": scale(total_q - remain_q)}
    if expire > 0:
        balance["expire_at"] = expire
    out.update(balance=balance, currency="CNY" if conf.get("quota_display_type") == "CNY" else "USD",
               # 账号级累计已用覆盖该账号全部令牌，比令牌层 billing 更全。
               spend=scale(used_q))
    return out


def quota_ccs():
    """new-api 网关的订阅余额或累计已用（失败单独报错不拖垮整组）。"""
    tokens = _newapi_access_tokens()
    out = []
    for p in _ccs_quota_providers():
        try:
            access = tokens.get(p["base"].rstrip("/"))
            out.append({"name": p["name"], "windows": [], "error": None,
                        **(_quota_newapi(p["base"], p["token"], access) or {})})
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


XAI_MGMT_ENV = Path.home() / ".grok" / "xai-management-key.env"
XAI_API_ENV = Path.home() / ".grok" / "xai-api-key.env"


def _env_file_vars(path):
    """极简 `export K="V"` 解析，只服务本地凭据文件，不碰 shell。"""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, _, v = line[len("export "):].partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def quota_xai_team():
    """xAI 团队账单（Management API）：用量分析按 api_key_id 分组，识别本机
    这把 key（列表 redactedApiKey 的尾四位与本地 key 尾缀匹配）的本月实付。
    普通团队 key 调不通（401），必须是 console 的 Management Key。"""
    mgmt = _env_file_vars(XAI_MGMT_ENV)
    token, team = mgmt.get("XAI_MANAGEMENT_KEY"), mgmt.get("XAI_TEAM_ID")
    if not token or not team:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "User-Agent": "Token-Dashboard/1.0"}
    keys = _http_get_json(
        f"https://management-api.x.ai/auth/teams/{team}/api-keys",
        headers).get("apiKeys") or []
    local = _env_file_vars(XAI_API_ENV).get("XAI_API_KEY") or ""
    mine = next((k for k in keys
                 if local and (k.get("redactedApiKey") or "").endswith(local[-4:])), None)
    now = datetime.now().astimezone()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    body = json.dumps({"analyticsRequest": {
        "timeRange": {"startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                      "endTime": (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                      "timezone": "Etc/GMT"},
        "timeUnit": "TIME_UNIT_NONE",
        "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
        "groupBy": ["api_key_id"], "filters": []}}).encode()
    req = urllib.request.Request(
        f"https://management-api.x.ai/v1/billing/teams/{team}/usage",
        data=body, headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        series = json.loads(resp.read().decode("utf-8")).get("timeSeries") or []
    by_key = {ts["group"][0]: sum(dp["values"][0] for dp in ts.get("dataPoints") or [])
              for ts in series if ts.get("group")}
    out = {"currency": "USD", "team_spend": round(sum(by_key.values()), 2)}
    if mine:
        out["key_name"] = mine.get("name") or "api-key"
        out["key_spend"] = round(by_key.get(mine.get("apiKeyId"), 0.0), 2)
    return out


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
                        ("ccs", quota_ccs), ("grok", quota_grok),
                        ("xai", quota_xai_team)):
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
            merged["xai"] = fresh["xai"] or old.get("xai")
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


VPS_INTERVAL = 600
VPS_CONF = HERE / "vps.local.json"


def vps_config():
    """VPS 连接配置。没配就返回 None，整个功能静默关闭。

    地址放 `vps.local.json`（已在 .gitignore 里）或环境变量 `VPS_SSH_HOST`，
    不写进仓库——那是用户自己的服务器地址。
    """
    conf = {}
    try:
        conf = json.loads(VPS_CONF.read_text(encoding="utf-8"))
    except Exception:
        pass
    host = os.environ.get("VPS_SSH_HOST") or conf.get("host")
    if not host:
        return None
    return {
        "host": host,
        "quota_gb": float(conf.get("quota_gb", 1000)),
        "reset_day": int(conf.get("reset_day", 25)),
        "tz_offset": float(conf.get("tz_offset", 8)),
        "ssh_opts": [str(x) for x in conf.get("ssh_opts", [])],
        "label": str(conf.get("label", "Bandwidth Usage")),
    }


def vps_probe(conf):
    """一次 SSH 往返：把 vps_probe.py 的源码喂给远端 python3，收回聚合 JSON。

    远端不落盘、不常驻、不开端口。VPS 是翻墙节点，多开一个对外 HTTP 接口
    就多一个被扫描的入口，而 SSH 本来就是现成的加密通道；聚合在远端做，
    回来的只有几 KB。数值全部先转成数字再拼进命令行，配置文件被改坏也
    注入不进东西。
    """
    script = (HERE / "vps_probe.py").read_bytes()
    remote = ("python3 - --quota-gb %.3f --reset-day %d --tz-offset %.2f"
              % (conf["quota_gb"], conf["reset_day"], conf["tz_offset"]))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=10", *conf["ssh_opts"], conf["host"], remote]
    done = subprocess.run(cmd, input=script, capture_output=True, timeout=90)
    if done.returncode != 0:
        err = (done.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(err.splitlines()[-1][:200] if err else "ssh 退出码 %d" % done.returncode)
    data = json.loads(done.stdout.decode("utf-8"))
    data["label"] = conf["label"]
    return data


class VpsPoller:
    """慢轮询 VPS 流量。失败沿用旧数据并标 stale，页面不会突然空掉。

    间隔比额度轮询长得多（默认 10 分钟）：流量是缓变量，而每次轮询是一次
    完整的 SSH 握手，没必要频繁。
    """

    def __init__(self, conf, interval=VPS_INTERVAL):
        self.conf = conf
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = None

    def get(self):
        with self.lock:
            return self.latest

    def poll_once(self):
        try:
            fresh = vps_probe(self.conf)
            fresh["stale"] = False
            fresh["error"] = ""
        except Exception as exc:
            with self.lock:
                if self.latest:
                    self.latest = {**self.latest, "stale": True, "error": str(exc)}
                else:
                    self.latest = {"error": str(exc), "stale": True}
            return
        with self.lock:
            self.latest = fresh

    def loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                print(f"[warn] VPS 流量轮询失败: {exc}", file=sys.stderr)
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
    coding_plans = CodingPlanPoller()
    threading.Thread(target=coding_plans.loop, daemon=True).start()
    # 没配 VPS 就整个不启动，页面上那块也不会出现
    vps_conf = vps_config()
    vps = VpsPoller(vps_conf) if vps_conf else None
    if vps:
        threading.Thread(target=vps.loop, daemon=True).start()

    def with_quota(payload):
        # 浅拷贝后挂额度，不污染 Snapshot 里的共享对象（_fill_week_est 会改
        # est_limit，连这一层也要拷，否则每次请求样本数 +1）
        extra = {**payload, "quota": quota.get(),
                 "profiles": [
                     {**p, "est_limit": dict(p["est_limit"])} if "est_limit" in p else dict(p)
                     for p in payload.get("profiles", [])
                 ]}
        if vps:
            extra["vps"] = vps.get()
        _fill_week_est(extra)
        return extra

    def _fill_week_est(payload):
        """cco 周窗没有撞墙报错样本（撞 5h 先挡住了）：额度已 ≥90% 的进行中
        周窗，其窗口用量本身就是接近上限的样本——用重置时间反推窗口起点，
        日粒度聚合近似（跨日误差可接受，反正是估计）。"""
        cco_q = (payload.get("quota") or {}).get("cco") or {}
        week = next((w for w in cco_q.get("windows", []) if w["key"] == "week"), None)
        if not week or not week.get("reset") or week.get("pct", 0) < 90:
            return
        try:
            reset = datetime.fromisoformat(week["reset"])
        except ValueError:
            return
        start = (reset - timedelta(days=7)).date().isoformat()
        total = 0
        for row in payload.get("daily", []):
            if row["profile"] == "cco" and start <= row["date"] <= reset.date().isoformat():
                total += row["incr"] + row["cache_read"] + row["reasoning"]
        if total <= 0:
            return
        profile = next((p for p in payload.get("profiles", []) if p["key"] == "cco"), None)
        if not profile:
            return
        old = profile.get("est_limit", {}).get("week") or {}
        n = old.get("samples", 0)
        # live 窗口并入既有撞墙样本的均值，而不是顶掉
        profile.setdefault("est_limit", {})["week"] = {
            "avg_tokens": round((old.get("avg_tokens", 0) * n + total) / (n + 1)),
            "samples": n + 1, "live": True}

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
            elif path == "/api/coding-plans":
                if urllib.parse.parse_qs(query).get("refresh") == ["1"]:
                    coding_plans.request_refresh()
                self._send(json.dumps(coding_plans.get(), ensure_ascii=False).encode("utf-8"),
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
    parser.add_argument("--vps-once", action="store_true",
                        help="只跑一次 VPS 流量采集并打印，用来调试 SSH 配置")
    args = parser.parse_args()

    if args.vps_once:
        conf = vps_config()
        if not conf:
            print("没有配置 VPS：建 vps.local.json 写 {\"host\": \"root@1.2.3.4\"} "
                  "或设环境变量 VPS_SSH_HOST", file=sys.stderr)
            return 1
        try:
            print(json.dumps(vps_probe(conf), ensure_ascii=False, indent=2))
        except Exception as exc:
            print("采集失败：%s" % exc, file=sys.stderr)
            print("排查：先确认 `ssh %s 'echo ok'` 免密能通，再确认远端有 "
                  "/usr/local/s-ui/db/s-ui.db" % conf["host"], file=sys.stderr)
            return 1
        return 0

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
    # main() 的返回值就是退出码（没有显式返回时是 None，等同于 0），
    # 这样 --vps-once 之类的调试入口失败时能被脚本检测到
    sys.exit(main())
