"""Continuous low-frequency evidence and a supervised one-minute exercise."""
class LowBlinkStreak:
    def __init__(self):
        self.reset()

    def reset(self, target=None):
        self.target = target
        self.start = self.last_seen = self.invalid_since = None
        self.observed = 0.0
        self.blinks = self.minutes = 0
        self.pending = False

    def observe(self, now, blinks, seconds, target, tracking, present, enabled=True):
        if not enabled or present is not True:
            self.reset(target)
            return
        if target != self.target or (self.last_seen is not None and now-self.last_seen > 3):
            self.reset(target)
        self.last_seen = now
        if not tracking:
            if self.invalid_since is None:
                self.invalid_since = now
            if now-self.invalid_since >= 3:
                self.reset(target)
            return
        self.invalid_since = None
        if seconds <= 0:
            return
        if self.start is None:
            self.start = now-seconds
        self.observed += seconds
        self.blinks += blinks
        if now-self.start >= 60:
            rate = round(self.blinks*60/self.observed, 1) if self.observed >= 50 else None
            if rate is not None and rate < target:
                self.minutes = min(10, self.minutes+1)
            else:
                self.minutes = 0
            self.pending = self.minutes >= 10
            self.start = now
            self.observed = 0.0
            self.blinks = 0

    def take(self, allowed=True):
        if not allowed or not self.pending:
            return None
        result = {'minutes': 10, 'target': self.target}
        self.reset(self.target)
        return result


class BlinkExercise:
    def __init__(self):
        self.state = None

    def active(self, now):
        if self.state and self.state['phase'] == 'running' and (now-self.state['heartbeat'] > 5 or now-self.state['end_at'] > 5):
            self.state['phase'] = 'cancelled'
        return self.state is not None and self.state['phase'] == 'running'

    def start(self, now):
        if self.active(now):
            raise ValueError('一分钟眨眼练习已在进行中')
        self.state = dict(phase='running', start_at=now, end_at=now+60, heartbeat=now)
        return dict(self.state)

    def heartbeat(self, now, available):
        if not self.active(now):
            raise ValueError('练习已中断，请重新开始')
        if not available:
            self.cancel()
            raise ValueError('离座、锁屏或检测中断，练习已取消')
        self.state['heartbeat'] = now
        return dict(self.state)

    def finish(self, now, available):
        self.heartbeat(now, available)
        if now < self.state['end_at']:
            raise ValueError('请完成一分钟练习')
        self.state['phase'] = 'completed'
        return dict(self.state)

    def cancel(self):
        if self.state and self.state['phase'] == 'running':
            self.state['phase'] = 'cancelled'

    def snapshot(self, now):
        self.active(now)
        return dict(self.state) if self.state else None
