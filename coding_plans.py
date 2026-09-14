"""独立的个人 Coding Plan 额度采集；只返回白名单字段，凭据不进入页面或日志。"""

import copy
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


ENDPOINTS = {
    "minimax": "https://www.minimaxi.com/v1/token_plan/remains",
    "kimi": "https://api.kimi.com/coding/v1/usages",
    "glm": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    "deepseek": "https://api.deepseek.com/user/balance",
}


def number(value):
    """缺失、布尔值和非有限数字不能误显示成剩余零。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        if isinstance(value, (int, float)):
            return value / 1000 if value > 1e11 else value
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def window(label, remaining=None, total=None, used=None, percent=None, reset=None):
    remaining, total, used, percent = map(number, (remaining, total, used, percent))
    if percent is None and total is not None and total > 0:
        if remaining is None and used is not None:
            remaining = total - used
        if remaining is not None:
            percent = remaining / total * 100
    return {"label": label,
            "remaining_pct": max(0, min(100, percent)) if percent is not None else None,
            "remaining": remaining, "total": total, "reset_at": timestamp(reset)}


def parse_quota(provider, payload):
    """按真实响应解析窗口，不把平台积分或百分比伪装成 token。"""
    windows = []
    if provider == "minimax":
        if (payload.get("base_resp") or {}).get("status_code") != 0:
            raise ValueError("服务返回业务错误，请检查订阅 Key 和套餐状态")
        rows = payload.get("model_remains") or []
        row = next((r for r in rows if r.get("model_name") == "general"), None)
        if row is None:
            row = next((r for r in rows if str(r.get("model_name", "")).lower().startswith("minimax")), None)
        if row is None:
            raise ValueError("接口未提供编程套餐额度")
        for label, prefix, reset in (("5 小时", "current_interval", "end_time"),
                                      ("每周", "current_weekly", "weekly_end_time")):
            # 当前套餐的计数可能都是 0，remaining_percent 才是有效的余额比例。
            windows.append(window(label, total=row.get(prefix + "_total_count"),
                                  used=row.get(prefix + "_usage_count"),
                                  percent=row.get(prefix + "_remaining_percent"), reset=row.get(reset)))
    elif provider == "kimi":
        for item in payload.get("limits") or []:
            detail = item.get("detail") or item
            w = item.get("window") or {}
            duration = number(w.get("duration"))
            unit = w.get("timeUnit", "")
            if duration == 300 and unit == "TIME_UNIT_MINUTE":
                label = "5 小时"
            else:
                units = {"TIME_UNIT_MINUTE": "分钟", "TIME_UNIT_HOUR": "小时",
                         "TIME_UNIT_DAY": "天", "TIME_UNIT_WEEK": "周"}
                label = "%g %s" % (duration, units.get(unit, "单位")) if duration else "其他窗口"
            windows.append(window(label, detail.get("remaining"), detail.get("limit"),
                                  detail.get("used"), reset=detail.get("resetTime")))
        summary = payload.get("usage")
        if isinstance(summary, dict):
            windows.append(window("每周", summary.get("remaining"), summary.get("limit"),
                                  summary.get("used"), reset=summary.get("resetTime")))
    elif provider == "glm":
        if payload.get("success") is not True or payload.get("code") != 200:
            raise ValueError("服务返回业务错误，请检查编程套餐 Key")
        for item in (payload.get("data") or {}).get("limits") or []:
            kind = item.get("type")
            if kind not in ("CREDIT_LIMIT", "TOKENS_LIMIT", "TIME_LIMIT"):
                continue
            # 平台 unit=3 为小时，unit=6 为周；不依据数组顺序猜测窗口。
            units = {3: "小时", 5: "月", 6: "周"}
            unit, count = item.get("unit"), number(item.get("number"))
            label = ("每周" if unit == 6 and count == 1 else
                     "%g %s" % (count, units[unit]) if count and unit in units else "套餐窗口")
            if kind == "TIME_LIMIT":
                label = "MCP · " + label
            pct = number(item.get("percentage"))
            # 有精确余额和总量时优先算比例，避免已用百分比取整产生偏差。
            total, remaining = number(item.get("usage")), number(item.get("remaining"))
            fallback = 100 - pct if pct is not None and not (total and remaining is not None) else None
            windows.append(window(label, remaining, total, item.get("currentValue"),
                                  fallback, item.get("nextResetTime")))
    elif provider == "deepseek":
        balances = []
        for item in payload.get("balance_infos") or []:
            total = number(item.get("total_balance"))
            if total is None or item.get("currency") not in ("CNY", "USD"):
                continue
            balances.append({"currency": item["currency"], "total": total,
                             "topped_up": number(item.get("topped_up_balance")),
                             "granted": number(item.get("granted_balance"))})
        if not balances:
            raise ValueError("接口未提供有效余额")
        return {"balances": balances, "available": payload.get("is_available"), "windows": []}
    if not windows or not any(w["remaining_pct"] is not None for w in windows):
        raise ValueError("接口未提供有效额度，暂时无法判断剩余量")
    return {"windows": windows}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """固定官方地址，不将认证头跟随重定向转交其他主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_account(account):
    provider, key = account["provider"], account["api_key"]
    request = urllib.request.Request(ENDPOINTS[provider], headers={
        "Authorization": key if provider == "glm" else "Bearer " + key,
        "Accept": "application/json", "User-Agent": "Token-Dashboard/1.0",
    })
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        payload = json.loads(response.read(262144))
    return parse_quota(provider, payload)


