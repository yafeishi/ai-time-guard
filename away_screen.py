"""One native screen per confirmed absence, without changing event timers."""
import subprocess
import threading
import sys
from pathlib import Path


class AwayScreenSession:
    def __init__(self, root):
        self.root = Path(root)
        self.process = None
        self.in_absence = False
        self.error = ''

    def sync(self, away, blocked=False):
        if not away:
            self.in_absence = False
            self.stop()
            return
        if self.in_absence:
            return
        # A manual exit or existing focus screen suppresses this entire absence.
        self.in_absence = True
        if blocked:
            return
        if self.process is not None and self.process.poll() is None:
            return
        try:
            self.process = subprocess.Popen([sys.executable, str(self.root/'native_focus.py'), '--away'])
            self.error = ''
        except OSError as exc:
            self.error = str(exc)

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                threading.Thread(target=self._reap, args=(self.process,), daemon=True).start()
            else:
                self.process.wait()
            self.process = None

    @staticmethod
    def _reap(process):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
