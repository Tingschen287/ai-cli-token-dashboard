#!/usr/bin/env python3
"""对比 Kimi K3 在两种接入方式下的 prompt 缓存命中率：

  官方  kimi code cli          ~/.kimi-code   (usage.record, model kimi-code/k3)
  转发  claude code+cc-switch  ~/.claude      (message.usage, model k3)

命中率 = cache_read / (input + cache_read + cache_write)，两边口径一致
（input 均不含 cache_read，kimi 侧已用 token_counting 实测验证）。

只读本地会话记录，不联网、不消耗 token。

用法：
    python3 cache_compare.py                        # 全部历史
    python3 cache_compare.py --since 2026-08-20     # 只看该日期起（实验时段）
"""
import argparse
import glob
import os
from pathlib import Path

import collect


def is_k3(model):
    # kimi 官方记 kimi-code/k3，cc-switch 转发记 k3，都按 K3 切片
    return bool(model) and model.split("/")[-1].lower().startswith("k3")


def scan(root, pattern, parser, since):
    """按 collect.collect() 相同的顺序处理：先按 dedup_key 跨文件去重，再滤全零占位。"""
    seen, rows = set(), []
    for f in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
        try:
            rs, _, _ = collect._rows_of(Path(f), parser)
        except OSError:
            continue
        for r in rs:
            if r.dedup_key:
                if r.dedup_key in seen:
                    continue
                seen.add(r.dedup_key)
            if r.input + r.output + r.cache_write + r.cache_read == 0:
                continue
            if is_k3(r.model) and (not since or r.date >= since):
                rows.append(r)
    return rows


def stats(rows):
    b = dict(calls=0, input=0, output=0, cache_read=0, cache_write=0)
    for r in rows:
        b["calls"] += 1
        b["input"] += r.input
        b["output"] += r.output
        b["cache_read"] += r.cache_read
        b["cache_write"] += r.cache_write
    return b


def line(name, b):
    prefix = b["input"] + b["cache_read"] + b["cache_write"]
    incr = b["input"] + b["output"] + b["cache_write"]
    hit = b["cache_read"] / prefix * 100 if prefix else 0.0
    print(f"  {name:<24} 调用 {b['calls']:<5} 增量 {incr:>12,}  "
          f"缓存读 {b['cache_read']:>13,}  缓存写 {b['cache_write']:>11,}  命中率 {hit:5.1f}%")
    return hit


def main():
    ap = argparse.ArgumentParser(description="Kimi K3 缓存命中率对比")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="只统计该日期起的数据")
    args = ap.parse_args()
    home = str(Path.home())
    kimi = scan(home + "/.kimi-code", "sessions/*/session_*/agents/*/wire.jsonl",
                collect.parse_kimi_file, args.since)
    ccs = scan(home + "/.claude", "projects/**/*.jsonl",
               collect.parse_claude_file, args.since)

    print(f"Kimi K3 缓存命中对比（{'自 ' + args.since if args.since else '全部历史'}）：")
    if not kimi:
        print("  [warn] kimi code cli 没有 K3 记录——确认用了 -m kimi-code/k3")
    if not ccs:
        print("  [warn] cc-switch 没有 K3 记录")
    line("kimi code cli（官方）", stats(kimi))
    line("claude code+cc-switch", stats(ccs))
    print("\n口径：命中率 = cache_read / (input + cache_read + cache_write)；"
          "增量 = input + output + cache_write。两边 input 均不含 cache_read。")


if __name__ == "__main__":
    main()
