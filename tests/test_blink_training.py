import tempfile
import unittest
from unittest.mock import patch
from blink_training import LowBlinkStreak, BlinkExercise
from camera_monitor import CameraService


class LowFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.streak=LowBlinkStreak()
        self.now=1000

    def minute(self, count=0, observed=60, target=12):
        for second in range(60):
            self.now+=1
            self.streak.observe(self.now, 1 if second<count else 0, 1 if second<observed else 0,
                                target, second<observed, True)

    def test_ten_consecutive_minutes_and_renewed_ten_minutes_after_reminder(self):
        for _ in range(9):self.minute(5)
        self.assertFalse(self.streak.pending)
        self.minute(5)
        self.assertTrue(self.streak.pending)
        self.assertIsNone(self.streak.take(allowed=False))
        self.assertEqual(self.streak.take()['minutes'],10)
        self.assertFalse(self.streak.pending)
        for _ in range(9):self.minute(5)
        self.assertFalse(self.streak.pending)
        self.minute(5);self.assertTrue(self.streak.pending)

    def test_a_qualified_minute_breaks_the_streak(self):
        for _ in range(5):self.minute(5)
        self.minute(12)
        self.assertEqual(self.streak.minutes,0)
        for _ in range(5):self.minute(5)
        self.assertFalse(self.streak.pending)

    def test_low_ten_minute_average_does_not_override_a_qualified_minute(self):
        for _ in range(5):self.minute(0)
        self.minute(20)
        for _ in range(4):self.minute(0)
        self.assertEqual(self.streak.minutes,4)
        self.assertFalse(self.streak.pending)

    def test_absence_disable_and_target_change_reset_evidence(self):
        for _ in range(3):self.minute(5)
        self.streak.observe(self.now+1,0,0,12,False,False)
        self.assertEqual(self.streak.minutes,0)
        self.minute(5)
        self.minute(5,target=15)
        self.assertEqual(self.streak.minutes,1)
        self.streak.observe(self.now+1,0,1,15,True,True,enabled=False)
        self.assertEqual(self.streak.minutes,0)

    def test_missing_frames_and_long_quality_failure_reset_streak(self):
        self.minute(0)
        self.now+=4
        self.streak.observe(self.now,0,.03,12,True,True)
        self.assertEqual(self.streak.minutes,0)
        self.minute(0)
        for _ in range(4):
            self.now+=1;self.streak.observe(self.now,0,0,12,False,True)
        self.assertEqual(self.streak.minutes,0)

    def test_recovery_before_popup_cancels_a_queued_reminder(self):
        for _ in range(10):self.minute(0)
        self.assertTrue(self.streak.pending)
        self.minute(15)
        self.assertFalse(self.streak.pending)
        self.assertIsNone(self.streak.take())

    def test_under_fifty_valid_seconds_cannot_count_as_a_low_minute(self):
        self.minute(0)
        for second in range(60):
            self.now+=1
            valid=second%3!=0
            self.streak.observe(self.now,0,1 if valid else 0,12,valid,True)
        self.assertEqual(self.streak.minutes,0)

    def test_a_short_bad_frame_does_not_erase_nine_minutes(self):
        for _ in range(9):self.minute(0)
        self.now+=.05;self.streak.observe(self.now,0,0,12,False,True)
        self.now+=.05;self.streak.observe(self.now,0,.05,12,True,True)
        self.assertEqual(self.streak.minutes,9)


class ExerciseTests(unittest.TestCase):
    def test_one_minute_required_and_finish_cannot_repeat(self):
        exercise=BlinkExercise();exercise.start(100)
        for now in range(101,160):exercise.heartbeat(now,True)
        with self.assertRaises(ValueError):exercise.finish(159.5,True)
        self.assertEqual(exercise.finish(160,True)['phase'],'completed')
        with self.assertRaises(ValueError):exercise.finish(161,True)

    def test_early_exit_lost_heartbeat_and_absence_are_not_completion(self):
        exercise=BlinkExercise();exercise.start(100);exercise.cancel()
        self.assertEqual(exercise.snapshot(110)['phase'],'cancelled')
        exercise.start(200)
        with self.assertRaises(ValueError):exercise.heartbeat(206,True)
        exercise.start(300)
        with self.assertRaises(ValueError):exercise.heartbeat(301,False)
        self.assertEqual(exercise.state['phase'],'cancelled')

    def test_completed_count_is_persisted_without_counting_early_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            service=CameraService(directory,directory)
            service.settings={'camera_blink_enabled':True}
            state={'status':'running','presence':'present'}
            with patch.object(service,'snapshot',return_value=state):
                with patch('camera_monitor.time.time',return_value=100):service.exercise_command('start')
                with patch('camera_monitor.time.time',return_value=101):service.exercise_command('cancel')
                self.assertEqual(service.exercise_count,0)
                with patch('camera_monitor.time.time',return_value=200):service.exercise_command('start')
                for now in range(201,260):
                    with patch('camera_monitor.time.time',return_value=now):service.exercise_command('heartbeat')
                with patch('camera_monitor.time.time',return_value=260):service.exercise_command('finish')
            self.assertEqual(CameraService(directory,directory).exercise_count,1)
