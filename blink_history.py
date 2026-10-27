"""Local minute-level blink statistics. No images or landmark data are stored."""
from contextlib import contextmanager
from datetime import datetime, date, time, timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


class BlinkHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.pending = {}
        self.minute = None

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        connection.execute('''CREATE TABLE IF NOT EXISTS minutes (
            minute INTEGER NOT NULL, target INTEGER NOT NULL,
            blinks INTEGER NOT NULL, observed REAL NOT NULL,
            PRIMARY KEY (minute, target))''')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, now, blinks, observed, target):
        if not blinks and observed <= 0:
            return
        minute = int(now//60)*60
        if self.minute is not None and minute != self.minute:
            self.flush()
        self.minute = minute
        values = self.pending.setdefault((minute, target), [0, 0.0])
        values[0] += blinks
        values[1] += observed

    def flush(self):
        if not self.pending:
            return
        with self._connect() as connection:
            connection.executemany('''INSERT INTO minutes VALUES (?, ?, ?, ?)
                ON CONFLICT(minute, target) DO UPDATE SET
                blinks=blinks+excluded.blinks, observed=observed+excluded.observed''',
                [(minute, target, values[0], values[1]) for (minute, target), values in self.pending.items()])
        self.pending.clear()

    def read(self, selected_date=None, page=1):
        day = date.fromisoformat(selected_date) if selected_date else datetime.now(ZONE).date()
        page = max(1, int(page))
        start = datetime.combine(day, time(), ZONE).timestamp()
        end = datetime.combine(day+timedelta(days=1), time(), ZONE).timestamp()
        self.flush()
        with self._connect() as connection:
            count, blinks, observed = connection.execute(
                'SELECT COUNT(*), COALESCE(SUM(blinks),0), COALESCE(SUM(observed),0) FROM minutes WHERE minute>=? AND minute<?',
                (start, end)).fetchone()
            pages = max(1, (count+19)//20)
            page = min(page, pages)
            records = connection.execute('SELECT minute,target,blinks,observed FROM minutes WHERE minute>=? AND minute<? ORDER BY minute DESC,target DESC LIMIT 20 OFFSET ?',
                                         (start, end, (page-1)*20)).fetchall()
        rows = []
        for minute, target, number, seconds in records:
            rate = round(number*60/seconds, 1) if seconds >= 50 else None
            rows.append(dict(time=minute, time_label=datetime.fromtimestamp(minute, ZONE).strftime('%H:%M'),
                             blinks=number, observed_seconds=round(seconds, 1), target=target, rate=rate,
                             status='insufficient' if rate is None else 'below_target' if rate < target else 'met'))
        return dict(date=str(day), timezone='Asia/Shanghai', page=page, pages=pages, total=count, rows=rows,
                    blinks=blinks, observed_seconds=round(observed, 1),
                    average_rate=round(blinks*60/observed, 1) if observed >= 50 else None)
