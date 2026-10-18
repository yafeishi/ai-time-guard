import tempfile
import unittest
from pathlib import Path
from event_reminders import EventReminders


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'events.json'
        self.engine = EventReminders(self.path, {})

    def add(self, mode='completion', scope='all', retry_minutes=5):
        self.engine.command('add', config=dict(name='test',minutes=3,mode=mode,scope=scope,retry_minutes=retry_minutes), now=100)
        return self.engine.snapshot()['events'][-1]['id']

    def test_parallel_deadlines_and_followup_completion(self):
        first=self.add(); second=self.add('once')
        for eid in (first,second): self.engine.command('start',eid,now=100)
        due=self.engine.tick(now=280)
        self.assertEqual(len(due),2)
        self.assertEqual(self.engine.tick(now=281),[])
        self.assertEqual(len(self.engine.tick(now=580)),2)
        self.engine.command('complete',first,now=600)
        self.engine.command('complete',second,now=600)
        events={e['id']:e for e in self.engine.snapshot()['events']}
        self.assertEqual(events[first]['due'],780)
        self.assertEqual(events[second]['state'],'idle')
        self.assertEqual([h['kind'] for h in self.engine.snapshot()['history']],['due','due','followup','followup','completed','completed'])

    def test_restart_keeps_deadline_and_pause(self):
        eid=self.add(); self.engine.command('start',eid,now=100)
        restored=EventReminders(self.path,{})
        self.assertEqual(restored.snapshot()['events'][-1]['due'],280)
        restored.command('pause',eid,now=200)
        self.assertEqual(restored.tick(now=1000),[])
        restored.command('resume',eid,now=1000)
        self.assertEqual(restored.snapshot()['events'][-1]['due'],1080)

    def test_work_scope_and_shift(self):
        eid=self.add(scope='work'); self.engine.command('start',eid,now=100)
        self.assertEqual(self.engine.tick(False,now=500),[])
        self.engine.shift(True,now=1000)
        self.assertEqual(self.engine.snapshot()['events'][-1]['due'],1180)

    def test_fixed_schedule_does_not_drift_on_completion(self):
        eid=self.add('fixed');self.engine.command('start',eid,now=100)
        self.engine.tick(now=280);self.engine.command('complete',eid,now=300)
        self.assertEqual(self.engine.snapshot()['events'][-1]['due'],460)

    def test_no_followup_when_disabled(self):
        eid=self.add(retry_minutes=0);self.engine.command('start',eid,now=100)
        self.engine.tick(now=280)
        self.assertEqual(self.engine.tick(now=1000),[])

    def test_invalid_config_rejected(self):
        for minutes in (float('nan'),float('inf'),-1):
            with self.assertRaises(ValueError): self.engine.command('add',config=dict(name='bad',minutes=minutes))

    def test_fixed_keeps_reminding_on_schedule_without_completion(self):
        eid=self.add('fixed',retry_minutes=0);self.engine.command('start',eid,now=100)
        self.assertEqual(len(self.engine.tick(now=280)),1)
        self.assertEqual(len(self.engine.tick(now=460)),1)
        self.assertEqual([h['kind'] for h in self.engine.snapshot()['history']],['due','due'])

    def test_timed_pause_resumes_without_resetting_other_events(self):
        eid=self.add();other=self.add()
        self.engine.command('start',eid,now=100);self.engine.command('start',other,now=100)
        self.engine.command('pause1h',eid,now=200)
        due=self.engine.tick(now=280)
        self.assertEqual([e['id'] for e in due],[other])
        self.assertEqual(self.engine.snapshot()['events'][-2]['due'],3800)

    def test_edit_preserves_deadline_and_deletion_keeps_records(self):
        eid=self.add();self.engine.command('start',eid,now=100)
        self.engine.command('edit',eid,config=dict(name='renamed',minutes=45),now=200)
        self.assertEqual(self.engine.snapshot()['events'][-1]['due'],280)
        self.engine.tick(now=280);self.engine.command('delete',eid)
        self.assertEqual(self.engine.snapshot()['history'][0]['event_id'],eid)

    def test_legacy_import_preserves_records_and_avoids_mirrors(self):
        import json
        from datetime import datetime
        day='2026-10-13'
        self.engine.data['history']=[dict(event_id='standing',name='站立活动',kind='due',time=day+'T10:00:00.100000'),
                                     dict(event_id='standing',name='站立活动',kind='completed',time=day+'T10:01:00.100000')]
        self.engine.save()
        (self.path.parent/'stand-up.json').write_text(json.dumps({day:{'reminders':[day+'T09:00:00',day+'T10:00:00.200000'],
                                                                      'stood_up':[day+'T09:01:00',day+'T10:01:00.200000']}}))
        before=self.engine.snapshot()['events']
        self.engine.merge_legacy_standing_history()
        history=self.engine.snapshot()['history']
        self.assertEqual(len(history),4)
        self.assertEqual(sum(h['kind']=='legacy_reminder' for h in history),1)
        self.assertEqual(sum(h['kind']=='completed' for h in history),2)
        self.assertEqual(self.engine.snapshot()['events'],before)
        restored=EventReminders(self.path,{})
        self.assertEqual(restored.snapshot()['history'],history)
        self.assertTrue((self.path.parent/'events.before-standing-migration.json').exists())

    def test_legacy_matching_is_one_to_one(self):
        import json
        day='2026-10-13'
        self.engine.data['history']=[dict(event_id='standing',name='站立活动',kind='completed',time=day+'T10:01:00')]
        self.engine.save()
        (self.path.parent/'stand-up.json').write_text(json.dumps({day:{'reminders':[], 'stood_up':[day+'T10:01:00',day+'T10:01:01']}}))
        self.engine.merge_legacy_standing_history()
        self.assertEqual(len(self.engine.snapshot()['history']),2)
        self.engine.merge_legacy_standing_history()
        self.assertEqual(len(self.engine.snapshot()['history']),2)

    def test_standing_minute_cannot_complete_early(self):
        self.engine.command('start','standing',now=100)
        deadline=self.engine.snapshot()['events'][1]['due']
        self.engine.command('standing_begin','standing',now=200)
        self.assertEqual(self.engine.snapshot()['events'][1]['due'],deadline)
        with self.assertRaises(ValueError):self.engine.command('standing_finish','standing',now=259)
        self.assertEqual(len(self.engine.snapshot()['history']),0)
        self.engine.command('standing_finish','standing',now=260)
        standing=self.engine.snapshot()['events'][1]
        self.assertEqual(standing['due'],260+45*60)
        self.assertNotIn('standing_until',standing)
        self.assertEqual(self.engine.snapshot()['history'][0]['kind'],'completed')
        with self.assertRaises(ValueError):self.engine.command('standing_finish','standing',now=261)

    def test_cancel_standing_minute_keeps_original_reminder(self):
        self.engine.command('start','standing',now=100)
        self.engine.tick(now=2800)
        before=self.engine.snapshot()['events'][1]
        self.engine.command('standing_begin','standing',now=2900)
        self.engine.command('standing_cancel','standing',now=2910)
        self.assertEqual(self.engine.snapshot()['events'][1],before)
        self.assertFalse(any(h['kind']=='completed' for h in self.engine.snapshot()['history']))

    def test_standing_minute_deadline_survives_restart(self):
        self.engine.command('standing_begin','standing',now=100)
        restored=EventReminders(self.path,{})
        self.assertEqual(restored.snapshot()['events'][1]['standing_until'],160)
        with self.assertRaises(ValueError):restored.command('standing_finish','standing',now=159)

    def test_weekday_schedule_boundaries(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from event_reminders import activity_schedule_state
        cases=[('2026-10-16 00:59',True),('2026-10-16 01:00',False),('2026-10-16 07:59',False),
               ('2026-10-16 08:00',True),('2026-10-17 00:59',True),('2026-10-17 01:00',False),
               ('2026-10-17 08:00',False),('2026-10-18 12:00',False),('2026-10-19 00:30',False),('2026-10-19 08:00',True)]
        for stamp,active in cases:
            with self.subTest(stamp=stamp):
                now=datetime.strptime(stamp,'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                self.assertEqual(activity_schedule_state(now)[0],active)

    def test_schedule_pauses_all_scopes_and_preserves_remaining(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        def stamp(value):return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        begin=stamp('2026-10-16T00:59:00');stop=begin+60;resume=stamp('2026-10-16T08:00:00')
        eid=self.add(scope='all');self.engine.command('start',eid,now=begin)
        self.engine.sync_schedule(True,now=begin)
        self.assertFalse(self.engine.sync_schedule(True,now=stop))
        self.assertEqual(self.engine.snapshot()['events'][-1]['state'],'scheduled_pause')
        self.assertEqual(self.engine.tick(now=stop+10000),[])
        with self.assertRaises(ValueError):self.engine.command('start',eid,now=stop+60)
        self.assertTrue(self.engine.sync_schedule(True,now=resume))
        self.assertEqual(self.engine.snapshot()['events'][-1]['due'],resume+120)
        deadline=self.engine.snapshot()['events'][-1]['due']
        self.engine.sync_schedule(True,now=resume+30)
        self.assertEqual(self.engine.snapshot()['events'][-1]['due'],deadline)

    def test_schedule_manual_override_expires_at_next_boundary(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        def stamp(value):return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        now=stamp('2026-10-16T10:00:00')
        self.engine.sync_schedule(True,now)
        self.engine.override_schedule(False,now)
        self.assertFalse(self.engine.sync_schedule(True,now+3600))
        self.assertTrue(self.engine.sync_schedule(True,stamp('2026-10-19T08:00:00')))
