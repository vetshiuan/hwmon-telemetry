# -*- coding: utf-8 -*-
"""后端关键回归测试：python -m unittest -v test_agent"""
import importlib.util
import os
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("hwmon_agent", os.path.join(HERE, "agent.py"))
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class TestGpuZColumns(unittest.TestCase):
    def test_header_mapping_survives_column_reorder(self):
        header = [
            "Date", "GPU Voltage [V]", "16-Pin Voltage [V]", "TDP [%]",
            "16-Pin Power [W]", "Board Power Draw [W]", "Memory Junction [°C]",
            "Hot Spot [°C]", "GPU Temperature [°C]",
        ]
        columns = agent.resolve_gpuz_columns(header)
        self.assertEqual(columns["gpu_core"], 8)
        self.assertEqual(columns["gpu_hot"], 7)
        self.assertEqual(columns["pin_volt"], 2)
        self.assertEqual(columns["gpu_volt"], 1)

    def test_missing_optional_column_is_none(self):
        columns = agent.resolve_gpuz_columns(["Date", "GPU Temperature [°C]"])
        self.assertEqual(columns["gpu_core"], 1)
        self.assertIsNone(columns["pin_volt"])

    def test_memory_temperature_alias(self):
        header = ["Date", "GPU Temperature [°C]", "Memory Temperature [°C]"]
        columns = agent.resolve_gpuz_columns(header)
        self.assertEqual(columns["gpu_mem"], 2)


class TestAlerts(unittest.TestCase):
    def test_threshold_requires_three_samples_but_official_gpu_limit_is_immediate(self):
        tracker = {}
        snap = {"gpu": {"core": 80, "hot": 90, "pin_volt": 11.8}, "cpu": {"temp": 85, "core_max": 85}}
        self.assertEqual(agent.evaluate_metric_alerts(snap, tracker), [])
        self.assertEqual(agent.evaluate_metric_alerts(snap, tracker), [])
        third = {a["key"] for a in agent.evaluate_metric_alerts(snap, tracker)}
        self.assertIn("gpu.hot", third)
        self.assertIn("cpu.temp", third)

        snap["gpu"]["core"] = 90
        alerts = agent.evaluate_metric_alerts(snap, tracker)
        core = next(a for a in alerts if a["key"] == "gpu.core")
        self.assertEqual(core["severity"], "bad")

    def test_high_power_and_load_are_neutral(self):
        tracker = {}
        snap = {
            "gpu": {"core": 60, "hot": 70, "power": 560, "load": 100, "tdp": 99},
            "cpu": {"temp": 60, "power": 219, "load": 100},
        }
        self.assertEqual(agent.evaluate_metric_alerts(snap, tracker), [])

    def test_pin_overvoltage_is_not_reported_as_normal(self):
        tracker = {}
        snap = {"gpu": {"pin_volt": 12.7}, "cpu": {}}
        for _ in range(3):
            alerts = agent.evaluate_metric_alerts(snap, tracker)
        pin = next(a for a in alerts if a["key"] == "gpu.pin_volt")
        self.assertEqual(pin["severity"], "bad")

    def test_hysteresis_prevents_flicker(self):
        tracker = {}
        hot = {"gpu": {"core": 92}, "cpu": {}}
        alerts = agent.evaluate_metric_alerts(hot, tracker)
        self.assertEqual(next(a for a in alerts if a["key"] == "gpu.core")["severity"], "bad")
        cooling = {"gpu": {"core": 88}, "cpu": {}}
        alerts = agent.evaluate_metric_alerts(cooling, tracker)
        self.assertEqual(next(a for a in alerts if a["key"] == "gpu.core")["severity"], "bad")
        for _ in range(3):
            alerts = agent.evaluate_metric_alerts({"gpu": {"core": 87}, "cpu": {}}, tracker)
        self.assertEqual(next(a for a in alerts if a["key"] == "gpu.core")["severity"], "warn")


class TestSnapshotHealth(unittest.TestCase):
    def test_stale_snapshot_becomes_bad(self):
        now = int(time.time() * 1000)
        snap = {
            "ts": now - 20_000, "alerts": [],
            "health": {"status": "ok", "label": "运行正常", "bad": 0, "warn": 0},
        }
        result = agent.with_freshness(snap)
        self.assertEqual(result["health"]["status"], "bad")
        self.assertTrue(any(a["key"] == "snapshot.stale" for a in result["alerts"]))

    def test_resource_alert_and_capacity_message(self):
        snap = {
            "mem": {"pct": 95},
            "disk": [{"name": "disk-a", "used_pct": 85}, {"name": "disk-b", "used_pct": 40}],
        }
        alerts = agent.resource_alerts(snap)
        self.assertEqual({a["key"] for a in alerts}, {"mem.pct", "disk.0"})
        self.assertEqual(next(a for a in alerts if a["key"] == "mem.pct")["severity"], "bad")


class TestDailyDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = agent.DB_PATH
        agent.DB_PATH = os.path.join(self.temp.name, "test.db")
        agent.db_init()

    def tearDown(self):
        agent.DB_PATH = self.old_db
        self.temp.cleanup()

    def _snap(self, ts, cpu_temp, gpu_hot, pin):
        return {
            "ts": ts,
            "cpu": {"temp": cpu_temp, "core_max": cpu_temp + 2, "power": 30, "load": 20},
            "gpu": {"core": gpu_hot - 10, "hot": gpu_hot, "mem": 40, "power": 100, "load": 30, "pin_volt": pin},
            "mem": {"pct": 40},
        }

    def test_today_summary_and_long_history(self):
        now = int(time.time() * 1000)
        agent.store_sample(self._snap(now - 4000, 40, 60, 11.9))
        agent.store_sample(self._snap(now, 90, 100, 11.7))
        summary = agent.daily_summary()
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["metrics"]["cpu_temp"]["avg"], 65.0)
        self.assertEqual(summary["metrics"]["pin_volt"]["min"], 11.7)
        self.assertTrue(summary["storage"]["ok"])
        history = agent.query_history("today")
        self.assertGreaterEqual(len(history["t"]), 1)
        self.assertEqual(history["_meta"]["window"], "today")


if __name__ == "__main__":
    unittest.main()
