import unittest
from blink_comparison import BlinkComparison


class ComparisonTests(unittest.TestCase):
    def test_window_excludes_setup_late_and_boundary_crossing_blinks(self):
        test=BlinkComparison();state=test.start(100)
        self.assertEqual((state['start_at'],state['end_at']),(103,113))
        test.observe(102,True,101,'tracking')
        test.observe(103.1,True,102.9,'tracking')
        test.observe(105,True,104.9,'tracking')
        test.observe(113,True,112.9,'tracking')
        self.assertIsNone(test.snapshot(114)['machine_count'])
        result=test.submit(114,1)
        self.assertEqual(result['machine_count'],1)
        self.assertEqual(result['difference'],0)
        self.assertFalse(result['valid'])

    def test_quality_and_blind_comparison(self):
        test=BlinkComparison();test.start(0)
        for index in range(200):test.observe(3+index/25,index%50==0,3+index/25-.01,'tracking')
        self.assertIsNone(test.snapshot(14)['machine_count'])
        result=test.submit(14,3)
        self.assertTrue(result['valid'])
        self.assertEqual(result['difference'],0)

    def test_cannot_restart_or_submit_during_trial(self):
        test=BlinkComparison();test.start(100)
        with self.assertRaises(ValueError):test.start(102)
        with self.assertRaises(ValueError):test.submit(110,5)
        for invalid in (None,True,-1,2.5,'5'):
            with self.assertRaises(ValueError):test.submit(114,invalid)
