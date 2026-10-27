"""Camera signal processing and native capture lifecycle. No images enter Python."""
from blink_history import BlinkHistory
from blink_comparison import BlinkComparison
from collections import deque
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import threading
import time


class CameraSignals:
    """Deterministic blink/presence state machine, independent of camera hardware."""
    def __init__(self):
        self.reset()
        self.day = str(datetime.now().date())
        self.total_blinks = 0
        self.total_observed = 0.0

    def reset(self):
        self.last_frame = None
        self.last_eye = None
        self.frame_times = deque(maxlen=60)
        self.missing_since = None
        self.present_since = None
        self.presence = 'unknown'
        self.faces = 0
        self.samples = deque(maxlen=150)
        self.eye_samples = [deque(maxlen=150), deque(maxlen=150)]
        self.eye_baselines = None
        self.latest_opening = None
        self.latest_ratio = None
        self.recent_ratios = deque(maxlen=300)
        self.session_blinks = 0
        self.last_blink_at = None
        self.blink_started_at = None
        self.eye_phase = 'unknown'
        self.fps = None
        self.calibration_start = None
        self.baseline = None
        self.closed_since = None
        self.blinks = deque()
        self.observations = deque()
        self.quality = 'warming'

    def feed(self, now, faces, opening=None, person=None, away_after=10, return_after=2, eyes=None):
        if self.last_frame is not None and now-self.last_frame > 2:
            self.reset()
        self.last_frame = now
        self.frame_times.append(now)
        self.faces = faces
        person = faces > 0 if person is None else person
        if person:
            self.missing_since = None
            if self.present_since is None:
                self.present_since = now
            if now-self.present_since >= return_after:
                self.presence = 'present'
        else:
            self.present_since = None
            if self.missing_since is None:
                self.missing_since = now
            if now-self.missing_since >= away_after:
                self.presence = 'away'
        if faces != 1 or opening is None or not math.isfinite(opening) or not 0 <= opening <= 1:
            self.quality = 'multiple_faces' if faces > 1 else 'eyes_unreliable' if person else 'no_person'
            # Face loss cancels the gesture, but one missing eye-landmark frame
            # during an already detected closure must not erase a real blink.
            if faces != 1 or self.closed_since is None or now-self.closed_since > .25:
                self.closed_since = None
            self.eye_phase = 'unknown'
            self.latest_ratio = None
            self.last_eye = None
            return False
        self.latest_opening = opening
        if len(self.frame_times) >= 5 and self.frame_times[-1]-self.frame_times[0] >= 1:
            self.fps = (len(self.frame_times)-1)/(self.frame_times[-1]-self.frame_times[0])
            if self.fps < 9.5:
                self.quality = 'low_fps'
                self.closed_since = None
                self.eye_phase = 'unknown'
                self.latest_ratio = None
                self.last_eye = None
                return False
        if self.calibration_start is None:
            self.calibration_start = now
        self.samples.append(opening)
        valid_eyes = (isinstance(eyes, (tuple, list)) and len(eyes) == 2
                      and all(isinstance(value, (float, int)) and math.isfinite(value) and 0 <= value <= 1 for value in eyes))
        if valid_eyes:
            for samples, value in zip(self.eye_samples, eyes):
                samples.append(value)
        if self.baseline is None:
            self.quality = 'calibrating'
            if len(self.samples) >= 30 and now-self.calibration_start >= 5:
                ordered = sorted(self.samples)
                baseline = ordered[int((len(ordered)-1)*.85)]
                if baseline >= .12:
                    self.baseline = baseline
                    if all(len(samples) >= 30 for samples in self.eye_samples):
                        self.eye_baselines = [sorted(samples)[int((len(samples)-1)*.85)] for samples in self.eye_samples]
                    self.quality = 'tracking'
            self.last_eye = now
            return False
        self.quality = 'tracking'
        dt = min(.3, max(0, now-self.last_eye)) if self.last_eye is not None else 0
        self.last_eye = now
        self.observations.append((now, dt))
        self.total_observed += dt
        blink = False
        # Each eye has its own open-eye baseline; asymmetric natural eye sizes
        # no longer mask closure. A wink alone still cannot count as a blink.
        ratio = max(value/base for value, base in zip(eyes, self.eye_baselines)) if valid_eyes and self.eye_baselines and min(self.eye_baselines) > .05 else opening/self.baseline
        self.latest_ratio = ratio
        self.recent_ratios.append((now, ratio))
        if ratio < .60:
            self.eye_phase = 'closed'
            if self.closed_since is None:
                self.closed_since = now
        elif ratio > .75:
            self.eye_phase = 'open'
            if self.closed_since is not None:
                duration = now-self.closed_since
                if .025 <= duration <= .8:
                    self.blinks.append(now)
                    self.total_blinks += 1
                    self.session_blinks += 1
                    self.last_blink_at = now
                    self.blink_started_at = self.closed_since
                    blink = True
                self.closed_since = None
            # Track the recent open-eye upper percentile after reopening. A
            # wider initial calibration must not keep subsequent blinks merged.
            ordered = sorted(self.samples)
            candidate = ordered[int((len(ordered)-1)*.85)]
            self.baseline = .98*self.baseline + .02*candidate
            if valid_eyes and self.eye_baselines:
                for index, samples in enumerate(self.eye_samples):
                    candidate_eye = sorted(samples)[int((len(samples)-1)*.85)]
                    self.eye_baselines[index] = .98*self.eye_baselines[index] + .02*candidate_eye
        return blink

    def snapshot(self, now, target=12):
        while self.blinks and self.blinks[0] < now-60:
            self.blinks.popleft()
        while self.observations and self.observations[0][0] < now-60:
            self.observations.popleft()
        while self.recent_ratios and self.recent_ratios[0][0] < now-10:
            self.recent_ratios.popleft()
        fresh = self.last_frame is not None and 0 <= now-self.last_frame <= 3
        observed = sum(dt for _, dt in self.observations)
        rate = round(len(self.blinks)*60/observed, 1) if fresh and self.quality == 'tracking' and observed >= 50 else None
        return dict(presence=self.presence if fresh else 'unknown', quality=self.quality if fresh else 'unavailable',
                    minute_blinks=len(self.blinks), observed_seconds=round(observed, 1), blink_rate=rate,
                    target=target, below_target=rate is not None and rate < target,
                    today_blinks=self.total_blinks, today_observed_seconds=round(self.total_observed, 1),
                    baseline=round(self.baseline, 3) if self.baseline else None,
                    last_frame=self.last_frame, missing_since=self.missing_since,
                    session_blinks=self.session_blinks, last_blink_at=self.last_blink_at,
                    eye_phase=self.eye_phase if fresh else 'unknown', fps=round(self.fps, 1) if self.fps and fresh else None,
                    eye_ratio=round(self.latest_ratio, 2) if self.latest_ratio is not None and fresh else None,
                    recent_min_ratio=round(min(ratio for _, ratio in self.recent_ratios), 2) if fresh and self.recent_ratios else None)


