import tempfile
import unittest
from pathlib import Path
from camera_monitor import CameraSignals, CameraService, offline_camera_command
from event_reminders import EventReminders


class SignalTests(unittest.TestCase):
    def calibrate(self, signals):
        for index in range(61):
            signals.feed(100+index*.1, 1, .32)

    def test_counts_closed_then_open_and_ignores_long_closure(self):
        signals=CameraSignals();self.calibrate(signals)
        signals.feed(106.1,1,.1)
        self.assertFalse(signals.feed(106.2,1,.1))
        self.assertTrue(signals.feed(106.3,1,.32))
        self.assertEqual(signals.total_blinks,1)
        signals.feed(106.4,1,.1)
        signals.feed(107.3,1,.1)
        self.assertFalse(signals.feed(107.4,1,.32))
        self.assertEqual(signals.total_blinks,1)

    def test_single_closed_frame_at_30fps_is_counted(self):
        signals=CameraSignals()
        for index in range(181):signals.feed(100+index/30,1,.32)
        signals.feed(106.04,1,.18)
        self.assertTrue(signals.feed(106.074,1,.32))
        self.assertEqual(signals.snapshot(106.074)['session_blinks'],1)

    def test_separate_eye_baselines_allow_asymmetry_but_reject_winks(self):
        signals=CameraSignals()
        for index in range(181):signals.feed(100+index/30,1,.32,eyes=[.24,.40])
        signals.feed(106.04,1,.13,eyes=[.10,.40])
        signals.feed(106.08,1,.32,eyes=[.24,.40])
        self.assertEqual(signals.total_blinks,0)
        signals.feed(106.12,1,.18,eyes=[.12,.22])
        self.assertTrue(signals.feed(106.16,1,.32,eyes=[.24,.40]))
        self.assertEqual(signals.total_blinks,1)

    def test_one_missing_eye_frame_during_closure_does_not_erase_blink(self):
        signals=CameraSignals();self.calibrate(signals)
        signals.feed(106.1,1,.16)
        signals.feed(106.14,1,None)
        self.assertTrue(signals.feed(106.2,1,.32))
        self.assertEqual(signals.total_blinks,1)

    def test_missing_landmarks_from_open_eyes_do_not_invent_a_blink(self):
        signals=CameraSignals();self.calibrate(signals)
        signals.feed(106.1,1,None)
        self.assertFalse(signals.feed(106.2,1,.32))
        self.assertEqual(signals.total_blinks,0)

    def test_small_open_eye_noise_is_not_counted(self):
        signals=CameraSignals();self.calibrate(signals)
        for index in range(100):
            signals.feed(106.1+index*.04,1,.28 if index%2 else .32)
        self.assertEqual(signals.total_blinks,0)

    def test_relaxed_reopening_does_not_merge_consecutive_blinks(self):
        signals=CameraSignals();self.calibrate(signals)
        # Normal open eyes are narrower than during initial calibration.
        for blink_index in range(6):
            start=106.1+blink_index*.65
            for frame in range(4):signals.feed(start+frame*.033,1,.08)
            for frame in range(4,19):signals.feed(start+frame*.033,1,.25)
        self.assertEqual(signals.total_blinks,6)
        self.assertEqual(signals.eye_phase,'open')

    def test_brief_face_loss_does_not_pause_or_count_blink(self):
        signals=CameraSignals();self.calibrate(signals)
        signals.feed(106.1,1,.1)
        signals.feed(106.2,0,None)
        signals.feed(106.3,1,.32)
        self.assertEqual(signals.total_blinks,0)
        self.assertNotEqual(signals.snapshot(106.3)['presence'],'away')

    def test_away_and_return_debounce_and_camera_stale(self):
        signals=CameraSignals()
        for second in range(14):signals.feed(100+second,0,None,False)
        self.assertEqual(signals.snapshot(113)['presence'],'away')
        signals.feed(114,1,.3,True)
        self.assertEqual(signals.snapshot(114)['presence'],'away')
        signals.feed(115,1,.3,True);signals.feed(116,1,.3,True)
        self.assertEqual(signals.snapshot(116)['presence'],'present')
        self.assertEqual(signals.snapshot(120)['presence'],'unknown')

    def test_visible_person_without_eyes_is_not_away(self):
        signals=CameraSignals()
        for second in range(15):signals.feed(100+second,0,None,True)
        self.assertEqual(signals.snapshot(114)['presence'],'present')
        self.assertIsNone(signals.snapshot(114)['blink_rate'])

    def test_rate_requires_sufficient_valid_observation(self):
        signals=CameraSignals();self.calibrate(signals)
        self.assertIsNone(signals.snapshot(106)['blink_rate'])
        for index in range(1,551):signals.feed(106+index*.1,1,.32)
        state=signals.snapshot(161)
        self.assertEqual(state['blink_rate'],0)
        self.assertTrue(state['below_target'])
        signals.feed(161.1,2,None)
        self.assertIsNone(signals.snapshot(161.1)['blink_rate'])

    def test_calibration_reset_keeps_aggregate_counts(self):
        signals=CameraSignals();signals.total_blinks=7
        self.calibrate(signals);signals.reset()
        self.assertIsNone(signals.baseline)
        self.assertEqual(signals.total_blinks,7)

    def test_low_frame_rate_is_not_reported_as_insufficient_blinking(self):
        signals=CameraSignals()
        for index in range(400):signals.feed(100+index*.2,1,.32)
        state=signals.snapshot(179.8)
        self.assertEqual(state['quality'],'low_fps')
        self.assertIsNone(state['blink_rate'])
        self.assertFalse(state['below_target'])

    def test_denied_or_failed_camera_is_unknown_not_away(self):
        with tempfile.TemporaryDirectory() as directory:
            service=CameraService(directory,directory)
            service._receive({'status':'denied','error':'permission denied'})
            state=service.snapshot()
            self.assertEqual(state['presence'],'unknown')
            self.assertEqual(state['status'],'denied')
            self.assertIsNone(state['blink_rate'])


