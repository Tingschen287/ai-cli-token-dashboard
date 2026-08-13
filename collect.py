#!/usr/bin/env python3
"""扫描本机各 AI 编码 CLI 的会话记录，聚合 token 消耗并生成看板。

静态生成只读本地文件、不联网、不调任何 API——刷新多少次都不消耗 token。
服务模式（--serve）额外开一个额度轮询线程，查询各平台套餐额度并合并进
/api/data（见 QuotaPoller 一节）；这是唯一的联网行为，失败自动沿用旧数据。

支持两种记录格式：
  claude  <config_dir>/projects/<项目>/<会话>.jsonl
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
    {"key": "cco",  "label": "Claude Official", "dir": "~/.claude-official", "format": "claude"},
    {"key": "ccs",  "label": "CC-Switch",       "dir": "~/.claude",          "format": "claude"},
    {"key": "grok", "label": "Grok",            "dir": "~/.grok",            "format": "grok"},
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


FORMATS = {
    "claude": (parse_claude_file, "projects/*/*.jsonl"),
    "grok":   (parse_grok_file,   "sessions/*/*/updates.jsonl"),
}

# 文件级缓存：path -> (签名, rows, raw_count)
# 会话记录是 append-only 的历史，改过的文件才需要重读。常态下每分钟只有
# 正在写入的那一两个文件变化，其余全部命中缓存。
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _rows_of(path, parser):
    st = path.stat()
    sig = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(path)
        if hit and hit[0] == sig:
            return hit[1], hit[2], True
    rows, raw = parser(path)
    with _CACHE_LOCK:
        _CACHE[path] = (sig, rows, raw)
    return rows, raw, False


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
    # 没有日期就只能给全时段总量。实测组合数不到 250 条，代价约 30KB。
    models = collections.defaultdict(blank)      # (date, profile, model)
    projects = collections.defaultdict(blank)    # (date, profile, project)
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
                    accumulate(projects[(row.date, key, row.project)], row)
                    accumulate(totals[key], row)

        meta[key] = {
            "label": profile["label"],
            "dir": profile["dir"],
            "present": root.is_dir(),
            "raw_rows": raw_rows,
            "deduped": duplicates,
        }

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
        "projects": sorted(flatten(projects, ("date", "profile", "project")),
                           key=lambda r: (r["date"], r["profile"], r["project"])),
    }


def render(payload, template, target):
    html = template.read_text(encoding="utf-8")
    marker = "/*__DATA__*/null"
    if marker not in html:
        sys.exit(f"模板缺少数据占位符 {marker}: {template}")
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
    """cc-switch 库里有额度接口的 claude 供应商（Kimi/MiniMax）。

    不管当前启用的是谁，两家都独立查询、独立展示（用户明确要求不合并）；
    腾云智算这类没有额度接口的供应商不出现在列表里。
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
        low = base.lower()
        if "kimi.com/coding" in low and token:
            out.append({"name": name, "kind": "kimi", "base": base, "token": token})
        elif "minimax" in low and token:
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
        for key, fn in (("cco", quota_cco), ("ccs", quota_ccs), ("grok", quota_grok)):
            try:
                fresh[key] = fn()
            except Exception:
                fresh[key] = None
        with self.lock:
            old = self.latest
            merged = {}
            merged["cco"] = fresh["cco"] or old.get("cco")
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

    template = HERE / "template.html"
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
                html = template.read_text(encoding="utf-8")
                blob = json.dumps(with_quota(snapshot.get()), ensure_ascii=False).replace("</", "<\\/")
                self._send(html.replace("/*__DATA__*/null", blob).encode("utf-8"),
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

    render(payload, HERE / "template.html", Path(args.out))

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
