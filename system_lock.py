"""macOS session authentication boundary; camera presence never unlocks a Mac."""
import ctypes
import os
import plistlib
import time


class MacSession:
    def __init__(self):
        self.error = ''
        self.cg = self.cf = self.login = None
        try:
            self.cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            self.cg.CGSessionCopyCurrentDictionary.argtypes = []
            self.cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
            self.cf.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p]
            self.cf.CFPropertyListCreateData.restype = ctypes.c_void_p
            self.cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
            self.cf.CFDataGetLength.restype = ctypes.c_long
            self.cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
            self.cf.CFDataGetBytePtr.restype = ctypes.c_void_p
            self.cf.CFRelease.argtypes = [ctypes.c_void_p]
            # This login.framework SPI is runtime checked. A macOS update may
            # remove it; report failure rather than substituting a screen cover.
            self.login = ctypes.CDLL('/System/Library/PrivateFrameworks/login.framework/Versions/Current/login')
            self.login.SACLockScreenImmediate.argtypes = []
            self.login.SACLockScreenImmediate.restype = ctypes.c_int
        except (OSError, AttributeError) as exc:
            self.error = '系统锁屏接口不可用：' + str(exc)

    @staticmethod
    def classify(values, uid):
        if not isinstance(values, dict):
            return 'unknown'
        if values.get('CGSSessionScreenIsLocked') or values.get('kCGSessionScreenIsLocked'):
            return 'locked'
        if values.get('kCGSSessionOnConsoleKey') is False:
            return 'locked'
        if values.get('kCGSSessionUserIDKey') != uid:
            return 'unknown'
        if values.get('kCGSSessionOnConsoleKey') is True and values.get('kCGSessionLoginDoneKey') is True:
            return 'unlocked'
        return 'unknown'

    def state(self):
        if self.cg is None or self.cf is None:
            return 'unknown'
        session = data = None
        try:
            session = self.cg.CGSessionCopyCurrentDictionary()
            if not session:
                return 'unknown'
            data = self.cf.CFPropertyListCreateData(None, session, 200, 0, None)
            if not data:
                return 'unknown'
            raw = ctypes.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data))
            return self.classify(plistlib.loads(raw), os.getuid())
        except Exception:
            return 'unknown'
        finally:
            if data:
                self.cf.CFRelease(data)
            if session:
                self.cf.CFRelease(session)

    def lock(self):
        if self.login is None or self.error:
            raise RuntimeError(self.error or '系统锁屏接口不可用')
        result = self.login.SACLockScreenImmediate()
        if result != 0:
            raise RuntimeError(f'系统锁屏请求失败（{result}），请手动锁屏')


class SecureAwayLock:
    def __init__(self, session=None):
        self.session = session if session is not None else MacSession()
        self.state = 'unknown'
        self.pending = False
        self.lock_seen = False
        self.in_absence = False
        self.requested_at = None
        self.locked_at = None
        self.last_away_seconds = None
        self.error = ''

    def poll(self, now=None):
        now = time.time() if now is None else now
        self.state = self.session.state()
        if self.state == 'locked':
            self.lock_seen = True
            self.in_absence = True
            if self.locked_at is None:
                self.locked_at = now
        elif self.state == 'unlocked' and self.lock_seen:
            self.pending = False
            self.lock_seen = False
            self.error = ''
            if self.locked_at is not None:
                self.last_away_seconds = max(0, now-self.locked_at)
            self.locked_at = None
        if self.pending and not self.lock_seen and self.requested_at is not None and now-self.requested_at >= 5 and not self.error:
            self.error = '尚未确认系统锁定，请手动使用 Apple 菜单 → 锁定屏幕；活动计时保持暂停'
        return self.state

    @property
    def blocking(self):
        return self.state != 'unlocked' or self.pending

    def sync(self, present, enabled, allowed=True, now=None):
        now = time.time() if now is None else now
        if present is True and not self.blocking:
            self.in_absence = False
        if enabled and allowed and present is False and self.state == 'unlocked' and not self.in_absence and not self.pending:
            self.in_absence = True
            self.pending = True
            self.requested_at = now
            self.error = ''
            try:
                self.session.lock()
            except Exception as exc:
                self.error = str(exc) + '；活动计时保持暂停，确认系统锁定并解锁后恢复'
            self.poll(now)
        if enabled or self.pending or self.lock_seen:
            # No face, any face, an API failure or a disabled camera cannot stand
            # in for a confirmed authenticated session and confirmed presence.
            return True if not self.blocking and present is True else False
        return present

    def snapshot(self):
        return dict(session=self.state, pending=self.pending, locked_at=self.locked_at,
                    last_away_seconds=round(self.last_away_seconds) if self.last_away_seconds is not None else None,
                    error=self.error or getattr(self.session, 'error', ''))
