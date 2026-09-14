"""额度口径和凭据边界回归；全部使用虚构数据，不读取本机 Key。"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_plans import CodingPlanPoller, NoRedirect, parse_quota, timestamp, window


class QuotaTests(unittest.TestCase):
    def test_minimax_percent_wins_over_zero_counts(self):
        result = parse_quota("minimax", {"base_resp": {"status_code": 0}, "model_remains": [{
            "model_name": "general", "current_interval_total_count": 0,
            "current_interval_usage_count": 0, "current_interval_remaining_percent": 37,
            "current_weekly_remaining_percent": 0,
        }]})
        self.assertEqual([w["remaining_pct"] for w in result["windows"]], [37, 0])

    def test_minimax_video_does_not_become_coding_quota(self):
        with self.assertRaises(ValueError):
            parse_quota("minimax", {"base_resp": {"status_code": 0}, "model_remains": [{
                "model_name": "video", "current_interval_remaining_percent": 100}]})

    def test_kimi_weekly_only_does_not_invent_short_window(self):
        result = parse_quota("kimi", {"usage": {"limit": "100", "remaining": "72"}})
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["windows"][0]["remaining_pct"], 72)
        self.assertEqual(result["windows"][0]["label"], "每周")

    def test_kimi_labels_actual_duration(self):
        result = parse_quota("kimi", {"limits": [{
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "used": "100"}}]})
        self.assertEqual(result["windows"][0]["label"], "5 小时")
        self.assertEqual(result["windows"][0]["remaining_pct"], 0)

    def test_glm_credit_limits_unordered(self):
        result = parse_quota("glm", {"success": True, "code": 200, "data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 1000,
             "currentValue": 115, "remaining": 884, "percentage": 11},
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 100,
             "currentValue": 100, "remaining": 0, "percentage": 100}]}})
        self.assertEqual([w["label"] for w in result["windows"]], ["每周", "5 小时"])
        self.assertEqual([w["remaining_pct"] for w in result["windows"]], [88.4, 0])

    def test_deepseek_currency_and_zero_balance(self):
        result = parse_quota("deepseek", {"is_available": False, "balance_infos": [
            {"currency": "CNY", "total_balance": "0", "granted_balance": "0", "topped_up_balance": "0"},
            {"currency": "USD", "total_balance": "1.23"}]})
        self.assertEqual([b["total"] for b in result["balances"]], [0, 1.23])
        self.assertFalse(result["available"])

    def test_missing_and_nonfinite_are_unknown(self):
        for value in (None, "NaN", "Infinity", True):
            self.assertIsNone(window("未知", percent=value)["remaining_pct"])
        with self.assertRaises(ValueError):
            parse_quota("kimi", {"usage": {"limit": "0"}})

    def test_timezone_and_milliseconds(self):
        self.assertEqual(timestamp("2026-01-01T08:00:00+08:00"), timestamp("2026-01-01T00:00:00Z"))
        self.assertEqual(timestamp(1767225600000), timestamp("2026-01-01T00:00:00Z"))

    def test_redirect_never_forwards_key(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.poller = CodingPlanPoller(path=Path('/unused-synthetic-test-path'))
        self.accounts = [{"id": "0", "provider": "kimi", "label": "测试账号", "api_key": "synthetic-test-key"}]
        self.good = {"windows": [{"label": "每周", "remaining_pct": 75}]}

    def poll(self, result=None, error=None):
        with patch.object(self.poller, '_accounts', return_value=self.accounts), patch(
                'coding_plans.query_account', return_value=result or self.good, side_effect=error):
            self.poller.poll_once()
        return self.poller.get()

    def test_error_retains_previous_result_without_leaking_key(self):
        old = self.poll()
        failed = self.poll(error=RuntimeError('response echoed synthetic-test-key'))
        a = failed['accounts'][0]
        self.assertEqual(a['status'], 'stale')
        self.assertEqual(a['updated_at'], old['accounts'][0]['updated_at'])
        self.assertNotIn('synthetic-test-key', json.dumps(failed))

    def test_changed_key_must_not_inherit_previous_balance(self):
        self.poll()
        self.accounts[0]['api_key'] = 'another-synthetic-key'
        a = self.poll(error=RuntimeError())['accounts'][0]
        self.assertEqual(a['status'], 'error')
        self.assertIsNone(a['updated_at'])

    def test_empty_key_clears_previous_result(self):
        self.poll()
        self.accounts[0]['api_key'] = ''
        self.assertEqual(self.poll()['accounts'][0]['status'], 'unconfigured')

    def test_removed_account_is_removed(self):
        self.poll()
        self.accounts.clear()
        self.assertEqual(self.poll()['accounts'], [])

    def test_invalid_configuration_clears_snapshot(self):
        self.poll()
        with patch.object(self.poller, '_accounts', side_effect=ValueError('配置无效')):
            self.poller.poll_once()
        self.assertEqual(self.poller.get()['accounts'], [])
        self.assertFalse(self.poller.get()['refreshing'])

    def test_snapshot_is_not_mutable_by_caller(self):
        result = self.poll()
        result['accounts'].clear()
        self.assertEqual(len(self.poller.get()['accounts']), 1)


if __name__ == '__main__':
    unittest.main()
