"""Menu settings and live blink statistics agree with the page configuration."""
import types
import unittest
from datetime import date, timedelta
from unittest.mock import patch
from test_countdown_standup import _load_module


class MenuItem:
    def __init__(self, title, callback=None):
        self.title, self.callback, self.children = title, callback, []
    def add(self, item):
        self.children.append(item)


class CameraMenuTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()
        self.mod.rumps.MenuItem = MenuItem
        self.app = self.mod.AITimeGuardApp.__new__(self.mod.AITimeGuardApp)
        self.app.config = dict(self.mod.DEFAULT_CONFIG, camera_blink_enabled=True)
        today = date.today()
        self.state = dict(status='running', quality='tracking', blink_rate=14,
                          today_blinks=8, today_observed_seconds=90, presence='present',
                          history={str(today): {'blinks': 5},
                                   str(today-timedelta(days=6)): {'blinks': 3},
                                   str(today-timedelta(days=7)): {'blinks': 100}})
        self.app.camera = types.SimpleNamespace(snapshot=lambda: self.state, recalibrate=lambda: None)
        self.app.build_camera_menu()

    def test_live_rate_and_week_total_do_not_double_count_today(self):
        self.assertIn('14 次/分', self.app.camera_menu.title)
        self.assertIn('达到目标', self.app.camera_rate_item.title)
        self.assertIn('8 次', self.app.camera_today_item.title)
        self.assertIn('11 次', self.app.camera_week_item.title)
        self.assertIn('在座', self.app.camera_presence_item.title)

    def test_settings_checkmark_follows_configuration(self):
        self.app.toggle_camera_setting(self.app.camera_blink_item)
        self.assertFalse(self.app.config['camera_blink_enabled'])
        self.app.update_camera_menu(self.state)
        self.assertFalse(self.app.camera_blink_item.state)
        self.assertNotIn('14 次/分', self.app.camera_menu.title)
        item = self.app.camera_target_items[-1]
        self.app.set_camera_value(item)
        self.assertEqual(self.app.config['camera_blink_target'], 20)
        self.assertTrue(item.state)
        self.assertEqual(sum(bool(i.state) for i in self.app.camera_target_items), 1)

    def test_failed_camera_does_not_show_stale_rate(self):
        self.state['status'] = 'error'
        self.app.update_camera_menu(self.state)
        self.assertIn('摄像头不可用', self.app.camera_menu.title)
        self.assertNotIn('达到目标', self.app.camera_rate_item.title)

    def test_blink_reminder_uses_shared_modal_and_acknowledgement(self):
        from unittest.mock import Mock
        self.app.camera.take_blink_reminder = Mock(return_value={'blink_rate': 6, 'target': 12})
        self.app.camera.blink_reminder_dismissed = Mock()
        self.app.show_modal_alert = Mock()
        self.app.show_blink_reminder()
        self.app.show_modal_alert.assert_called_once()
        args, kwargs = self.app.show_modal_alert.call_args
        self.assertEqual(args[0], '眨眼提醒')
        self.assertIn('6 次', args[1])
        self.assertEqual(kwargs['ok'], '知道了')
        self.app.camera.blink_reminder_dismissed.assert_called_once()
        self.app.focus_until = self.mod.time.time() + 60
        self.app.camera.take_blink_reminder.return_value = None
        self.app.show_blink_reminder()
        self.app.camera.take_blink_reminder.assert_called_with(allowed=False)
        self.app.show_modal_alert.assert_called_once()
