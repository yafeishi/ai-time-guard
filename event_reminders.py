"""Persistent concurrent reminder schedules, independent of the menu and HTTP UI."""
import json
import math
import threading
import time
import uuid
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path


def activity_schedule_state(now=None):
    """Latest Mon–Fri 08:00 start or next-day 01:00 stop, in Shanghai."""
    now = datetime.now(ZoneInfo('Asia/Shanghai')) if now is None else now.astimezone(ZoneInfo('Asia/Shanghai'))
    transitions = []
    for offset in range(9):
        day = now.date() - timedelta(days=offset)
        if day.weekday() < 5:
            start = datetime(day.year, day.month, day.day, 8, tzinfo=now.tzinfo)
            stop = start.replace(hour=1) + timedelta(days=1)
            transitions.extend(((start, True), (stop, False)))
    boundary, active = max((stamp, active) for stamp, active in transitions if stamp <= now)
    return active, boundary.isoformat()


class EventReminders:
    def __init__(self, path, config):
        self.path = Path(path)
        self.lock = threading.RLock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            now = time.time()
            standing = self.new_event('站立活动', config.get('stand_up_interval_minutes', 45), 'completion', 'work')
            standing.update(id='standing', state='running' if config.get('stand_up_enabled') else 'idle',
                            due=(config.get('stand_up_last_reminder_time') or now) + config.get('stand_up_interval_minutes', 45) * 60)
            if config.get('stand_up_pending'):
                standing.update(pending=True, due=(config.get('stand_up_last_reminder_time') or now) + 300)
            if config.get('stand_up_pause_until'):
                standing.update(state='snoozed', due=config['stand_up_pause_until'])
            self.data = {'events': [self.new_event('握力训练', config.get('countdown_duration_seconds', 180) / 60, 'once', 'all'), standing], 'history': []}
            self.save()
        self.merge_legacy_standing_history()
        self.sync_schedule(config.get("event_schedule_enabled", False))

    def sync_schedule(self, enabled=True, now=None):
        now = time.time() if now is None else now
        active, boundary = activity_schedule_state(datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')))
        with self.lock:
            old = self.data.get('schedule', {})
            if enabled and old.get('override_boundary') == boundary:
                active = old['override_active']
            if not enabled:
                active = True
            changed = old.get('enabled') != enabled or old.get('active') != active or old.get('boundary') != boundary
            crossed_session = enabled and active and old.get('enabled') and old.get('active') and old.get('boundary') != boundary
            for event in self.data['events']:
                if (not active or crossed_session) and event['state'] not in ('idle', 'scheduled_pause'):
                    event['_schedule_state'] = event['state']
                    event['_schedule_remaining'] = max(0, (event.get('due') or now) - now)
                    event['state'] = 'scheduled_pause'
                    event.pop('standing_until', None)
                    changed = True
                if active and event['state'] == 'scheduled_pause':
                    state = event.pop('_schedule_state', 'running')
                    remaining = event.pop('_schedule_remaining', 0)
                    event['state'] = state
                    if state != 'paused':
                        event['due'] = now + (remaining or event['seconds'])
                        event['anchor'] = event['due']
                    changed = True
            if changed:
                self.data['schedule'] = dict(enabled=enabled, active=active, boundary=boundary,
                                             timezone='Asia/Shanghai', override_boundary=old.get('override_boundary'),
                                             override_active=old.get('override_active'))
                self.save()
            return active

    def override_schedule(self, active, now=None):
        now = time.time() if now is None else now
        _, boundary = activity_schedule_state(datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')))
        with self.lock:
            self.data.setdefault('schedule', {}).update(override_boundary=boundary, override_active=active)
            self.save()
        return self.sync_schedule(True, now)

    def merge_legacy_standing_history(self):
        """Import old standing records without guessing reminder categories.

        Unified standing events also write the legacy file. Pair nearby timestamps
        one-to-one so these mirrors are never counted twice.
        """
        legacy_path = self.path.with_name('stand-up.json')
        if not legacy_path.exists():
            return
        legacy = json.loads(legacy_path.read_text())
        imported = []
        for kinds, source_key, target_kind in (
            (('due', 'followup', 'legacy_reminder'), 'reminders', 'legacy_reminder'),
            (('completed',), 'stood_up', 'completed'),
        ):
            existing = [row for row in self.data['history'] if row['event_id'] == 'standing' and row['kind'] in kinds]
            by_day = {}
            for row in existing:
                by_day.setdefault(row['time'][:10], []).append(datetime.fromisoformat(row['time']).timestamp())
            for day, record in legacy.items():
                candidates = by_day.get(day, []).copy()
                for stamp in sorted(record.get(source_key, [])):
                    instant = datetime.fromisoformat(stamp).timestamp()
                    match = min(range(len(candidates)), key=lambda i: abs(candidates[i]-instant)) if candidates else None
                    if match is not None and abs(candidates[match]-instant) <= 5:
                        candidates.pop(match)
                    else:
                        imported.append(dict(event_id='standing', name='站立活动', kind=target_kind,
                                             time=stamp, source='legacy_standing'))
        if imported:
            backup = self.path.with_name('events.before-standing-migration.json')
            if not backup.exists():
                shutil.copy2(self.path, backup)
            self.data['history'].extend(imported)
            self.data['history'].sort(key=lambda row: row['time'])
            self.save()

    @staticmethod
    def new_event(name, minutes, mode='completion', scope='work', retry_minutes=5, notification='modal'):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('名称需要 1～80 个字符')
        minutes = float(minutes)
        retry_minutes = float(retry_minutes)
        if not math.isfinite(minutes) or not 1/60 <= minutes <= 1440:
            raise ValueError('时长应在 1 秒～24 小时之间')
        if not math.isfinite(retry_minutes) or not 0 <= retry_minutes <= 120:
            raise ValueError('追提醒应在 0～120 分钟之间，0 表示不追提醒')
        if mode not in ('once', 'completion', 'fixed') or scope not in ('work', 'all') or notification not in ('modal', 'notification'):
            raise ValueError('无效的提醒配置')
        return dict(id=uuid.uuid4().hex, name=name.strip(), seconds=minutes*60, mode=mode, scope=scope,
                    retry=retry_minutes*60, notification=notification, state='idle', pending=False, due=None)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        tmp.replace(self.path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def log(self, event, kind, now):
        self.data['history'].append(dict(event_id=event['id'], name=event['name'], kind=kind,
                                         time=datetime.fromtimestamp(now).isoformat()))

    def command(self, action, event_id=None, config=None, now=None):
        now = time.time() if now is None else now
        with self.lock:
            schedule = self.data.get('schedule', {})
            if schedule.get('enabled') and not schedule.get('active') and action not in ('add', 'edit', 'delete', 'stop', 'standing_cancel'):
                raise ValueError('活动已暂停，将在下一个工作日早上 8 点恢复')
            if action == 'add':
                event = self.new_event(**config)
                self.data['events'].append(event)
            else:
                event = next((e for e in self.data['events'] if e['id'] == event_id), None)
                if event is None:
                    raise ValueError('事件不存在')
                if action in ('standing_begin', 'standing_finish', 'standing_cancel'):
                    if event_id != 'standing':
                        raise ValueError('此操作仅用于站立活动')
                    if action == 'standing_begin':
                        if not event.get('standing_until'):
                            event['standing_until'] = now + 60
                        self.save()
                        return
                    if action == 'standing_cancel':
                        event.pop('standing_until', None)
                        self.save()
                        return
                    until = event.get('standing_until')
                    if not until or now < until:
                        raise ValueError('请完成一分钟站立倒计时')
                    event.pop('standing_until', None)
                    if event['state'] == 'idle':
                        event['state'] = 'running'
                    action = 'complete'
                if action == 'edit':
                    fresh = self.new_event(**config)
                    for key in ('name', 'seconds', 'mode', 'scope', 'retry', 'notification'):
                        event[key] = fresh[key]
                elif action == 'delete':
                    self.data['events'].remove(event)
                elif action == 'start':
                    event.update(state='running', pending=False, due=now+event['seconds'], anchor=now+event['seconds'])
                elif action == 'stop':
                    event.pop('standing_until', None)
                    event.update(state='idle', pending=False, due=None)
                elif action == 'pause':
                    event.update(remaining=max(0, (event['due'] or now)-now), state='paused')
                elif action == 'resume':
                    if event['state'] == 'paused':
                        event.update(state='running', due=now+event.get('remaining', event['seconds']), anchor=now+event.get('remaining', event['seconds']))
                elif action in ('pause1h', 'pause2h'):
                    event.update(state='snoozed', due=now+(3600 if action == 'pause1h' else 7200))
                elif action == 'snooze':
                    event.update(state='snoozed', due=now+(event['retry'] or 300))
                elif action == 'complete':
                    if event['state'] == 'idle' and not event['pending']:
                        raise ValueError('请先开始事件')
                    event.pop('standing_until', None)
                    self.log(event, 'completed', now)
                    if event['mode'] == 'once':
                        event.update(state='idle', pending=False, due=None)
                    elif event['mode'] == 'completion':
                        event.update(state='running', pending=False, due=now+event['seconds'], anchor=now+event['seconds'])
                    else:
                        anchor = event.get('anchor', now)
                        due = anchor + (max(0, math.floor((now-anchor)/event['seconds']))+1)*event['seconds'] if anchor <= now else anchor
                        event.update(state='running', pending=False, due=due)
                else:
                    raise ValueError('无效操作')
            self.save()

    def tick(self, on_duty=True, now=None):
        now = time.time() if now is None else now
        with self.lock:
            due = []
            for event in self.data['events']:
                if event['scope'] == 'work' and not on_duty:
                    continue
                if event['state'] not in ('running', 'snoozed', 'waiting') or event['due'] is None or now < event['due']:
                    continue
                anchor = event.get('anchor', event['due'])
                fixed_due = event['mode'] == 'fixed' and now >= anchor
                self.log(event, 'due' if fixed_due or not event['pending'] else 'followup', now)
                next_retry = now+event['retry'] if event['retry'] else None
                if event['mode'] == 'fixed':
                    if fixed_due:
                        anchor += (math.floor((now-anchor)/event['seconds'])+1)*event['seconds']
                    event['anchor'] = anchor
                    next_retry = min(anchor, next_retry) if next_retry else anchor
                event.update(pending=True, state='waiting', due=next_retry)
                due.append(dict(event))
            if due:
                self.save()
            return due

    def shift(self, on_duty, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for event in self.data['events']:
                if event['scope'] == 'work' and on_duty and event['state'] != 'idle':
                    event.update(state='running', pending=False, due=now+event['seconds'], anchor=now+event['seconds'])
            self.save()
