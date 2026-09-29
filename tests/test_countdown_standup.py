"""Tests for the 3-minute countdown and 45-minute stand-up reminder logic."""
import importlib
import sys
import time
import types
import unittest
from unittest.mock import patch


def _load_module():
    # rumps.alert 是模态框的唯一出口，记录下来供断言
    alert_calls = []
    fake_rumps = types.SimpleNamespace(
        App=type("App", (), {}),
        MenuItem=type("MenuItem", (), {}),
        Timer=type("Timer", (), {}),
        separator=object(),
        notification=lambda **kwargs: None,
        alert=lambda **kwargs: alert_calls.append(kwargs) or 1,
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
    # AppKit 必须一起 mock：测试进程里真正导入 AppKit 会加载 objc 扩展，
    # 二次 import 会抛 "Reload of objc._objc detected"。
    # 模块在 import 时会调 NSApplication.sharedApplication()，这里给它一个空壳。
    fake_appkit = types.SimpleNamespace(
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: None),
        NSApplication=types.SimpleNamespace(
            sharedApplication=lambda: types.SimpleNamespace(
                setActivationPolicy_=lambda *_: None,
            )
        ),
        NSApplicationActivationPolicyAccessory=0,
        NSApplicationActivationPolicyRegular=1,
        NSImage=None,
        NSColor=types.SimpleNamespace(),
        NSAttributedString=None,
        NSMutableAttributedString=None,
        NSFont=None,
        NSForegroundColorAttributeName="FG",
        NSFontAttributeName="FNT",
    )
    with patch.dict(
        sys.modules,
        {"rumps": fake_rumps, "psutil": fake_psutil, "AppKit": fake_appkit},
    ):
        if "ai_time_guard" in sys.modules:
            del sys.modules["ai_time_guard"]
        mod = importlib.import_module("ai_time_guard")
    # 纯文本渲染路径不受 AppKit 影响，强制走它，便于断言字符串
    mod.HAS_APPKIT = False
    mod.RUMPS_ALERT_CALLS = alert_calls
    return mod


