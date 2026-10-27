import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from blink_history import BlinkHistory, ZONE
from camera_monitor import CameraService


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE).timestamp()


class BlinkHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'blink.sqlite3'
        self.store = BlinkHistory(self.path)

    def test_restart_and_repeated_reads_do_not_duplicate_counts(self):
        now = stamp('2026-10-27T09:10:00')
        self.store.record(now, 4, 30, 12)
        self.store.read('2026-10-27')
        self.store.record(now+30, 3, 30, 12)
        self.store.flush()
        restored = BlinkHistory(self.path)
        for _ in range(2):
            data = restored.read('2026-10-27')
            self.assertEqual(data['blinks'], 7)
            self.assertEqual(data['rows'][0]['rate'], 7)
            self.assertEqual(data['observed_seconds'], 60)
            self.assertEqual(data['rows'][0]['status'], 'below_target')

    def test_insufficient_observation_is_preserved_without_rate_claim(self):
        self.store.record(stamp('2026-10-27T09:00:00'), 2, 12, 12)
        row = self.store.read('2026-10-27')['rows'][0]
        self.assertEqual(row['blinks'], 2)
        self.assertIsNone(row['rate'])
        self.assertEqual(row['status'], 'insufficient')

    def test_changing_target_preserves_both_targets(self):
        now = stamp('2026-10-27T09:00:00')
        self.store.record(now, 5, 20, 10)
        self.store.record(now+20, 6, 30, 15)
        data = self.store.read('2026-10-27')
        self.assertEqual({row['target'] for row in data['rows']}, {10,15})
        self.assertEqual(data['blinks'], 11)
        self.assertEqual(data['average_rate'], 13.2)

    def test_midnight_uses_shanghai_dates_and_minute_transition_flushes(self):
        self.store.record(stamp('2026-10-26T23:59:59'), 3, 50, 12)
        self.store.record(stamp('2026-10-27T00:00:00'), 4, 50, 12)
        restored = BlinkHistory(self.path)
        self.assertEqual(restored.read('2026-10-26')['blinks'],3)
        self.assertEqual(self.store.read('2026-10-27')['rows'][0]['time_label'],'00:00')

    def test_date_filter_pagination_and_empty_day(self):
        start = stamp('2026-10-27T08:00:00')
        for index in range(23):
            self.store.record(start+index*60, 12, 60, 12)
        data = self.store.read('2026-10-27', 1)
        self.assertEqual((data['total'], data['pages'], len(data['rows'])),(23,2,20))
        second = self.store.read('2026-10-27', 2)
        self.assertEqual(len(second['rows']),3)
        self.assertTrue(all(row['status']=='met' for row in second['rows']))
        self.assertFalse({r['time'] for r in second['rows']} & {r['time'] for r in data['rows']})
        self.assertEqual(self.store.read('2026-10-25')['total'],0)
        with self.assertRaises(ValueError):self.store.read('invalid')

    def test_old_daily_totals_are_preserved_without_fabricated_detail(self):
        import json
        root = Path(self.temp.name)
        (root/'camera-history.json').write_text(json.dumps({'2026-10-26':{'blinks':200,'observed_seconds':1000}}))
        service = CameraService(root, root)
        data = service.blink_details('2026-10-26')
        self.assertEqual(data['daily_total']['blinks'],200)
        self.assertEqual(data['total'],0)
        service._save()
        self.assertEqual(json.loads((root/'camera-history.json').read_text())['2026-10-26']['blinks'],200)

    def test_frame_signals_are_recorded_but_not_images(self):
        from unittest.mock import patch
        service = CameraService(self.temp.name, self.temp.name)
        service.settings = {'camera_blink_enabled':True,'camera_blink_target':15}
        now = stamp('2026-10-27T09:00:00')
        service.signals.day = '2026-10-27'
        service.signals.baseline = .32
        service.signals.last_eye = now-.1
        service.last_save = now
        with patch('camera_monitor.time.time', return_value=now):
            service._receive({'status':'frame','faces':1,'opening':.32,'person':True})
        data = service.blink_details('2026-10-27')
        self.assertEqual(data['rows'][0]['observed_seconds'], .1)
        self.assertEqual(data['rows'][0]['target'],15)
        self.assertNotIn('opening',data['rows'][0])