class CodingPlanPoller:
    """与个人 CLI 额度隔离，支持多账号、热读配置和并发查询，快照只放内存。"""

    def __init__(self, path=None, interval=180):
        self.path = Path(path) if path else Path(__file__).resolve().parent / "coding-plans.local.json"
        self.interval = interval
        self.lock = threading.Lock()
        self.poll_lock = threading.Lock()
        self.wake = threading.Event()
        self.last_started = 0
        self.identities = {}
        self.latest = {"accounts": [], "refreshing": False, "checked_at": None, "error": ""}

    def get(self):
        with self.lock:
            return copy.deepcopy(self.latest)

    def request_refresh(self):
        self.wake.set()

    def _accounts(self):
        if not self.path.exists():
            return []
        try:
            cfg = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = cfg["accounts"]
            if not isinstance(accounts, list):
                raise ValueError()
            result = []
            for i, item in enumerate(accounts):
                provider = item["provider"]
                key = item.get("api_key", "").strip()
                if provider not in ENDPOINTS or len(key) > 16384:
                    raise ValueError()
                result.append({"id": str(i), "provider": provider, "api_key": key,
                               "label": str(item.get("label") or provider)[:80]})
            return result
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            raise ValueError("本机额度配置无法读取，请检查 JSON 格式及 provider") from None

    def poll_once(self):
        if not self.poll_lock.acquire(blocking=False):
            return
        try:
            self.last_started = time.monotonic()
            with self.lock:
                self.latest["refreshing"] = True
            try:
                accounts = self._accounts()
            except ValueError as exc:
                with self.lock:
                    self.latest.update(accounts=[], error=str(exc), checked_at=time.time())
                    self.identities = {}
                return
            old = {a["id"]: a for a in self.get()["accounts"]}
            identities = {a["id"]: hashlib.sha256((a["provider"] + "\0" + a["api_key"]).encode()).hexdigest()
                          for a in accounts}

            def fetch(account):
                ident = account["id"]
                row = {k: account[k] for k in ("id", "provider", "label")}
                row.update(status="unconfigured", windows=[], error="", updated_at=None)
                if not account["api_key"]:
                    return row
                try:
                    data = query_account(account)
                    row.update(data, status="ok", updated_at=time.time())
                except Exception as exc:
                    # 原始响应、异常字符串可能带认证信息，只映射固定错误信息。
                    if isinstance(exc, urllib.error.HTTPError):
                        error = ("认证失败，请检查 Key" if exc.code in (401, 403) else
                                 "查询过于频繁，稍后自动重试" if exc.code == 429 else
                                 "平台查询失败（HTTP %d）" % exc.code)
                    elif isinstance(exc, (TimeoutError, urllib.error.URLError)):
                        error = "连接超时或网络不可用"
                    else:
                        error = "未取得有效额度，请检查套餐状态或稍后重试"
                    previous = old.get(ident)
                    if previous and previous.get("updated_at") and self.identities.get(ident) == identities[ident]:
                        row = {**previous, "label": account["label"], "status": "stale", "error": error}
                    else:
                        row.update(status="error", error=error)
                return row

            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(fetch, accounts))
            with self.lock:
                self.identities = identities
                self.latest.update(accounts=rows, checked_at=time.time(), error="")
        finally:
            with self.lock:
                self.latest["refreshing"] = False
            self.poll_lock.release()

    def loop(self):
        while True:
            self.wake.clear()
            self.poll_once()
            self.wake.wait(self.interval)
            # 手动刷新也有最短间隔，连续点击不会同时打上游。
            delay = 15 - (time.monotonic() - self.last_started)
            if delay > 0:
                time.sleep(delay)
