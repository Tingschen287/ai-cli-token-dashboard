#!/usr/bin/env python3
"""扫描本机各 AI 编码 CLI 的会话记录，聚合 token 消耗并生成看板。

只读本地文件，不联网、不调任何 API——刷新多少次都不消耗 token。

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
from datetime import datetime, timezone
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
                self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path in ("/", "/index.html", "/dashboard.html"):
                html = template.read_text(encoding="utf-8")
                blob = json.dumps(snapshot.get(), ensure_ascii=False).replace("</", "<\\/")
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
