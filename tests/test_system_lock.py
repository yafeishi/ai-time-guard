"""Authentication state is authoritative; human presence cannot unlock a session."""
import unittest
from unittest.mock import Mock
from system_lock import MacSession, SecureAwayLock


class SessionClassificationTests(unittest.TestCase):
    def test_only_own_authenticated_console_session_is_unlocked(self):
        own={'kCGSSessionUserIDKey':503,'kCGSSessionOnConsoleKey':True,'kCGSessionLoginDoneKey':True}
        self.assertEqual(MacSession.classify(own,503),'unlocked')
        self.assertEqual(MacSession.classify(own,501),'unknown')
        for key in ('CGSSessionScreenIsLocked','kCGSessionScreenIsLocked'):
            self.assertEqual(MacSession.classify({**own,key:True},503),'locked')
        self.assertEqual(MacSession.classify({**own,'kCGSSessionOnConsoleKey':False},503),'locked')
        self.assertEqual(MacSession.classify(None,503),'unknown')
        self.assertEqual(MacSession.classify({},503),'unknown')
        self.assertEqual(MacSession.classify({'kCGSSessionUserIDKey':503},503),'unknown')


class SecureLockTests(unittest.TestCase):
    def setUp(self):
        self.native=Mock(error='')
        self.native.state.return_value='unlocked'
        self.lock=SecureAwayLock(self.native)
        self.lock.poll(now=100)

    def test_confirmed_absence_requests_real_lock_once(self):
        self.assertFalse(self.lock.sync(False,True,now=101))
        self.lock.sync(False,True,now=102)
        self.native.lock.assert_called_once()
        self.assertTrue(self.lock.pending)

    def test_someone_approaching_does_not_release_pending_or_locked_state(self):
        self.lock.sync(False,True,now=101)
        self.assertFalse(self.lock.sync(True,True,now=102))
        self.native.state.return_value='locked';self.lock.poll(now=103)
        self.assertFalse(self.lock.sync(True,True,now=104))
        self.assertTrue(self.lock.blocking)

    def test_resume_requires_unlock_and_confirmed_presence(self):
        self.lock.sync(False,True,now=101)
        self.native.state.return_value='locked';self.lock.poll(now=102)
        self.native.state.return_value='unlocked';self.lock.poll(now=110)
        self.assertFalse(self.lock.sync(None,True,now=111))
        self.assertFalse(self.lock.sync(False,True,now=112))
        self.native.lock.assert_called_once()  # do not immediately re-lock after login
        self.assertTrue(self.lock.sync(True,True,now=113))
        self.assertEqual(self.lock.last_away_seconds,8)
        self.lock.sync(False,True,now=130)
        self.assertEqual(self.native.lock.call_count,2)

    def test_camera_failure_or_disabling_option_does_not_release_pending_lock(self):
        self.lock.sync(False,True,now=101)
        self.assertFalse(self.lock.sync(None,False,now=102))
        self.assertFalse(self.lock.sync(True,False,now=103))
        self.native.state.return_value='locked';self.lock.poll(now=104)
        self.native.state.return_value='unlocked';self.lock.poll(now=105)
        self.assertTrue(self.lock.sync(True,False,now=106))

    def test_session_api_failure_is_not_treated_as_unlocked(self):
        self.native.state.return_value='unknown';self.lock.poll(now=101)
        self.assertFalse(self.lock.sync(True,True,now=102))
        self.native.lock.assert_not_called()

    def test_failed_lock_request_keeps_timers_paused_and_reports_error(self):
        self.native.lock.side_effect=RuntimeError('lock failed')
        self.assertFalse(self.lock.sync(False,True,now=101))
        self.assertIn('lock failed',self.lock.snapshot()['error'])
        self.assertFalse(self.lock.sync(True,True,now=102))

    def test_lock_request_must_be_confirmed_by_system(self):
        self.lock.sync(False,True,now=101)
        self.lock.poll(now=107)
        self.assertIn('尚未确认系统锁定',self.lock.error)
        self.assertFalse(self.lock.sync(True,True,now=108))

    def test_disabled_feature_and_off_duty_do_not_trigger_lock(self):
        self.lock.sync(False,False,now=101)
        self.lock.sync(False,True,allowed=False,now=102)
        self.native.lock.assert_not_called()

    def test_restart_while_locked_still_requires_authentication(self):
        self.native.state.return_value='locked'
        restored=SecureAwayLock(self.native);restored.poll(now=100)
        self.assertFalse(restored.sync(True,True,now=101))
        self.native.state.return_value='unlocked';restored.poll(now=105)
        self.assertTrue(restored.sync(True,True,now=106))

    def test_manual_system_lock_also_pauses_events(self):
        self.native.state.return_value='locked';self.lock.poll(now=101)
        self.assertFalse(self.lock.sync(None,False,now=102))