_BUILD_LOCK = threading.Lock()


def build_camera_helper(root, source_root):
    with _BUILD_LOCK:
        return _build_camera_helper(root, source_root)


def _build_camera_helper(root, source_root):
    source = Path(source_root) / 'native' / 'CameraMonitor.swift'
    bundle = Path(root) / 'runtime' / 'AI Time Guard Camera.app'
    executable = bundle / 'Contents' / 'MacOS' / 'camera-monitor'
    fingerprint = hashlib.sha256(source.read_bytes() + platform.machine().encode()).hexdigest()
    stamp = bundle / 'Contents' / 'Resources' / 'source.sha256'
    stamp.parent.mkdir(parents=True, exist_ok=True)
    old_stamp = bundle / 'Contents' / 'source.sha256'
    if old_stamp.exists():
        old_stamp.unlink()
    if executable.exists() and stamp.exists() and stamp.read_text() == fingerprint:
        verified = subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(bundle)], capture_output=True, timeout=15)
        if verified.returncode == 0:
            return executable
    compiler = Path('/Library/Developer/CommandLineTools/usr/bin/swiftc')
    sdk = Path('/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk')
    if not compiler.exists() or not sdk.exists():
        raise RuntimeError('需要安装 Apple Command Line Tools 才能构建本机摄像头组件')
    executable.parent.mkdir(parents=True, exist_ok=True)
    info = dict(CFBundleName='AI Time Guard Camera', CFBundleDisplayName='AI Time Guard Camera',
                CFBundleIdentifier='com.dang.ai-time-guard.camera', CFBundleExecutable='camera-monitor',
                CFBundlePackageType='APPL', CFBundleVersion='1', CFBundleShortVersionString='1.0',
                LSUIElement=True, NSCameraUsageDescription='仅在本机检测眨眼和是否离座；不保存或上传摄像头画面。')
    with (bundle/'Contents'/'Info.plist').open('wb') as f:
        plistlib.dump(info, f)
    temporary = executable.with_suffix('.building')
    result = subprocess.run([str(compiler), '-sdk', str(sdk), '-swift-version', '5', '-O', str(source), '-o', str(temporary)],
                            env={**os.environ, 'DEVELOPER_DIR': '/Library/Developer/CommandLineTools'},
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError('摄像头组件构建失败，请检查 Command Line Tools 安装')
    temporary.replace(executable)
    stamp.write_text(fingerprint)
    result = subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(bundle)], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('摄像头组件签名失败')
    return executable