class _TimerStub:
    """Captures start/stop calls so we can drive countdown_tick manually."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.callback = None
        self.interval = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class _NotificationRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message):
        self.calls.append((title, message))


class _AppStub:
    """Minimal stand-in for AITimeGuardApp with just the attributes the new
    countdown / stand-up methods touch."""

    def __init__(self, mod):
        cfg = mod.DEFAULT_CONFIG.copy()
        cfg["stand_up_last_reminder_time"] = 0  # ensure first reminder can fire on demand
        self.config = cfg
        self.title = ""
        self.is_monitoring = True
        self.today_seconds = 0
        self.is_ai_active = False
        # countdown
        self.countdown_duration_seconds = 180
        self.countdown_duration_minutes = 3
        self.countdown_remaining_seconds = 0
        self.countdown_state = "idle"
        self.countdown_timer = None
        self.countdown_action_item = types.SimpleNamespace(title="开始 03:00 倒计时")
        self.countdown_menu = types.SimpleNamespace(title="🕒 3 分钟倒计时")
        # stand-up
        self.stand_up_interval_minutes = 45
        self.stand_up_enabled = False
        self.stand_up_last_reminder_time = time.time()
        self.stand_up_title_mode = "final5"
        self.stand_up_menu = types.SimpleNamespace(title="🧍 站立提醒")
        self.stand_up_toggle_item = types.SimpleNamespace(title="启用站立提醒", state=False)
        # notifications (replaced per-test)
        self.send_notification = _NotificationRecorder()
        # 模态框：真实实现 + 记录器。横幅通道已失效，用户可见的提醒只有这条路。
        self.alerts = mod.RUMPS_ALERT_CALLS
        self.alerts.clear()
        self.show_modal_alert = types.MethodType(
            mod.AITimeGuardApp.show_modal_alert, self
        )
        self.update_stand_up_menu_title = types.MethodType(
            mod.AITimeGuardApp.update_stand_up_menu_title, self
        )
        self.update_title = types.MethodType(mod.AITimeGuardApp.update_title, self)
        self.refresh_countdown_labels = types.MethodType(
            mod.AITimeGuardApp.refresh_countdown_labels, self
        )


class CountdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.app = _AppStub(self.mod)
        # Inject our timer stub factory
        self.timer_instances = []
        timer_stub = _TimerStub

        def _make_timer(cb, interval):
            t = timer_stub()
            t.callback = cb
            t.interval = interval
            self.timer_instances.append(t)
            return t

        # Bind unbound methods from the real class onto the stub
        cls = self.mod.AITimeGuardApp
        self.app.start_countdown = types.MethodType(cls.start_countdown, self.app)
        self.app.cancel_countdown = types.MethodType(cls.cancel_countdown, self.app)
        self.app.countdown_tick = types.MethodType(cls.countdown_tick, self.app)
        self.app.countdown_finished = types.MethodType(cls.countdown_finished, self.app)
        self.app.update_title = types.MethodType(cls.update_title, self.app)
        # Replace Timer construction for countdown only
        self._orig_timer_factory = None

        def fake_rumps_timer(cb, interval):
            t = _TimerStub()
            t.callback = cb
            t.interval = interval
            self.timer_instances.append(t)
            return t

        self._fake_rumps_timer = fake_rumps_timer

    def _patch_timer(self):
        return patch.object(self.mod.rumps, "Timer", self._fake_rumps_timer)

    def test_start_runs_timer_and_updates_menu(self):
        with self._patch_timer():
            self.app.start_countdown()
        self.assertEqual(self.app.countdown_state, "running")
        self.assertEqual(self.app.countdown_remaining_seconds, 180)
        self.assertEqual(len(self.timer_instances), 1)
        self.assertEqual(self.timer_instances[0].started, 1)
        self.assertEqual(self.timer_instances[0].interval, 1)
        self.assertEqual(self.app.countdown_action_item.title, "取消倒计时")
        self.assertIn("03:00", self.app.countdown_menu.title)

    def test_double_start_is_a_noop(self):
        with self._patch_timer():
            self.app.start_countdown()
            self.app.start_countdown()  # should be ignored
        self.assertEqual(len(self.timer_instances), 1, "second start should not create a new timer")

    def test_tick_decrements_until_finished(self):
        with self._patch_timer():
            self.app.start_countdown()
        # Drive ticks manually, fast-forward to finish
        for _ in range(180):
            self.app.countdown_tick(None)
        self.assertEqual(self.app.countdown_state, "finished")
        self.assertEqual(self.timer_instances[0].stopped, 1)
        self.assertEqual(self.app.countdown_action_item.title, "重新开始 03:00 倒计时")
        # A modal alert should have fired once at finish
        titles = [a["title"] for a in self.app.alerts]
        self.assertIn("🕒 3 分钟倒计时结束", titles)

    def test_tick_partial_updates_menu_title(self):
        with self._patch_timer():
            self.app.start_countdown()
        for _ in range(5):
            self.app.countdown_tick(None)
        self.assertEqual(self.app.countdown_remaining_seconds, 175)
        self.assertIn("02:55", self.app.countdown_menu.title)

    def test_cancel_resets_to_idle(self):
        with self._patch_timer():
            self.app.start_countdown()
        self.app.cancel_countdown()
        self.assertEqual(self.app.countdown_state, "idle")
        self.assertEqual(self.app.countdown_remaining_seconds, 0)
        self.assertEqual(self.timer_instances[0].stopped, 1)
        self.assertEqual(self.app.countdown_action_item.title, "开始 03:00 倒计时")

    def test_update_title_uses_countdown_when_running(self):
        with self._patch_timer():
            self.app.start_countdown()
        for _ in range(30):
            self.app.countdown_tick(None)
        # Should be 02:30 left
        self.assertTrue(self.app.title.startswith("⏱02:30"))

    def test_update_title_falls_back_to_idle_when_countdown_not_running(self):
        self.app.update_title()
        self.assertTrue(self.app.title.startswith("IDLE"), self.app.title)


class StandUpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.app = _AppStub(self.mod)
        cls = self.mod.AITimeGuardApp
        self.app.update_title = types.MethodType(cls.update_title, self.app)
        self.app.fire_stand_up_reminder = types.MethodType(cls.fire_stand_up_reminder, self.app)
        self.app.update_stand_up_menu_title = types.MethodType(
            cls.update_stand_up_menu_title, self.app
        )
        self.app.toggle_stand_up = types.MethodType(cls.toggle_stand_up, self.app)
        self.app.set_stand_up_interval = types.MethodType(cls.set_stand_up_interval, self.app)
        # interval submenu mock for set_stand_up_interval
        fake_items = []
        for mins in [30, 45, 60, 90, 120]:
            it = types.SimpleNamespace(_mins=mins, state=False)
            fake_items.append(it)
        self.app.stand_up_interval_submenu = types.SimpleNamespace(values=lambda: fake_items)

    def test_force_fires_immediately(self):
        self.app.stand_up_last_reminder_time = time.time()
        self.app.fire_stand_up_reminder(force=True)
        self.assertEqual(len(self.app.alerts), 1)
        self.assertEqual(self.app.alerts[0]["title"], "🧍 站起来活动一下")

    def test_skips_when_below_interval(self):
        # Just fired, immediate check should not fire again
        self.app.stand_up_last_reminder_time = time.time()
        self.app.fire_stand_up_reminder()
        self.assertEqual(self.app.alerts, [])

    def test_fires_when_interval_elapsed(self):
        # Pretend the last reminder was 46 minutes ago (> default 45)
        self.app.stand_up_last_reminder_time = time.time() - 46 * 60
        self.app.fire_stand_up_reminder()
        self.assertEqual(len(self.app.alerts), 1)

    def test_toggle_enables_and_resets_last_time(self):
        sender = types.SimpleNamespace(state=False)
        self.app.stand_up_enabled = False
        self.app.stand_up_last_reminder_time = 0
        self.app.toggle_stand_up(sender)
        self.assertTrue(self.app.stand_up_enabled)
        self.assertTrue(sender.state)
        self.assertGreater(self.app.stand_up_last_reminder_time, 0)
        # 启用后应显示倒计时
        self.assertIn("下次", self.app.stand_up_menu.title)

    def test_toggle_off_keeps_last_time(self):
        sender = types.SimpleNamespace(state=True)
        self.app.stand_up_enabled = True
        before = self.app.stand_up_last_reminder_time
        self.app.toggle_stand_up(sender)
        self.assertFalse(self.app.stand_up_enabled)
        self.assertFalse(sender.state)
        # Disabling should NOT reset last reminder (so enabling later respects history)
        self.assertEqual(self.app.stand_up_last_reminder_time, before)

    def test_set_interval_resets_last_time_and_state(self):
        # Find the 60-minute item
        item60 = next(i for i in self.app.stand_up_interval_submenu.values() if i._mins == 60)
        item60.state = False
        before_last = self.app.stand_up_last_reminder_time
        self.app.set_stand_up_interval(item60)
        self.assertEqual(self.app.stand_up_interval_minutes, 60)
        self.assertGreater(self.app.stand_up_last_reminder_time, before_last)
        # 默认是禁用状态，标题显示新的间隔（无倒计时）
        self.assertIn("60 分钟", self.app.stand_up_menu.title)


class DefaultConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_new_keys_present_with_expected_defaults(self):
        self.assertEqual(self.mod.DEFAULT_CONFIG["stand_up_interval_minutes"], 45)
        self.assertFalse(self.mod.DEFAULT_CONFIG["stand_up_enabled"])
        self.assertEqual(self.mod.DEFAULT_CONFIG["stand_up_last_reminder_time"], 0)
        self.assertEqual(self.mod.DEFAULT_CONFIG["countdown_duration_seconds"], 180)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class WeeklyChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_app(self, history):
        app = _AppStub(self.mod)
        # Provide 8 stats items (header + 7 days)
        header = types.SimpleNamespace(title="init")
        days = [types.SimpleNamespace(title=f"day{i}") for i in range(7)]
        app.stats_header_item = header
        app.stats_day_items = days
        # Seed history with realistic data spanning 7 days
        from datetime import timedelta
        today = self.mod.date.today()
        seeded = {}
        # Make day -6 = 1h, day -5 = 0, today = real-time seconds
        seeded[str(today - self.mod.timedelta(days=6))] = {"total": 3600.0, "tools": {}}
        seeded[str(today - self.mod.timedelta(days=5))] = {"total": 0, "tools": {}}
        seeded[str(today - self.mod.timedelta(days=4))] = {"total": 1800.0, "tools": {}}  # 30 min
        seeded[str(today - self.mod.timedelta(days=3))] = {"total": 600.0, "tools": {}}   # 10 min
        seeded[str(today - self.mod.timedelta(days=2))] = {"total": 60.0, "tools": {}}    # 1 min (▏)
        seeded[str(today - self.mod.timedelta(days=1))] = {"total": 7200.0, "tools": {}}  # 2h
        app.history = seeded
        app.today_seconds = 300.0  # 5 min today
        return app, header, days

    def test_update_weekly_stats_populates_header_and_days(self):
        app, header, days = self._make_app(None)
        cls = self.mod.AITimeGuardApp
        app.update_weekly_stats = types.MethodType(cls.update_weekly_stats, app)
        app.update_weekly_stats()
        # Header: label + total
        self.assertIn("近 7 天", header.title)
        self.assertIn("3:46:00", header.title)  # 13560s = 3h 46m
        # Last day is "今天" with bar + format
        self.assertIn("今天", days[6].title)
        self.assertIn("5:00", days[6].title)
        # Day 5 (the -1 day) was 2h → 4 格实心（30min/格）
        self.assertIn("昨天", days[5].title)
        self.assertIn("████", days[5].title)
        self.assertIn("2:00:00", days[5].title)
        # 不足一格用 ▏ 占位（1min / 10min 都 < 30min）
        self.assertIn("▏", days[4].title)
        # 0 天不画实心块，只剩轨道，且时长显示为破折号
        self.assertNotIn("█", days[1].title)
        self.assertIn("░", days[1].title)   # 轨道仍然画满，保证行宽一致
        self.assertIn("—", days[1].title)

    def test_weekly_rows_have_constant_width(self):
        """每行总宽度必须一致，否则菜单里时长数字会参差不齐"""
        app, _, days = self._make_app(None)
        cls = self.mod.AITimeGuardApp
        app.update_weekly_stats = types.MethodType(cls.update_weekly_stats, app)
        app.update_weekly_stats()
        widths = {self.mod._display_width(d.title) for d in days}
        self.assertEqual(len(widths), 1, f"行宽不一致: {widths}")

    def test_limit_marker_present_in_under_limit_rows(self):
        """未超限的行应在轨道上画出日限额刻度线 │"""
        app, _, days = self._make_app(None)
        cls = self.mod.AITimeGuardApp
        app.update_weekly_stats = types.MethodType(cls.update_weekly_stats, app)
        app.update_weekly_stats()
        # 5min / 1min / 0 都远低于 3h 限额 → 应该有刻度线
        self.assertIn("│", days[6].title)   # 今天 5min
        self.assertIn("│", days[1].title)   # 0 天
        # 2h 也 < 3h，仍应可见刻度
        self.assertIn("│", days[5].title)

    def test_cjk_day_label_is_aligned(self):
        """今天/昨天 是双宽字符，不能用 ljust 对齐"""
        app, _, days = self._make_app(None)
        cls = self.mod.AITimeGuardApp
        app.update_weekly_stats = types.MethodType(cls.update_weekly_stats, app)
        app.update_weekly_stats()
        # 日期标签占 5 个显示列，所以 "  今天" 之前只有 1 个空格
        self.assertIn("   今天", days[6].title)
        self.assertIn("  09/2", days[0].title)

    def test_update_weekly_stats_uses_realtime_today(self):
        """当 history 里今日数据滞后于 self.today_seconds，应以实时为准"""
        app, _, days = self._make_app(None)
        cls = self.mod.AITimeGuardApp
        app.update_weekly_stats = types.MethodType(cls.update_weekly_stats, app)
        # history has no entry for today, but app.today_seconds = 300
        app.update_weekly_stats()
        # Today row should reflect 5:00
        self.assertIn("5:00", days[6].title)


class StandUpFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.app = _AppStub(self.mod)
        cls = self.mod.AITimeGuardApp
        # Stub rumps.Timer so feedback helper can construct one in tests
        self._timer_stubs = []
        def fake_timer(cb, interval):
            t = _TimerStub()
            t.callback = cb
            t.interval = interval
            self._timer_stubs.append(t)
            return t
        self._timer_patch = patch.object(self.mod.rumps, "Timer", fake_timer)
        self._timer_patch.start()
        # Bind the new methods
        for name in [
            "update_title",
            "update_stand_up_menu_title",
            "_show_stand_up_feedback",
            "_clear_stand_up_feedback",
            "test_stand_up_reminder",
            "fire_stand_up_reminder",
            "toggle_stand_up",
        ]:
            setattr(self.app, name, types.MethodType(getattr(cls, name), self.app))

    def tearDown(self):
        self._timer_patch.stop()

    def test_feedback_overrides_countdown_title(self):
        # Enable stand-up so default title would be "下次 ..."
        self.app.stand_up_enabled = True
        self.app.stand_up_last_reminder_time = time.time()  # just fired
        self.app.update_stand_up_menu_title()
        self.assertIn("下次", self.app.stand_up_menu.title)
        # Trigger feedback
        self.app._show_stand_up_feedback("已发送 ✅", duration=5.0)
        self.assertIn("已发送 ✅", self.app.stand_up_menu.title)
        # Clear
        self.app._clear_stand_up_feedback(None)
        self.assertNotIn("已发送", self.app.stand_up_menu.title)
        self.assertIn("下次", self.app.stand_up_menu.title)

    def test_feedback_expires_automatically(self):
        # Expired feedback must not stick
        self.app.stand_up_enabled = True
        self.app._stand_up_feedback_text = "x"
        self.app._stand_up_feedback_expire = time.time() - 1  # already expired
        self.app.update_stand_up_menu_title()
        self.assertNotIn("x", self.app.stand_up_menu.title)

    def test_test_stand_up_reminder_invokes_feedback(self):
        # Replace notification sink and timer constructor
        sender = types.SimpleNamespace(state=False)
        self.app.stand_up_enabled = False
        self.app.test_stand_up_reminder(sender)
        self.assertTrue(self.app.stand_up_enabled is False)
        # Feedback text should be set with "已发送 ✅"
        self.assertIn("已发送 ✅", self.app._stand_up_feedback_text)




class StandUpTitleModeTests(unittest.TestCase):
    """菜单栏倒计时显示方式：final5 vs always"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.app = _AppStub(self.mod)
        cls = self.mod.AITimeGuardApp
        for name in ("update_title", "set_stand_up_title_mode"):
            setattr(self.app, name, types.MethodType(getattr(cls, name), self.app))
        # 启用 + 启用后 1 分钟
        self.app.stand_up_enabled = True
        self.app.stand_up_last_reminder_time = time.time() - 60
        self.app.stand_up_title_mode = "final5"

    def test_final5_hides_prefix_when_remaining_over_5_min(self):
        """final5 + 剩余 44 分 → 不应该显示前缀"""
        self.app.update_title()
        self.assertFalse(self.app.title.startswith("🧍"))
        self.assertTrue(self.app.title.startswith("IDLE"))

    def test_final5_shows_prefix_when_remaining_under_5_min(self):
        """final5 + 剩余 3 分 → 应该显示前缀"""
        # last=4 分钟前 → remaining = 45 - 4 = 41 ... 不对，重设
        # 想要 remaining = 3min → last = 42min ago
        self.app.stand_up_last_reminder_time = time.time() - (45 - 3) * 60
        self.app.update_title()
        self.assertTrue(self.app.title.startswith("🧍"))
        self.assertTrue(self.app.title.startswith("🧍02:") or self.app.title.startswith("🧍03:"))

    def test_always_shows_prefix_regardless_of_remaining(self):
        """always + 剩余 44 分 → 仍显示前缀"""
        self.app.stand_up_title_mode = "always"
        self.app.update_title()
        self.assertTrue(self.app.title.startswith("🧍"))
        self.assertTrue(self.app.title.startswith("🧍44:") or self.app.title.startswith("🧍43:"))

    def test_set_stand_up_title_mode_persists_and_updates_title(self):
        """set_stand_up_title_mode 改模式 + 立刻刷新标题"""
        # 先禁用前缀
        self.app.stand_up_title_mode = "final5"
        self.app.update_title()
        before = self.app.title
        self.assertFalse(before.startswith("🧍"))
        # 切到 always
        sender = types.SimpleNamespace(_mode="always", state=False)
        fake_items = [
            types.SimpleNamespace(_mode="final5", state=True),
            types.SimpleNamespace(_mode="always", state=False),
        ]
        self.app.stand_up_title_mode_submenu = types.SimpleNamespace(values=lambda: fake_items)
        self.app.set_stand_up_title_mode(sender)
        self.assertEqual(self.app.stand_up_title_mode, "always")
        self.assertTrue(self.app.title.startswith("🧍"))
        # 子菜单勾选状态被翻转
        self.assertFalse(fake_items[0].state)
        self.assertTrue(fake_items[1].state)


