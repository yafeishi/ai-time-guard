import importlib
import sys
import types
import unittest
from unittest.mock import patch


def _load_module():
    fake_rumps = types.SimpleNamespace(
        App=type("App", (), {}),
        MenuItem=type("MenuItem", (), {}),
        Timer=type("Timer", (), {}),
        separator=object(),
        notification=lambda **kwargs: None,
        alert=lambda **kwargs: 0,
        quit_application=lambda: None,
    )
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda *args, **kwargs: [],
        NoSuchProcess=Exception,
        AccessDenied=Exception,
        ZombieProcess=Exception,
        Process=lambda pid: types.SimpleNamespace(
            cpu_percent=lambda interval=None: 0.0,
            memory_info=lambda: types.SimpleNamespace(rss=0),
            memory_percent=lambda: 0.0,
        ),
    )

    with patch.dict(sys.modules, {"rumps": fake_rumps, "psutil": fake_psutil}):
        if "ai_time_guard" in sys.modules:
            del sys.modules["ai_time_guard"]
        return importlib.import_module("ai_time_guard")


class FakeAppState:
    def __init__(self, today_key):
        self.config = {"daily_limit_minutes": 240}
        self.history = {
            today_key: {"total": 10.0, "tools": {"Claude Code": 10.0}},
        }
        self.today_seconds = 120.4
        self.today_tools = {"Claude Code": 110.25, "Codex": 10.15}
        self.is_ai_active = True
        self.active_detail = "Claude Code（终端前台）"
        self.is_monitoring = True


class ReportHandlerBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def tearDown(self):
        self.mod._app_instance = None

    def test_build_daily_uses_realtime_data_for_today(self):
        today_key = str(self.mod.date.today())
        self.mod._app_instance = FakeAppState(today_key)

        result = self.mod.ReportHandler.build_daily(object(), today_key)

        self.assertEqual(result["date"], today_key)
        self.assertEqual(result["total_seconds"], round(self.mod._app_instance.today_seconds, 1))
        self.assertEqual(
            result["tools"],
            {k: round(v, 1) for k, v in self.mod._app_instance.today_tools.items()},
        )
        self.assertEqual(result["limit_minutes"], 240)

    def test_build_daily_supports_legacy_numeric_history(self):
        target_date = "2026-02-20"
        self.mod._app_instance = None

        with patch.object(self.mod, "load_config", return_value={"daily_limit_minutes": 180}):
            with patch.object(self.mod, "load_history", return_value={target_date: 3661.29}):
                result = self.mod.ReportHandler.build_daily(object(), target_date)

        self.assertEqual(result["date"], target_date)
        self.assertEqual(result["total_seconds"], 3661.3)
        self.assertEqual(result["tools"], {})
        self.assertEqual(result["limit_minutes"], 180)

    def test_build_summary_aggregates_days_and_realtime_today(self):
        today = self.mod.date.today()
        d0 = str(today)
        d1 = str(today - self.mod.timedelta(days=1))
        d2 = str(today - self.mod.timedelta(days=2))

        app = FakeAppState(d0)
        app.history[d1] = {"total": 300.0, "tools": {"Codex": 300.0}}
        app.history[d2] = {"total": 30.0, "tools": {"Claude Code": 30.0}}
        app.today_seconds = 90.0
        app.today_tools = {"Claude Code": 40.0, "Codex": 50.0}
        self.mod._app_instance = app

        result = self.mod.ReportHandler.build_summary(object(), 3)

        self.assertEqual(result["days"], 3)
        self.assertEqual(result["start_date"], d2)
        self.assertEqual(result["end_date"], d0)
        self.assertEqual(result["total_seconds"], 420.0)
        self.assertEqual(result["daily_avg_seconds"], 140.0)
        self.assertEqual(result["active_days"], 2)
        self.assertEqual(result["max_day"]["date"], d1)
        self.assertEqual(result["max_day"]["seconds"], 300.0)
        self.assertEqual(result["tools"]["Codex"], 350.0)
        self.assertEqual(result["tools"]["Claude Code"], 70.0)
        self.assertEqual(len(result["daily"]), 3)
        self.assertEqual(result["daily"][-1]["total"], 90.0)

    def test_build_monthly_summary_groups_by_month(self):
        today = self.mod.date.today()
        month_start = self.mod.shift_months(today, 0)
        prev_month_start = self.mod.shift_months(today, -1)

        app = FakeAppState(str(today))
        app.history = {
            str(month_start): {"total": 120.0, "tools": {"Codex": 120.0}},
            str(prev_month_start): {"total": 240.0, "tools": {"Claude Code": 240.0}},
        }
        app.today_seconds = 60.0
        app.today_tools = {"Codex": 60.0}
        self.mod._app_instance = app

        result = self.mod.ReportHandler.build_monthly_summary(object(), 2)

        self.assertEqual(result["months"], 2)
        self.assertEqual(len(result["monthly"]), 2)
        self.assertEqual(result["monthly"][-1]["month"], f"{today.year}-{today.month:02d}")
        self.assertGreaterEqual(result["total_seconds"], 300.0)
        self.assertIn("Codex", result["tools"])

    def test_build_yearly_summary_groups_by_year(self):
        today = self.mod.date.today()
        current_year_day = str(self.mod.date(today.year, 1, 1))
        prev_year_day = str(self.mod.date(today.year - 1, 1, 1))

        app = FakeAppState(str(today))
        app.history = {
            current_year_day: {"total": 300.0, "tools": {"Codex": 300.0}},
            prev_year_day: {"total": 600.0, "tools": {"Claude Code": 600.0}},
        }
        app.today_seconds = 0.0
        app.today_tools = {}
        self.mod._app_instance = app

        result = self.mod.ReportHandler.build_yearly_summary(object(), 2)

        self.assertEqual(result["years"], 2)
        self.assertEqual(len(result["yearly"]), 2)
        self.assertEqual(result["start_year"], str(today.year - 1))
        self.assertEqual(result["end_year"], str(today.year))
        self.assertGreaterEqual(result["total_seconds"], 900.0)

    def test_infer_tool_name_supports_workbuddy(self):
        self.assertEqual(
            self.mod.infer_tool_name("WorkBuddy（前台）"),
            "WorkBuddy",
        )

    def test_build_summary_aggregates_workbuddy(self):
        today = self.mod.date.today()
        d0 = str(today)
        d1 = str(today - self.mod.timedelta(days=1))

        app = FakeAppState(d0)
        app.history[d1] = {"total": 120.0, "tools": {"WorkBuddy": 120.0}}
        app.today_seconds = 60.0
        app.today_tools = {"WorkBuddy": 60.0}
        self.mod._app_instance = app

        result = self.mod.ReportHandler.build_summary(object(), 2)

        self.assertEqual(result["tools"]["WorkBuddy"], 180.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
