"""A local, bounded comparison trial with a shared visible sampling window."""
class BlinkComparison:
    def __init__(self):
        self.trial = None

    def start(self, now):
        if self.active(now):
            raise ValueError('测试正在进行，请等待结束')
        self.trial = dict(start_at=now+3, end_at=now+13, machine_count=0,
                          frames=0, valid_frames=0, human_count=None)
        return self.snapshot(now)

    def active(self, now):
        return self.trial is not None and now < self.trial['end_at']

    def observe(self, now, blink, onset, quality):
        trial = self.trial
        if trial is None or not trial['start_at'] <= now < trial['end_at']:
            return
        trial['frames'] += 1
        trial['valid_frames'] += quality == 'tracking'
        if blink and onset is not None and onset >= trial['start_at']:
            trial['machine_count'] += 1

    def submit(self, now, count):
        if self.trial is None or self.active(now):
            raise ValueError('请等待测试结束后填写次数')
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100:
            raise ValueError('请填写 0 到 100 之间的整数')
        self.trial['human_count'] = count
        return self.snapshot(now)

    def snapshot(self, now):
        if self.trial is None:
            return None
        trial = self.trial
        phase = 'ready' if now < trial['start_at'] else 'running' if now < trial['end_at'] else 'finished'
        revealed = trial['human_count'] is not None
        coverage = trial['valid_frames']/trial['frames'] if trial['frames'] else 0
        return {**trial, 'phase': phase, 'machine_count': trial['machine_count'] if revealed else None,
                'valid': trial['frames'] >= 100 and coverage >= .8,
                'difference': trial['machine_count']-trial['human_count'] if revealed else None}
