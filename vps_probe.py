#!/usr/bin/env python3
"""在 VPS 上就地聚合流量，输出一份小 JSON。

这个文件不部署到 VPS——`collect.py` 把它的源码通过 SSH stdin 喂给远端
`python3 -` 执行，远端不落盘、不常驻、不开端口。改完这里立刻生效。

两个口径，故意分开，不要混：

  billing  网卡进出字节（vnstat），和服务商账单同口径，决定会不会超额。
  user     S-UI 记的每个账号实际用量，能分人，但天生约为账单的一半
           ——一个字节进来再出去，网卡上算两遍。

VPS 跑在 UTC，看板使用者在 +8。S-UI 存的是 unix 时间戳，能精确按 +8 切天；
vnstat 只按它自己的本地日切天，所以 vnstat **只用于总量校准，不逐日匹配**，
避开 8 小时错位。
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

FALLBACK_RATIO = 2.0    # 没有实测重叠期时，双向计费的理论倍数


def cycle_start(now, reset_day):
    """当前计费周期的起点（本地时区 reset_day 零点）。"""
    start = now.replace(day=reset_day, hour=0, minute=0, second=0, microsecond=0)
    if now.day < reset_day:
        start = (start.replace(day=1) - timedelta(days=1)).replace(
            day=reset_day, hour=0, minute=0, second=0, microsecond=0)
    return start


def sui_daily(con, since_ts, tz_hours):
    """按天按账号聚合。direction 0=up 1=down（拿 clients 表的 up/down 比例核对过）。"""
    rows = con.execute(
        """
        SELECT tag,
               CAST(strftime('%s', date_time, 'unixepoch', ?) AS INTEGER) / 86400
                   AS day_idx,
               SUM(CASE WHEN direction = 0 THEN traffic ELSE 0 END),
               SUM(CASE WHEN direction = 1 THEN traffic ELSE 0 END)
        FROM stats
        WHERE resource = 'user' AND date_time >= ?
        GROUP BY tag, day_idx
        """, ("%+d hours" % tz_hours, since_ts)).fetchall()
    out = {}
    for tag, day_idx, up, down in rows:
        date = (datetime(1970, 1, 1) + timedelta(days=day_idx)).strftime("%Y-%m-%d")
        out.setdefault(date, {})[tag] = {"up": int(up or 0), "down": int(down or 0)}
    return out


def vnstat_daily():
    """vnstat 按天的 rx+tx。没装或拿不到就返回空，上层自动退回估算。"""
    try:
        raw = subprocess.run(["vnstat", "--json", "d"], capture_output=True,
                             text=True, timeout=15).stdout
        data = json.loads(raw)
    except Exception:
        return {}, None
    for iface in data.get("interfaces", []):
        if iface.get("name") == "lo":
            continue
        created = (iface.get("created") or {}).get("date") or {}
        since = ("%04d-%02d-%02d" % (created.get("year", 0), created.get("month", 0),
                                     created.get("day", 0))) if created else None
        days = {}
        for d in (iface.get("traffic") or {}).get("day", []):
            dd = d.get("date") or {}
            key = "%04d-%02d-%02d" % (dd.get("year", 0), dd.get("month", 0),
                                      dd.get("day", 0))
            days[key] = int(d.get("rx", 0)) + int(d.get("tx", 0))
        return days, since
    return {}, None


def main():
    ap = argparse.ArgumentParser(description="S-UI + vnstat 流量聚合")
    ap.add_argument("--db", default="/usr/local/s-ui/db/s-ui.db")
    ap.add_argument("--quota-gb", type=float, default=1000)
    ap.add_argument("--reset-day", type=int, default=25)
    ap.add_argument("--tz-offset", type=float, default=8,
                    help="看板使用者的时区偏移小时数，按它切天")
    args = ap.parse_args()

    tz = timezone(timedelta(hours=args.tz_offset))
    now = datetime.now(tz)
    start = cycle_start(now, args.reset_day)

    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    daily = sui_daily(con, int(start.timestamp()), int(args.tz_offset))
    clients = [{"name": n, "up": int(u or 0), "down": int(d or 0)}
               for n, u, d in con.execute(
                   "SELECT name, up, down FROM clients WHERE enable = 1")]
    con.close()

    vn_days, vn_since = vnstat_daily()

    # 校准倍数：拿 vnstat 真正记满的整天和同期 S-UI 用量比。装 vnstat 当天
    # 只记了半天，不算数；今天还没过完，也不算数。
    today = now.strftime("%Y-%m-%d")
    full = [d for d in vn_days
            if (vn_since is None or d > vn_since) and d < today and d in daily]
    ratio = FALLBACK_RATIO
    measured = False
    if full:
        vn_sum = sum(vn_days[d] for d in full)
        sui_sum = sum(v["up"] + v["down"] for d in full for v in daily[d].values())
        if sui_sum > 0:
            ratio = vn_sum / sui_sum
            measured = True

    # 周期账单量：vnstat 记满的日子用实测值，其余的按 S-UI × 倍数估算。
    # 随着 vnstat 攒够整天，估算的部分会自己缩小到零。
    billing = 0.0
    estimated_days = 0
    for date, per_user in sorted(daily.items()):
        if date in full:
            billing += vn_days[date]
        else:
            billing += sum(v["up"] + v["down"] for v in per_user.values()) * ratio
            estimated_days += 1

    # 日均取最近 7 个完整天，据此推还能撑多久
    recent = sorted(d for d in daily if d < today)[-7:]
    per_day = 0.0
    if recent:
        used = sum(v["up"] + v["down"] for d in recent for v in daily[d].values()) * ratio
        per_day = used / len(recent)
    quota_bytes = int(args.quota_gb * 1024 ** 3)
    days_left = int((quota_bytes - billing) / per_day) if per_day > 0 else None

    nxt = (start.replace(day=1) + timedelta(days=32)).replace(
        day=args.reset_day, hour=0, minute=0, second=0, microsecond=0)

    print(json.dumps({
        "cycle": {"start": start.strftime("%Y-%m-%d"),
                  "reset": nxt.strftime("%Y-%m-%d"),
                  "days_in": (now - start).days + 1},
        "billing": {"bytes": int(billing), "quota_bytes": quota_bytes,
                    "pct": round(billing / quota_bytes * 100, 1),
                    "ratio": round(ratio, 3), "ratio_measured": measured,
                    "estimated_days": estimated_days,
                    "vnstat_since": vn_since},
        "forecast": {"per_day_bytes": int(per_day), "days_left": days_left,
                     "sample_days": len(recent)},
        "daily": daily,
        "clients": clients,
        "collected": now.isoformat(timespec="seconds"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