class BlinkReminderTests(unittest.TestCase):
    def test_modal_reminder_requires_current_valid_measurement_and_cooldown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            service = CameraService(directory, directory)
            service.settings = {'camera_blink_enabled': True}
            state = dict(status='running', presence='present', below_target=True, blink_rate=6, target=12)
            with patch.object(service, 'snapshot', return_value=state), patch('camera_monitor.time.time', return_value=1000):
                self.assertIsNone(service.take_blink_reminder(allowed=False))
                self.assertEqual(service.take_blink_reminder()['blink_rate'], 6)
                self.assertIsNone(service.take_blink_reminder())
            with patch.object(service, 'snapshot', return_value=state), patch('camera_monitor.time.time', return_value=1400):
                service.blink_reminder_dismissed()
                self.assertIsNone(service.take_blink_reminder())
            with patch.object(service, 'snapshot', return_value=state), patch('camera_monitor.time.time', return_value=1700):
                state['presence'] = 'away'
                self.assertIsNone(service.take_blink_reminder())
                state['presence'] = 'present'
                state['below_target'] = False
                self.assertIsNone(service.take_blink_reminder())
                state['below_target'] = True
                self.assertIsNotNone(service.take_blink_reminder())


class CameraIsolationTests(unittest.TestCase):
    def test_camera_sandbox_denies_network_connections(self):
        import subprocess
        import sys
        result = subprocess.run(offline_camera_command(sys.executable, '-c',
            "import socket; socket.create_connection(('127.0.0.1', 19527), timeout=2)"),
            capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Operation not permitted', result.stderr)

    def test_stalled_capture_reports_error_and_unknown_presence(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            service = CameraService(directory, directory)
            service.status = 'running'
            service.streaming_since = 100
            with patch('camera_monitor.time.time', return_value=109):
                state = service.snapshot()
            self.assertEqual(state['status'], 'error')
            self.assertEqual(state['presence'], 'unknown')


class PresenceTimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.engine=EventReminders(Path(self.tmp.name)/'events.json',{})
        self.id=self.engine.snapshot()['events'][0]['id']
        self.engine.command('start',self.id,now=100)

    def event(self):return next(e for e in self.engine.snapshot()['events'] if e['id']==self.id)

    def test_away_freezes_and_return_restores_remaining(self):
        self.engine.sync_presence(False,now=200)
        self.assertEqual(self.event()['state'],'presence_paused')
        self.assertEqual(self.engine.tick(now=1000),[])
        self.engine.sync_presence(True,now=1000)
        self.assertEqual(self.event()['due'],1080)

    def test_camera_failure_resumes_only_automatic_pauses(self):
        self.engine.sync_presence(False,now=200)
        self.engine.sync_presence(None,now=400)
        self.assertEqual(self.event()['due'],480)
        self.engine.command('pause',self.id,now=410)
        self.engine.sync_presence(False,now=420);self.engine.sync_presence(True,now=500)
        self.assertEqual(self.event()['state'],'paused')

    def test_manual_pause_while_away_uses_frozen_remaining(self):
        self.engine.sync_presence(False,now=200)
        self.engine.command('pause',self.id,now=500)
        self.engine.sync_presence(True,now=600)
        self.assertEqual(self.event()['remaining'],80)
        self.assertEqual(self.event()['state'],'paused')

    def test_standing_minute_is_not_paused_or_recorded_as_done(self):
        self.engine.command('start','standing',now=100)
        self.engine.command('standing_begin','standing',now=200)
        self.engine.sync_presence(False,now=210)
        standing=next(e for e in self.engine.snapshot()['events'] if e['id']=='standing')
        self.assertEqual(standing['standing_until'],260)
        self.assertEqual(standing['state'],'running')
        self.assertFalse(any(h['kind']=='completed' for h in self.engine.snapshot()['history']))

    def test_schedule_pause_and_presence_do_not_unpause_each_other(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        def stamp(value):return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        begin=stamp('2026-10-16T00:59:00');stop=begin+60;resume=stamp('2026-10-16T08:00:00')
        self.engine.command('start',self.id,now=begin)
        self.engine.sync_schedule(True,now=begin)
        self.engine.sync_presence(False,now=begin+10)
        self.engine.sync_schedule(True,now=stop)
        self.engine.sync_presence(True,now=stop+10)
        self.assertEqual(self.event()['state'],'scheduled_pause')
        self.engine.sync_schedule(True,now=resume)
        self.engine.sync_presence(True,now=resume)
        self.assertEqual(self.event()['state'],'running')
        self.assertEqual(self.event()['due'],resume+170)

    def test_restart_does_not_leave_orphan_camera_pause(self):
        self.engine.sync_presence(False,now=200)
        restored=EventReminders(self.engine.path,{})
        restored.sync_presence(None,now=1000)
        event=next(e for e in restored.snapshot()['events'] if e['id']==self.id)
        self.assertEqual(event['due'],1080)
