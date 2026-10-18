"""Native macOS screen-covering focus timer, launched separately from the menu app."""
import json
import sys
import time
import urllib.request
import objc
from Foundation import NSObject, NSTimer, NSMakeRect
from AppKit import (
    NSApplication, NSWindow, NSScreen, NSTextField, NSButton, NSFont, NSColor,
    NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
    NSWindowStyleMaskBorderless, NSStatusWindowLevel,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSApplicationPresentationAutoHideDock, NSApplicationPresentationAutoHideMenuBar,
    NSWorkspace, NSEvent,
)

BASE = 'http://127.0.0.1:19527'
EVENT_ID = sys.argv[1]
STANDING_MINUTE = '--standing-minute' in sys.argv


def command(action):
    req = urllib.request.Request(BASE + '/api/events', data=json.dumps({'action': action, 'id': EVENT_ID}).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=2) as response:
        result = json.load(response)
    if result.get('error'):
        raise ValueError(result['error'])


def read_event():
    with urllib.request.urlopen(BASE + '/api/events', timeout=2) as response:
        data = json.load(response)
    return next(e for e in data['events'] if e['id'] == EVENT_ID)


class FocusWindow(NSWindow):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def cancelOperation_(self, sender):
        self.focusController.exit_(None)

    def keyDown_(self, event):
        if event.keyCode() == 53:
            self.focusController.exit_(None)
        else:
            objc.super(FocusWindow, self).keyDown_(event)


class FocusController(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self.previous = NSWorkspace.sharedWorkspace().frontmostApplication()
        self.active = False
        self.finished = False
        self.confirming = False
        screens = NSScreen.screens()
        point = NSEvent.mouseLocation()
        screen = next((s for s in screens if s.frame().origin.x <= point.x < s.frame().origin.x+s.frame().size.width
                       and s.frame().origin.y <= point.y < s.frame().origin.y+s.frame().size.height), NSScreen.mainScreen())
        frame = screen.frame()
        self.window = FocusWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        self.window.focusController = self
        self.window.setReleasedWhenClosed_(False)
        self.window.setLevel_(NSStatusWindowLevel + 1)
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.window.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.035, 0.05, 0.095, 1))
        width, height = frame.size.width, frame.size.height
        self.name = self.label_('专注倒计时', (0, height*.70, width, 70), 36)
        self.clock = self.label_('--:--', (0, height*.35, width, height*.30), min(180, width*.15))
        self.status = self.label_('正在连接应用…', (0, height*.30, width, 60), 20)
        self.primary = self.button_('我已完成', (width/2-130, height*.20, 260, 50), 'primary:')
        self.primary.setHidden_(True)
        self.name.setHidden_(True)
        self.status.setHidden_(True)
        self.exitButton = self.button_('退出 · Esc', (width-240, height-80, 210, 44), 'exit:')
        self.hint = self.label_('退出展示后事件计时继续；Cmd+Tab 仍可切换应用。', (0, 40, width, 44), 15)
        self.hint.setHidden_(True)
        app = NSApplication.sharedApplication()
        app.setPresentationOptions_(NSApplicationPresentationAutoHideDock | NSApplicationPresentationAutoHideMenuBar)
        self.window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
        try:
            self.event = read_event()
            self.name.setStringValue_(self.event['name'])
            self.primary_(None)
        except Exception as exc:
            self.status.setHidden_(False)
            self.status.setStringValue_('无法开始：' + str(exc))
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1, self, 'tick:', None, True)

    @objc.python_method
    def label_(self, text, rect, size):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(*rect))
        label.setStringValue_(text)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBordered_(False)
        label.setDrawsBackground_(False)
        label.setAlignment_(1)
        label.setTextColor_(NSColor.whiteColor())
        label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.2))
        self.window.contentView().addSubview_(label)
        return label

    @objc.python_method
    def button_(self, text, rect, selector):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(*rect))
        button.setTitle_(text)
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_(selector)
        self.window.contentView().addSubview_(button)
        return button

    def primary_(self, sender):
        try:
            if self.confirming:
                command('complete')
                command('focus_end')
                self.active = False
                self.confirming = False
                self.finished = True
                self.status.setStringValue_('本轮已完成')
                self.primary.setHidden_(True)
            elif not self.active:
                command('focus')
                if STANDING_MINUTE:
                    command('standing_begin')
                    self.name.setStringValue_('至少站立 1 分钟')
                    self.name.setHidden_(False)
                self.active = True
                self.finished = False
                self.primary.setTitle_('暂停')
            else:
                command('resume' if self.event['state'] == 'paused' else 'pause')
            self.tick_(None)
        except Exception as exc:
            self.status.setStringValue_('操作失败：' + str(exc))

    def tick_(self, timer):
        try:
            if self.active:
                command('focus_heartbeat')
            self.event = read_event()
            if self.event['state'] == 'scheduled_pause':
                self.exit_(None)
                return
            if STANDING_MINUTE and self.active and not self.event.get('standing_until'):
                self.exit_(None)
                return
            self.name.setStringValue_('至少站立 1 分钟' if STANDING_MINUTE else self.event['name'])
            if self.finished or self.confirming:
                self.clock.setStringValue_('完成' if self.finished else '时间到')
                return
            seconds = max(0, self.event.get('standing_until', time.time()) - time.time()) if STANDING_MINUTE else self.event.get('remaining', 0) if self.event['state'] == 'paused' else max(0, (self.event['due'] or time.time())-time.time())
            n = int(seconds + .999)
            self.clock.setStringValue_(f'{n//60:02d}:{n%60:02d}')
            if self.active and seconds <= 0 and self.event['state'] != 'paused':
                if STANDING_MINUTE or self.event['mode'] == 'once':
                    command('standing_finish' if STANDING_MINUTE else 'complete')
                    command('focus_end')
                    self.active = False
                    self.finished = True
                    self.status.setStringValue_('时间到，本轮计时已结束')
                    self.clock.setStringValue_('完成')
                    self.primary.setHidden_(True)
                else:
                    self.confirming = True
                    self.status.setStringValue_('时间到，请实际完成后确认')
                    self.clock.setStringValue_('时间到')
                    self.primary.setTitle_('我已完成')
                    self.primary.setHidden_(False)
            elif self.active:
                paused = self.event['state'] == 'paused'
                self.primary.setTitle_('继续' if paused else '暂停')
                self.status.setStringValue_('已暂停' if paused else '专注当前动作，其他事情稍后处理')
        except Exception:
            self.status.setHidden_(False)
            self.status.setStringValue_('连接中断，请按 Esc 退出')

    def applicationDidResignActive_(self, notification):
        if hasattr(self, "window") and self.window.isVisible():
            self.exit_(None)

    def exit_(self, sender):
        if hasattr(self, 'timer'):
            self.timer.invalidate()
        try:
            if STANDING_MINUTE and not self.finished:
                command('standing_cancel')
            command('focus_end')
        except Exception:
            pass
        self.window.orderOut_(None)
        NSApplication.sharedApplication().setPresentationOptions_(0)
        if self.previous:
            self.previous.activateWithOptions_(0)
        NSApplication.sharedApplication().terminate_(None)


if __name__ == '__main__':
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = FocusController.alloc().init()
    app.finishLaunching()
    app.setDelegate_(controller)
    controller.applicationDidFinishLaunching_(None)
    app.run()
