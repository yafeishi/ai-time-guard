"""Absence transitions open exactly one screen and preserve manual dismissal."""
import unittest
from unittest.mock import Mock, patch
from away_screen import AwayScreenSession


class AwayScreenTests(unittest.TestCase):
    def setUp(self):
        self.session = AwayScreenSession('/tmp/app')
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = patch('away_screen.subprocess.Popen', return_value=self.process).start()
        self.addCleanup(patch.stopall)

    def test_one_launch_per_absence_even_after_manual_exit(self):
        self.session.sync(True)
        self.session.sync(True)
        self.process.poll.return_value = 0
        self.session.sync(True)
        self.assertEqual(self.popen.call_count, 1)
        self.session.sync(False)
        self.session.sync(True)
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(self.popen.call_args.args[0][-1], '--away')

    def test_return_or_disabled_feature_closes_screen(self):
        self.session.sync(True)
        self.session.sync(False)
        self.process.terminate.assert_called_once()
        self.assertFalse(self.session.in_absence)

    def test_existing_focus_is_not_covered_during_same_absence(self):
        self.session.sync(True, blocked=True)
        self.session.sync(True, blocked=False)
        self.popen.assert_not_called()
        self.session.sync(False)
        self.session.sync(True)
        self.popen.assert_called_once()

    def test_launch_failure_does_not_crash_or_repeat_each_second(self):
        self.popen.side_effect = OSError('test failure')
        self.session.sync(True)
        self.session.sync(True)
        self.assertEqual(self.session.error, 'test failure')
        self.popen.assert_called_once()