class ModalAlertVisibilityTests(unittest.TestCase):
    """提醒必须以模态弹窗呈现，不能只发系统横幅。

    背景：rumps 走的是已废弃的 NSUserNotification，而本项目跑在没有 bundle ID
    的 Homebrew Python 进程里，系统没有对应授权记录，横幅会被静默丢弃——
    通知能进通知中心（deliveredNotifications 可见），但用户永远看不到。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.app = _AppStub(self.mod)
        cls = self.mod.AITimeGuardApp
        for name in (
            "update_title",
            "countdown_finished",
            "fire_stand_up_reminder",
            "test_stand_up_reminder",
            "_show_stand_up_feedback",
            "_clear_stand_up_feedback",
        ):
            setattr(self.app, name, types.MethodType(getattr(cls, name), self.app))
        # _AppStub 已把真实 show_modal_alert 绑好，alerts 记录的是 rumps.alert 的实参
        # 菜单反馈会起一个自动清除的 Timer，桩掉它避免真的去 start
        self._timer_patch = patch.object(
            self.mod.rumps, "Timer", lambda cb, interval: _TimerStub()
        )
        self._timer_patch.start()

    def tearDown(self):
        self._timer_patch.stop()

    def test_countdown_finished_shows_modal_alert(self):
        self.app.countdown_finished()
        self.assertEqual(len(self.app.alerts), 1, f"倒计时结束应弹一次模态框: {self.app.alerts}")
        self.assertIn("倒计时结束", self.app.alerts[0]["title"])

    def test_stand_up_fire_shows_modal_alert(self):
        self.app.stand_up_last_reminder_time = time.time() - 46 * 60
        self.app.fire_stand_up_reminder()
        self.assertEqual(len(self.app.alerts), 1, f"站立提醒应弹一次模态框: {self.app.alerts}")
        self.assertIn("站起来", self.app.alerts[0]["title"])

    def test_manual_stand_up_test_does_not_double_alert(self):
        """手动测试会走 fire_stand_up_reminder，本身已弹模态框，不能再弹第二个"""
        self.app.test_stand_up_reminder(None)
        self.assertEqual(len(self.app.alerts), 1, f"手动测试只应弹一次: {self.app.alerts}")

    def test_modal_alert_has_dismissible_button(self):
        """模态框必须有确认按钮文案，否则用户不知道要点击才能关闭"""
        self.app.countdown_finished()
        self.assertTrue(self.app.alerts[0].get("ok"), "模态框必须提供 ok 按钮")

    def test_alert_does_not_depend_on_broken_banner_channel(self):
        """提醒必须来自 rumps.alert，而不是已经失效的横幅通道"""
        self.app.countdown_finished()
        self.assertEqual(
            self.app.send_notification.calls, [],
            "倒计时结束不应再依赖 send_notification 横幅通道",
        )


class CountdownDurationTests(unittest.TestCase):
    """倒计时时长可配置：1/2/3/5/10/15/20/30 分钟，记住上次选择。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.app = _AppStub(self.mod)
        cls = self.mod.AITimeGuardApp
        for name in (
            "update_title",
            "start_countdown",
            "cancel_countdown",
            "countdown_tick",
            "countdown_finished",
            "set_countdown_duration",
        ):
            setattr(self.app, name, types.MethodType(getattr(cls, name), self.app))
        self.app.countdown_duration_seconds = 180
        self.app.countdown_duration_minutes = 3
        # 时长选择子菜单
        self.items = [
            types.SimpleNamespace(_mins=m, state=(m == 3))
            for m in self.mod.COUNTDOWN_DURATION_CHOICES
        ]
        self.app.countdown_duration_submenu = types.SimpleNamespace(
            values=lambda: self.items
        )
        self._timer_patch = patch.object(
            self.mod.rumps, "Timer", lambda cb, interval: _TimerStub()
        )
        self._timer_patch.start()
        self._save_patch = patch.object(self.mod, "save_config", lambda c: None)
        self._save_patch.start()

    def tearDown(self):
        self._timer_patch.stop()
        self._save_patch.stop()

    def test_choices_match_request(self):
        self.assertEqual(
            tuple(self.mod.COUNTDOWN_DURATION_CHOICES), (1, 2, 3, 5, 10, 15, 20, 30)
        )

    def test_default_is_three_minutes(self):
        self.assertEqual(self.mod.DEFAULT_CONFIG["countdown_duration_seconds"], 180)

    def test_set_duration_persists_to_config(self):
        item5 = next(i for i in self.items if i._mins == 5)
        self.app.set_countdown_duration(item5)
        self.assertEqual(self.app.countdown_duration_minutes, 5)
        self.assertEqual(self.app.countdown_duration_seconds, 300)
        self.assertEqual(self.app.config["countdown_duration_seconds"], 300)

    def test_set_duration_moves_checkmark(self):
        item10 = next(i for i in self.items if i._mins == 10)
        self.app.set_countdown_duration(item10)
        self.assertTrue(item10.state)
        self.assertFalse(next(i for i in self.items if i._mins == 3).state)

    def test_menu_labels_follow_selected_duration(self):
        item20 = next(i for i in self.items if i._mins == 20)
        self.app.set_countdown_duration(item20)
        self.assertIn("20 分钟倒计时", self.app.countdown_menu.title)
        self.assertEqual(self.app.countdown_action_item.title, "开始 20:00 倒计时")

    def test_cancel_restores_label_with_new_duration(self):
        item5 = next(i for i in self.items if i._mins == 5)
        self.app.set_countdown_duration(item5)
        self.app.start_countdown()
        self.app.cancel_countdown()
        self.assertEqual(self.app.countdown_action_item.title, "开始 05:00 倒计时")
        self.assertEqual(self.app.countdown_menu.title, "🕒 5 分钟倒计时")

    def test_finished_title_follows_selected_duration(self):
        item1 = next(i for i in self.items if i._mins == 1)
        self.app.set_countdown_duration(item1)
        self.app.countdown_finished()
        self.assertEqual(self.app.countdown_menu.title, "🕒 1 分钟倒计时（已完成）")

    def test_start_uses_selected_duration(self):
        item15 = next(i for i in self.items if i._mins == 15)
        self.app.set_countdown_duration(item15)
        self.app.start_countdown()
        self.assertEqual(self.app.countdown_remaining_seconds, 900)

    def test_changing_duration_does_not_disturb_running_countdown(self):
        """改时长只影响下次开始，不打断正在跑的倒计时"""
        self.app.start_countdown()
        item30 = next(i for i in self.items if i._mins == 30)
        self.app.set_countdown_duration(item30)
        self.assertEqual(self.app.countdown_state, "running")
        self.assertEqual(self.app.countdown_remaining_seconds, 180)