def offline_camera_command(executable, *arguments):
    """Deny all network access in the process that can see raw camera frames."""
    sandbox = Path('/usr/bin/sandbox-exec')
    if not sandbox.is_file():
        raise RuntimeError('无法启用摄像头网络隔离，已停止摄像头检测')
    return [str(sandbox), '-p', '(version 1)(allow default)(deny network*)', str(executable), *arguments]


class CameraService:
    def __init__(self, root, source_root):
        self.root, self.source_root = Path(root), Path(source_root)
        self.lock = threading.RLock()
        self.signals = CameraSignals()
        self.process = None
        self.worker = None
        self.generation = 0
        self.settings = {}
        self.status = 'disabled'
        self.error = ''
        self.device = ''
        self.last_blink_reminder = 0
        self.diagnostic_frames = deque(maxlen=900)
        self.comparison = BlinkComparison()
        self.streaming_since = None
        self.last_save = 0
        self.wanted = None
        self.history_file = self.root/'camera-history.json'
        self.blink_history = BlinkHistory(self.root/'blink-history.sqlite3')
        try:
            self.history = json.loads(self.history_file.read_text()) if self.history_file.exists() else {}
            if not isinstance(self.history, dict):
                self.history = {}
        except (OSError, ValueError):
            self.history = {}
        day = self.history.get(self.signals.day, {})
        self.signals.total_blinks = day.get('blinks', 0)
        self.signals.total_observed = day.get('observed_seconds', 0)

    def configure(self, settings, allowed=True):
        wanted = (bool(settings.get('camera_blink_enabled') or settings.get('camera_away_enabled')) and allowed,
                  settings.get('camera_device_id', ''), bool(settings.get('camera_blink_enabled')))
        with self.lock:
            self.settings = dict(settings)
            if wanted == self.wanted:
                return
            if self.process:
                self._save()
            self.wanted = wanted
            self.generation += 1
            generation = self.generation
            self._stop_process()
            self.signals.reset()
            self.diagnostic_frames.clear()
            self.error = ''
            self.status = 'starting' if wanted[0] else 'outside_hours' if not allowed and (settings.get('camera_blink_enabled') or settings.get('camera_away_enabled')) else 'disabled'
            if wanted[0]:
                self.worker = threading.Thread(target=self._run, args=(generation, wanted[1], wanted[2]), daemon=True)
                self.worker.start()

    def _run(self, generation, device_id, blink_enabled):
        try:
            executable = build_camera_helper(self.root, self.source_root)
            with self.lock:
                if generation != self.generation:
                    return
                args = [str(executable)] + (['--device', device_id] if device_id else []) + ([] if blink_enabled else ['--presence-only'])
                process = subprocess.Popen(offline_camera_command(args[0], *args[1:]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, bufsize=1)
                self.process = process
            for line in process.stdout:
                try:
                    data = json.loads(line)
                except (ValueError, TypeError):
                    continue
                with self.lock:
                    if generation != self.generation:
                        return
                    self._receive(data)
            with self.lock:
                if generation == self.generation and self.status not in ('denied', 'error'):
                    self.status = 'error'
                    self.error = '摄像头连接已中断，可关闭后重新启用'
        except Exception as exc:
            with self.lock:
                if generation == self.generation:
                    self.status = 'error'
                    self.error = str(exc)

    def _receive(self, data):
        now = time.time()
        if data.get('status') == 'frame':
            self.status = 'running'
            self.error = ''
            day = str(datetime.fromtimestamp(now).date())
            if day != self.signals.day:
                self._save()
                self.signals.day = day
                self.signals.total_blinks = self.signals.total_observed = 0
                self.signals.reset()
            before_blinks, before_observed = self.signals.total_blinks, self.signals.total_observed
            blink = self.signals.feed(now, int(data.get('faces', 0)), data.get('opening'), data.get('person'),
                                      self.settings.get('camera_away_seconds', 10), eyes=[data.get('left_opening'), data.get('right_opening')])
            self.comparison.observe(now, blink, self.signals.blink_started_at if blink else None, self.signals.quality)
            if self.settings.get('camera_blink_enabled'):
                self.diagnostic_frames.append(dict(time=now, quality=self.signals.quality,
                    opening=data.get('opening'), left=data.get('left_opening'), right=data.get('right_opening'),
                    eye_baselines=list(self.signals.eye_baselines) if self.signals.eye_baselines else None,
                    ratio=self.signals.latest_ratio, phase=self.signals.eye_phase, blink=bool(blink),
                    blink_started_at=self.signals.blink_started_at if blink else None))
                self.blink_history.record(now, self.signals.total_blinks-before_blinks,
                                          self.signals.total_observed-before_observed,
                                          self.settings.get('camera_blink_target', 12))
            if now-self.last_save >= 60:
                self._save()
        else:
            self.status = data.get('status', 'error')
            if self.status == 'running':
                self.streaming_since = now
            self.error = data.get('error', '')
            self.device = data.get('device', self.device)
            if self.status in ('denied', 'error'):
                self.signals.reset()

    def take_blink_reminder(self, allowed=True):
        """Main UI thread consumes a current low-rate condition, never a stale queue."""
        with self.lock:
            now = time.time()
            state = self.snapshot()
            if (not allowed or self.comparison.active(now) or not self.settings.get('camera_blink_enabled')
                    or state['status'] != 'running' or state['presence'] != 'present'
                    or not state['below_target'] or now-self.last_blink_reminder < 300):
                return None
            self.last_blink_reminder = now
            return state

    def blink_reminder_dismissed(self):
        with self.lock:
            self.last_blink_reminder = time.time()

    def _save(self):
        self.blink_history.flush()
        self.history[self.signals.day] = dict(blinks=self.signals.total_blinks, observed_seconds=round(self.signals.total_observed, 1))
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.history_file.with_suffix('.tmp')
        temp.write_text(json.dumps(self.history, ensure_ascii=False, indent=2))
        temp.replace(self.history_file)
        self.last_save = time.time()

    def start_comparison(self):
        with self.lock:
            state = self.snapshot()
            if not self.settings.get('camera_blink_enabled') or state['status'] != 'running' or state['quality'] != 'tracking':
                raise ValueError('请先开启眨眼检测，并自然睁眼完成校准')
            return self.comparison.start(time.time())

    def submit_comparison(self, count):
        with self.lock:
            return self.comparison.submit(time.time(), count)

    def diagnostics(self):
        """A bounded in-memory signal trace for supervised local comparison tests."""
        with self.lock:
            now = time.time()
            return {'frames': [frame.copy() for frame in self.diagnostic_frames if now-frame['time'] <= 30],
                    'status': self.status, 'session_blinks': self.signals.session_blinks}

    def blink_details(self, selected_date=None, page=1):
        with self.lock:
            result = self.blink_history.read(selected_date, page)
            if result['date'] == self.signals.day:
                result['daily_total'] = {'blinks': self.signals.total_blinks,
                                         'observed_seconds': round(self.signals.total_observed, 1)}
            else:
                result['daily_total'] = self.history.get(result['date'], {'blinks': 0, 'observed_seconds': 0})
            return result

    def list_devices(self):
        executable = build_camera_helper(self.root, self.source_root)
        result = subprocess.run(offline_camera_command(executable, '--list-devices'), capture_output=True, text=True, timeout=15)
        for line in result.stdout.splitlines():
            try:
                data = json.loads(line)
                if data.get('status') == 'devices':
                    return data['devices']
            except ValueError:
                pass
        raise RuntimeError('无法列出摄像头，请检查设备连接')

    def recalibrate(self):
        with self.lock:
            self.signals.reset()
            self.diagnostic_frames.clear()

    def snapshot(self):
        with self.lock:
            now = time.time()
            state = self.signals.snapshot(now, self.settings.get('camera_blink_target', 12))
            status, error = self.status, self.error
            if status == 'running' and ((state['last_frame'] is not None and now-state['last_frame'] > 3) or (state['last_frame'] is None and self.streaming_since is not None and now-self.streaming_since > 8)):
                status, error = 'error', '摄像头没有新画面，自动暂停已解除；请检查设备是否被其他应用占用'
            return {**state, 'status': status, 'error': error, 'device': self.device,
                    'settings': {key: value for key, value in self.settings.items() if key.startswith('camera_')},
                    'history': dict(self.history), 'comparison': self.comparison.snapshot(now)}

    def _stop_process(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            # A native camera session can be stopping asynchronously. Reap without
            # blocking the menu thread; terminate is enough to release the device.
            threading.Thread(target=self._reap, args=(self.process,), daemon=True).start()
        self.process = None

    @staticmethod
    def _reap(process):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def stop(self):
        with self.lock:
            self.generation += 1
            self._stop_process()
            self._save()
