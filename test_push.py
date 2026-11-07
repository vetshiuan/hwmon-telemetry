# -*- coding: utf-8 -*-
"""push_monitor 状态机与判定逻辑测试。"""

import unittest

import push_monitor as pm


class TestCompare(unittest.TestCase):
    def test_thresholds(self):
        self.assertTrue(pm.compare(108, ">=", 108))
        self.assertFalse(pm.compare(107.9, ">=", 108))
        self.assertTrue(pm.compare(11.5, "<", 11.6))
        self.assertFalse(pm.compare(11.6, "<", 11.6))
        self.assertTrue(pm.compare(12.9, ">", 12.8))

    def test_none_value(self):
        self.assertFalse(pm.compare(None, ">=", 1))


class TestDig(unittest.TestCase):
    def test_nested(self):
        snap = {"gpu": {"hot": 96.4, "pin_volt": 12.01}, "cpu": {"temp": 44}}
        self.assertEqual(pm.dig(snap, "gpu.hot"), 96.4)
        self.assertEqual(pm.dig(snap, "cpu.temp"), 44)
        self.assertIsNone(pm.dig(snap, "gpu.missing"))
        self.assertIsNone(pm.dig(snap, "nope.deep.path"))


class TestMetricState(unittest.TestCase):
    def test_needs_confirm_samples(self):
        st = pm.MetricState()
        self.assertIsNone(st.update(True, 1000.0, 2, 3, 10))
        self.assertFalse(st.active)
        self.assertEqual(st.update(True, 1030.0, 2, 3, 10), "enter")
        self.assertTrue(st.active)

    def test_no_duplicate_enter_while_active(self):
        st = pm.MetricState()
        st.update(True, 1000.0, 2, 3, 10)
        st.update(True, 1030.0, 2, 3, 10)
        self.assertIsNone(st.update(True, 1060.0, 2, 3, 10))
        self.assertTrue(st.active)

    def test_recover_needs_samples(self):
        st = pm.MetricState()
        st.update(True, 1000.0, 2, 3, 10)
        st.update(True, 1030.0, 2, 3, 10)
        self.assertIsNone(st.update(False, 1060.0, 2, 3, 10))
        self.assertIsNone(st.update(False, 1090.0, 2, 3, 10))
        self.assertEqual(st.update(False, 1120.0, 2, 3, 10), "recover")
        self.assertFalse(st.active)

    def test_single_spike_does_not_fire(self):
        st = pm.MetricState()
        st.update(True, 1000.0, 2, 3, 10)
        st.update(False, 1030.0, 2, 3, 10)
        self.assertFalse(st.active)
        self.assertIsNone(st.update(True, 1060.0, 2, 3, 10))
        self.assertFalse(st.active)

    def test_flap_guard_suppresses_reenter(self):
        st = pm.MetricState()
        st.update(True, 1000.0, 2, 3, 10)
        st.update(True, 1030.0, 2, 3, 10)
        for t in (1060.0, 1090.0, 1120.0):
            st.update(False, t, 2, 3, 10)
        self.assertFalse(st.active)
        st.update(True, 1200.0, 2, 3, 10)
        self.assertIsNone(st.update(True, 1230.0, 2, 3, 10))
        self.assertTrue(st.active)

    def test_repeat_due(self):
        st = pm.MetricState()
        st.update(True, 1000.0, 2, 3, 10)
        st.update(True, 1030.0, 2, 3, 10)
        self.assertFalse(st.due_repeat(1060.0, 300))
        self.assertTrue(st.due_repeat(1331.0, 300))


class TestNotifyDryRun(unittest.TestCase):
    def test_missing_webhook_is_safe(self):
        self.assertFalse(pm.wecom_send("", "标题", "内容"))
        self.assertFalse(pm.pushplus("", "标题", "内容"))

    def test_notify_before_init(self):
        saved = pm._NOTIFIER
        pm._NOTIFIER = None
        try:
            self.assertFalse(pm.notify("标题", "内容"))
        finally:
            pm._NOTIFIER = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
