#!/usr/bin/env python3
"""
AI Time Guard - Mac 菜单栏应用
监控 AI 工具（CodeBuddy、Claude Code）使用时间，防止沉迷。

功能：
- 实时监控 AI 工具进程活跃状态
- 每日使用时长统计
- 达到限额时弹窗提醒
- 可配置每日限额、提醒间隔
- 支持暂停/恢复监控
- 使用数据持久化，每日自动重置
"""

import rumps
import psutil
import json
import os
import time
import subprocess
import threading
import unicodedata
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from AppKit import (
        NSWorkspace, NSApplication,
        NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular,
        NSImage,
        NSColor, NSAttributedString, NSMutableAttributedString, NSFont,
        NSForegroundColorAttributeName, NSFontAttributeName,
    )
    # 默认使用菜单栏应用模式，避免常驻 Dock 图标影响显示与聚焦行为。
    # 若需排查菜单栏问题，可设置环境变量 AITG_DOCK_DEBUG=1 临时启用 Dock 图标。
    debug_dock = os.environ.get("AITG_DOCK_DEBUG") == "1"
    activation_policy = (
        NSApplicationActivationPolicyRegular
        if debug_dock
        else NSApplicationActivationPolicyAccessory
    )
    NSApplication.sharedApplication().setActivationPolicy_(activation_policy)
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False


# ─── 配置 ───────────────────────────────────────────────

APP_NAME = "AI Time Guard"
CONFIG_DIR = Path.home() / ".ai-time-guard"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
DEBUG_LOG_FILE = CONFIG_DIR / "debug.log"

HISTORY_SAVE_INTERVAL_SECONDS = 60  # 历史数据写入节流间隔（秒）

# 菜单栏倒计时的可选时长（分钟）。用户的选择记在 countdown_duration_seconds，
# 下次打开菜单时沿用。
COUNTDOWN_DURATION_CHOICES = (1, 2, 3, 5, 10, 15, 20, 30)

DEFAULT_CONFIG = {
    "daily_limit_minutes": 180,       # 每日限额（分钟）
    "warning_at_percent": 80,         # 使用达到百分比时首次提醒
    "remind_interval_minutes": 15,    # 超限后每隔多久再次提醒
    "periodic_alert_minutes": 30,     # 每隔多久弹一次阻塞式提醒（必须手动关闭）
    "periodic_alert_enabled": True,   # 是否启用定时弹窗
    "check_interval_seconds": 10,     # 检测进程间隔（秒）
    "strict_mode": False,             # 严格模式：超限后尝试发送通知并持续提醒
    "theme": "dark",                  # 主题: dark | tencent-blue
    "stand_up_interval_minutes": 45,  # 站立活动提醒间隔（分钟）
    "stand_up_enabled": False,        # 是否启用站立提醒（默认关闭，按需启用）
    "stand_up_last_reminder_time": 0, # 上次提醒时间戳（秒），0 表示从未提醒
    "stand_up_title_mode": "final5",  # 菜单栏显示方式：final5 | always
    "countdown_duration_seconds": 180,# 菜单内倒计时时长（秒），记住用户上次选择
}


# ─── 工具函数 ─────────────────────────────────────────────

def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def debug_log(message):
    try:
        ensure_config_dir()
        with open(DEBUG_LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {message}\n")
    except Exception:
        pass


def load_config():
    ensure_config_dir()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
            config = {**DEFAULT_CONFIG, **saved}
            return config
        except Exception:
            pass
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(config):
    ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_history():
    """加载历史数据，兼容旧格式（纯数字）和新格式（含工具分类）"""
    ensure_config_dir()
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r") as f:
                raw = json.load(f)
            # 兼容旧格式：将 {日期: 数字} 迁移为 {日期: {total: 数字, tools: {}}}
            migrated = {}
            for key, val in raw.items():
                if isinstance(val, (int, float)):
                    migrated[key] = {"total": val, "tools": {}}
                elif isinstance(val, dict):
                    migrated[key] = val
                else:
                    migrated[key] = {"total": 0, "tools": {}}
            return migrated
        except Exception:
            pass
    return {}


def save_history(history):
    ensure_config_dir()
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ── NSColor 工厂：用名字解析为 NSColor 实例（无 AppKit 时全为 None） ──
# NSColor.systemBlueColor 等是类方法，必须加 () 拿实例；这里把"调出实例"这一步延后到 _set_attributed_title 内部
_STAND_UP_COLOR_NAMES = (
    "gray", "secondary", "blue", "green", "yellow", "orange", "red", "purple", "label",
)


def _resolve_color(name):
    """把名字解析成 NSColor 实例；无 AppKit 时返回 None

    全部使用系统动态色（labelColor / secondaryLabelColor / systemXxxColor），
    它们会跟随 macOS 的浅色/深色外观自动切换，所以在深色窗口下也能保持对比度。
    """
    if not HAS_APPKIT:
        return None
    if name == "gray":
        return NSColor.tertiaryLabelColor()
    if name == "quaternary":
        return NSColor.quaternaryLabelColor()
    if name == "secondary":
        return NSColor.secondaryLabelColor()
    if name == "blue":
        return NSColor.systemBlueColor()
    if name == "green":
        return NSColor.systemGreenColor()
    if name == "yellow":
        return NSColor.systemYellowColor()
    if name == "orange":
        return NSColor.systemOrangeColor()
    if name == "red":
        return NSColor.systemRedColor()
    if name == "purple":
        return NSColor.systemPurpleColor()
    if name == "label":
        return NSColor.labelColor()
    return None


# ── 字体工厂 ──
# 菜单默认字体是 SF Pro（比例字体），方块字符 █░│ 会回退到 Apple Symbols，
# 宽度与正文不一致，导致 ljust/rjust 补出来的列全是歪的。
# 因此方块段单独用等宽字体（Menlo），数字段用等宽数字字体，两者都能对齐。
_MONO_FONT_NAME = "Menlo"
_MONO_FONT_SIZE = 11.0


def _resolve_font(kind):
    """kind: None/'label' → 系统字体, 'bold' → 系统粗体, 'mono' → 等宽, 'digit' → 等宽数字"""
    if not HAS_APPKIT:
        return None
    if kind == "bold":
        return NSFont.boldSystemFontOfSize_(0)
    if kind == "mono":
        return NSFont.fontWithName_size_(_MONO_FONT_NAME, _MONO_FONT_SIZE)
    if kind == "digit":
        # 等宽数字：让 1:14:49 和 15:36 的冒号/数字占位一致
        return NSFont.monospacedDigitSystemFontOfSize_weight_(0, 0.0)
    return NSFont.systemFontOfSize_(0)


def _color_name_for_total_hours(hours):
    """按总时长返回色彩名"""
    if hours <= 0:
        return "label"
    if hours < 20:
        return "green"
    if hours < 40:
        return "yellow"
    if hours < 60:
        return "orange"
    return "red"


def _color_name_for_day_seconds(secs, limit_minutes):
    """按单日时长相对每日限额返回色彩名"""
    if secs <= 0:
        return "secondary"
    limit_sec = max(1, limit_minutes * 60)
    ratio = secs / limit_sec
    if ratio < 0.3:
        return "blue"
    if ratio < 0.6:
        return "green"
    if ratio < 1.0:
        return "yellow"
    if ratio < 1.5:
        return "orange"
    return "red"


def _set_attributed_title(menu_item, text, color_name=None, bold=False, secondary_label=False):
    """给 rumps MenuItem 设置带色/加粗的标题；缺 AppKit/NSMenuItem 时退化为纯文本"""
    # 不需要富文本：直接走纯文本
    if color_name is None and not bold and not secondary_label:
        menu_item.title = text
        return
    # 没有 AppKit 或没有 NSMenuItem 包装：退化为纯文本
    if not HAS_APPKIT:
        menu_item.title = text
        return
    nsitem = getattr(menu_item, "_menuitem", None)
    if nsitem is None or not hasattr(nsitem, "setAttributedTitle_"):
        menu_item.title = text
        return
    attrs = {}
    if bold:
        attrs[NSFontAttributeName] = NSFont.boldSystemFontOfSize_(0)
    chosen_name = color_name if color_name is not None else (
        "secondary" if secondary_label else None
    )
    if chosen_name is not None:
        chosen_color = _resolve_color(chosen_name)
        if chosen_color is not None:
            attrs[NSForegroundColorAttributeName] = chosen_color
    attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    nsitem.setAttributedTitle_(attr_str)


def _set_segmented_title(menu_item, segments):
    """把一行拆成多段富文本，每段可独立指定颜色和字体

    segments: [(text, color_name, font_kind), ...]
    典型用法是「日期 + 方块柱 + 时长」三段：柱子和时长用语义色，
    日期用 labelColor，这样深色窗口下每一段都清晰可见。
    无 AppKit 时退化为拼接纯文本。
    """
    plain = "".join(seg[0] for seg in segments)
    nsitem = getattr(menu_item, "_menuitem", None) if HAS_APPKIT else None
    if nsitem is None or not hasattr(nsitem, "setAttributedTitle_"):
        menu_item.title = plain
        return
    result = NSMutableAttributedString.alloc().initWithString_("")
    for text, color_name, font_kind in segments:
        if not text:
            continue
        attrs = {}
        font = _resolve_font(font_kind)
        if font is not None:
            attrs[NSFontAttributeName] = font
        if color_name is not None:
            color = _resolve_color(color_name)
            if color is not None:
                attrs[NSForegroundColorAttributeName] = color
        result.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        )
    nsitem.setAttributedTitle_(result)
    # 同步 .title，/api/debug/menu 等纯文本读取仍能拿到完整内容
    menu_item.title = plain


def format_duration(seconds):
    """将秒数格式化为 h:mm:ss 或 m:ss"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_minutes(minutes):
    """将分钟数格式化为可读字符串"""
    h = int(minutes // 60)
    m = int(minutes % 60)
    if h > 0 and m > 0:
        return f"{h}小时{m}分钟"
    elif h > 0:
        return f"{h}小时"
    return f"{m}分钟"


def _display_width(text):
    """估算字符串在菜单里的显示列宽，CJK/全角字符按 2 列算

    str.ljust/rjust 按字符个数补空格，对 "今天"(2 字 = 4 列) 和 "09/23"(5 字 = 5 列)
    会补出不同的宽度，导致日期列歪掉。这里统一按显示列宽来算。
    """
    width = 0
    for ch in text:
        # CJK 统一表意文字、全角标点、假名等
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def _pad_display(text, width, align="left"):
    """按显示列宽对齐（CJK 安全）"""
    pad = max(0, width - _display_width(text))
    if align == "right":
        return " " * pad + text
    return text + " " * pad


def shift_months(base_date, months_delta):
    """将日期按月偏移，返回目标月份的第一天"""
    month_index = (base_date.year * 12 + (base_date.month - 1)) + months_delta
    year = month_index // 12
    month = (month_index % 12) + 1
    return date(year, month, 1)


def get_frontmost_app():
    """获取当前前台活跃应用的名称和 Bundle ID"""
    if HAS_APPKIT:
        try:
            active = NSWorkspace.sharedWorkspace().frontmostApplication()
            return {
                "name": active.localizedName(),
                "bundle_id": active.bundleIdentifier() or "",
            }
        except Exception:
            pass
    # fallback: 用 osascript
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return {"name": result.stdout.strip(), "bundle_id": ""}
    except Exception:
        pass
    return {"name": "", "bundle_id": ""}


def get_active_window_title():
    """获取当前前台应用的窗口标题（用于检测Obsidian等笔记软件是否在运行AI插件）"""
    try:
        script = '''
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
            try
                tell process frontApp
                    set windowTitle to name of front window
                end tell
                return windowTitle
            on error
                return ""
            end try
        end tell
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return result.stdout.strip().lower()
    except Exception:
        pass
    return ""


_terminal_ai_cache = {
    "tool": None,
    "last_check": 0,
    "ttl": 3,  # 终端进程扫描缓存（秒）
}

# 终端里可能跑的 AI CLI。顺序敏感：更具体的（*-internal）必须排在前面，
# 否则 "claude-internal" 会被 "claude" 先匹配走。
TERMINAL_AI_TOOLS = (
    ("claude-internal", "Claude Internal"),
    ("claude internal", "Claude Internal"),
    ("claude", "Claude Code"),
    ("gemini-internal", "Gemini Internal"),
    ("gemini internal", "Gemini Internal"),
    ("gemini", "Gemini"),
    ("codex-internal", "Codex Internal"),
    ("codex internal", "Codex Internal"),
    ("codex", "Codex"),
    ("workbuddy", "WorkBuddy"),
    ("work buddy", "WorkBuddy"),
    ("kimi", "Kimi"),
)


def _match_terminal_ai_tool(cmd_str):
    """命令行 → 工具名；匹配不上返回 None"""
    for pattern, tool in TERMINAL_AI_TOOLS:
        if pattern in cmd_str:
            return tool
    return None


# 可执行名必须完全相等才认的工具。
# "agy" 只有三个字母，做子串匹配会误伤 legacy / agyl / 任何路径里含 agy
# 的东西；而真实会话的命令行就是光秃秃一个 "agy"，子串匹配反而抓不到。
AI_TOOL_EXACT_EXE = {
    "agy": "Antigravity",
}

# 后台常驻进程（更新器等），不是真实的 AI 会话。
# `agy --bg-updater` 的命令行里含 "antigravity-cli"，子串匹配会把它误判成
# Antigravity，而真正的两个 agy 会话反而认不出来——方向正好反了。
AI_TOOL_BG_FLAGS = ("--bg-updater",)

# 资源面板用的进程关键字（工具名, 关键字列表）。这里的"antigravity"只能认出
# GUI 版 Antigravity 应用——CLI 的可执行名是 agy，认不出来，见 AI_TOOL_EXACT_EXE。
ai_tool_patterns = [
    ('CodeBuddy', ['codebuddy', 'code buddy']),
    ('WorkBuddy', ['workbuddy', 'work buddy']),
    ('Cursor', ['cursor']),
    ('Claude Code', ['claude']),
    ('Claude Internal', ['claude-internal']),
    ('Gemini', ['gemini']),
    ('Gemini Internal', ['gemini-internal']),
    ('Codex', ['codex']),
    ('Codex Internal', ['codex-internal']),
    ('Kimi', ['kimi', 'kimi-client']),
    ('Antigravity', ['antigravity']),
]


def _is_background_helper(cmd_str):
    """是否是后台辅助进程（更新器等），这类不该计入使用时间"""
    return any(flag in cmd_str for flag in AI_TOOL_BG_FLAGS)


def _exact_exe_tool(name, cmdline):
    """按可执行名精确匹配 AI 工具，匹配不上返回 None

    优先用 cmdline[0] 的 basename——真实命令行是 /Users/dang/.local/bin/agy，
    psutil 的 name 字段在不同环境下时而是 agy 时而是完整路径。
    """
    base = name
    if cmdline:
        candidate = os.path.basename(cmdline[0])
        if candidate:
            base = candidate
    return AI_TOOL_EXACT_EXE.get(base)


def _pick_terminal_ai_tool(candidates):
    """从候选里挑一个工具名，结果必须稳定。

    candidates: [(工具名, 进程启动时间), ...]
    规则：取启动时间最新的（通常是最后开的那个）；时间相同再按名字排，
    保证同一组候选无论扫描顺序如何都得出同一个结果。
    """
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c[1], c[0]))
    return best[0]


def _tty_foreground_pgids():
    """返回 {tty 名: 该 tty 的前台进程组 pid}

    macOS 禁止对别人的 tty 调 TIOCGPGRP（ENOTTY），只有 ps 的 tpgid 列可用。
    用来判断"这个 CLI 是不是它所在终端的前台进程"——你切到别的窗格时，
    它就不该再被算作正在使用。
    """
    mapping = {}
    try:
        result = subprocess.run(
            ["ps", "-axo", "tty=,tpgid="],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return mapping
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                mapping[parts[0]] = int(parts[1])
            except ValueError:
                continue
    except Exception:
        pass
    return mapping


def find_terminal_ai_tools():
    """扫描所有终端里跑的 AI CLI，返回 [(工具名, 启动时间), ...]

    优先只认"自己 tty 的前台进程"；万一一个都没有（ps 拿不到、或某些 CLI
    不占前台），退回"只要挂在 tty 上就算"，避免把原来能检出的情况判没了。
    """
    foreground = _tty_foreground_pgids()
    in_foreground = []
    merely_has_tty = []

    for proc in psutil.process_iter(['name', 'cmdline', 'ppid', 'status']):
        try:
            cmdline = proc.info.get('cmdline') or []
            cmd_str = ' '.join(cmdline).lower()
            if 'ai_time_guard' in cmd_str:
                continue
            # 后台更新器之类的常驻进程不算真实会话
            if _is_background_helper(cmd_str):
                continue

            tty = None
            try:
                tty = proc.terminal()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            name = (proc.info.get('name') or '').lower()
            tool = (
                _match_terminal_ai_tool(cmd_str)
                or _match_terminal_ai_tool(name)
                or _exact_exe_tool(name, cmdline)
            )
            if not tool:
                continue

            # node 包装的 claude 进程可能没挂 tty，靠可执行名兜底
            is_node_claude = (
                name == 'node' and cmdline
                and cmdline[0].lower() == 'claude'
            )
            if not tty:
                if is_node_claude:
                    merely_has_tty.append((tool, proc.create_time()))
                continue

            try:
                started = proc.create_time()
            except Exception:
                started = 0.0

            entry = (tool, started)
            tty_name = tty.split("/")[-1]
            if foreground.get(tty_name) == proc.pid:
                in_foreground.append(entry)
            else:
                merely_has_tty.append(entry)

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except (SystemError, OSError, RuntimeError):
            continue

    picked = in_foreground if in_foreground else merely_has_tty
    # 同一工具可能开了多个进程（主进程 + 子进程），去重但保留最新启动时间
    latest = {}
    for tool, started in picked:
        if tool not in latest or started > latest[tool]:
            latest[tool] = started
    return [(tool, started) for tool, started in latest.items()]


def check_terminal_has_ai_tool():
    """检查终端中是否有活跃的 AI CLI 工具进程，返回工具名或 None"""
    now = time.time()
    if now - _terminal_ai_cache["last_check"] < _terminal_ai_cache["ttl"]:
        return _terminal_ai_cache["tool"]

    tool = _pick_terminal_ai_tool(find_terminal_ai_tools())

    _terminal_ai_cache["tool"] = tool
    _terminal_ai_cache["last_check"] = now
    return tool


# 资源数据缓存
_resource_cache = {
    'data': [],
    'last_update': 0,
    'cache_ttl': 5,  # 缓存有效期 5 秒
}


def get_ai_tool_resource_usage(use_cache=True):
    """获取所有 AI 工具的资源使用情况（CPU、内存）"""
    global _resource_cache
    
    # 检查缓存是否有效
    now = time.time()
    if use_cache and now - _resource_cache['last_update'] < _resource_cache['cache_ttl']:
        return _resource_cache['data']
    
    resources = []
    
    # 定义要监控的进程关键字
    seen_pids = set()
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent', 'memory_info', 'memory_percent']):
        try:
            pid = proc.info['pid']
            if pid in seen_pids:
                continue
                
            cmdline = proc.info.get('cmdline') or []
            cmd_str = ' '.join(cmdline).lower()
            name = (proc.info.get('name') or '').lower()
            
            # 跳过自身
            if 'ai_time_guard' in cmd_str:
                continue

            # 后台更新器之类的常驻进程，不是真实会话
            if _is_background_helper(cmd_str):
                continue

            # 匹配 AI 工具
            matched_tool = None
            for tool_name, patterns in ai_tool_patterns:
                for pattern in patterns:
                    if pattern in cmd_str or pattern in name:
                        matched_tool = tool_name
                        break
                if matched_tool:
                    break

            # 长关键字表抓不到的短名字工具（如 agy），按可执行名精确匹配兜底
            if not matched_tool:
                matched_tool = _exact_exe_tool(name, cmdline)

            if matched_tool:
                seen_pids.add(pid)
                # 使用非阻塞方式获取 CPU（首次可能为 0，但响应快）
                try:
                    proc_obj = psutil.Process(pid)
                    cpu_percent = proc_obj.cpu_percent(interval=None)  # 非阻塞
                    memory_mb = proc_obj.memory_info().rss / 1024 / 1024
                    memory_percent = proc_obj.memory_percent()

                    resources.append({
                        'tool': matched_tool,
                        'pid': pid,
                        'cpu_percent': round(cpu_percent, 1),
                        'memory_mb': round(memory_mb, 1),
                        'memory_percent': round(memory_percent, 2),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                    break
                    
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except (SystemError, OSError, RuntimeError):
            continue
    
    # 更新缓存
    _resource_cache['data'] = resources
    _resource_cache['last_update'] = now
    
    return resources


# GUI 应用关键字（只在前台时才计时）
FOREGROUND_APP_KEYWORDS = [
    "codebuddy", "code buddy",
    "workbuddy", "work buddy",
    "cursor",
    "codex",
    "kimi", "kimi-client",
    "antigravity",
]

# Obsidian + AI 插件检测配置
OBSIDIAN_AI_KEYWORDS = ["claude", "chat", "ai assistant", "copilot", "gpt"]  # 窗口标题中可能出现的AI关键词

# 笔记应用（需要进一步检查是否在运行AI插件）
NOTE_APP_KEYWORDS = [
    "obsidian",
    "notion",
    "logseq",
]

# 终端应用名和 bundle ID（前台是终端时，进一步检查是否在跑 claude CLI）
TERMINAL_APP_NAMES = [
    "terminal", "iterm", "iterm2", "warp", "alacritty", "kitty",
    "hyper", "tabby", "ghostty", "rio", "wezterm", "contour",
    "foot", "zellij", "cool-retro-term", "terminus",
]
# Electron 终端（Tabby/Hyper 等）在 System Events 可能报告为 electron
TERMINAL_BUNDLE_KEYWORDS = [
    "com.apple.terminal", "com.googlecode.iterm2", "dev.warp.warp",
    "org.tabby", "net.kovidgoyal.kitty", "io.alacritty",
    "co.zeit.hyper", "com.mitchellh.ghostty", "com.github.wez.wezterm",
]


def check_ai_active(config):
    """
    智能检测 AI 工具是否正在被"真正使用"：

    1. GUI 应用（CodeBuddy）：只有在前台（当前活跃窗口）才计时
       - 开着但切到别的应用 → 不计时
       - 最小化 → 不计时

    2. CLI 工具（claude）：终端在前台 + claude 进程存在 → 计时
       - 终端在后台 → 不计时（你在做别的事）
       - 终端在前台但没跑 claude → 不计时

    返回: (is_active, detail_str)
    """
    front = get_frontmost_app()
    front_name = front["name"].lower()
    front_bundle = front["bundle_id"].lower()

    # 1) GUI AI 工具在前台
    for kw in FOREGROUND_APP_KEYWORDS:
        if kw in front_name or kw in front_bundle:
            # 根据匹配到的关键字返回对应的工具名
            if "codebuddy" in kw or "code buddy" in kw:
                return True, "CodeBuddy（前台）"
            elif "workbuddy" in kw or "work buddy" in kw:
                return True, "WorkBuddy（前台）"
            elif "cursor" in kw:
                return True, "Cursor（前台）"
            elif "codex" in kw:
                return True, "Codex（前台）"
            elif "kimi" in kw:
                return True, "Kimi（前台）"
            elif "antigravity" in kw:
                return True, "Antigravity（前台）"
            else:
                return True, "其他 AI 工具（前台）"

    # 2) 笔记应用在前台 → 检查窗口标题是否包含AI关键词（如Obsidian+Claudian）
    for note_kw in NOTE_APP_KEYWORDS:
        if note_kw in front_name.lower():
            window_title = get_active_window_title()
            for ai_kw in OBSIDIAN_AI_KEYWORDS:
                if ai_kw in window_title:
                    return True, f"{front_name.title()} + AI（前台）"
            # 如果在笔记应用中但未检测到AI关键词，不计时
            return False, ""

    # 3) 终端在前台 → 检查是否在跑 claude CLI
    is_terminal = any(t in front_name for t in TERMINAL_APP_NAMES)
    # 也通过 bundle ID 匹配（更可靠，避免 Electron 终端名称不一致）
    if not is_terminal and front_bundle:
        is_terminal = any(b in front_bundle for b in TERMINAL_BUNDLE_KEYWORDS)
    # Electron 终端 fallback：名为 electron 但有 claude 进程
    if not is_terminal and ("electron" in front_name):
        is_terminal = True
    if is_terminal:
        # 检查各种 CLI 工具
        tool_name = check_terminal_has_ai_tool()
        if tool_name:
            return True, f"{tool_name}（终端前台）"

    return False, ""


def infer_tool_name(active_detail):
    """从活跃明细中提取统一的工具名。"""
    tool_name = "其他"
    if "CodeBuddy" in active_detail:
        tool_name = "CodeBuddy"
    elif "WorkBuddy" in active_detail:
        tool_name = "WorkBuddy"
    elif "Cursor" in active_detail:
        tool_name = "Cursor"
    elif "Claude Internal" in active_detail:
        tool_name = "Claude Internal"
    elif "Claude" in active_detail:
        tool_name = "Claude Code"
    elif "Gemini Internal" in active_detail:
        tool_name = "Gemini Internal"
    elif "Gemini" in active_detail:
        tool_name = "Gemini"
    elif "Codex Internal" in active_detail:
        tool_name = "Codex Internal"
    elif "Codex" in active_detail:
        tool_name = "Codex"
    elif "Kimi" in active_detail:
        tool_name = "Kimi"
    elif "Antigravity" in active_detail:
        tool_name = "Antigravity"
    elif "AI" in active_detail:
        if "Obsidian" in active_detail:
            tool_name = "Obsidian + AI"
        elif "Notion" in active_detail:
            tool_name = "Notion + AI"
        else:
            tool_name = "笔记 + AI"
    return tool_name


# ─── 内置 HTTP 报告服务器 ──────────────────────────────────

REPORT_PORT = 19527
APP_DIR = Path(__file__).parent.resolve()
_app_instance = None  # 全局引用，供 handler 访问实时状态


class ReportHandler(SimpleHTTPRequestHandler):
    """处理报告页面和 API 请求"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/' or path == '/index.html':
            # 直接读取 report.html 返回
            html_path = APP_DIR / 'report.html'
            try:
                content = html_path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
            return

        if path == '/share' or path == '/share.html':
            html_path = APP_DIR / 'share.html'
            try:
                content = html_path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
            return

        # 浏览器经常会直接请求 /favicon.ico，统一映射到项目图标
        if path == '/favicon.ico' or path == '/favicon.svg':
            favicon = APP_DIR / 'assets' / 'favicon.svg'
            if favicon.exists():
                try:
                    content = favicon.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/svg+xml')
                    self.send_header('Content-Length', str(len(content)))
                    self.send_header('Cache-Control', 'public, max-age=300')
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self.send_error(500, str(e))
            else:
                self.send_error(404)
            return

        if path.startswith('/assets/'):
            asset_path = (APP_DIR / path.lstrip('/')).resolve()
            assets_root = (APP_DIR / 'assets').resolve()
            if not asset_path.is_relative_to(assets_root):
                self.send_error(403)
                return
            if not asset_path.exists() or not asset_path.is_file():
                self.send_error(404)
                return
            try:
                content = asset_path.read_bytes()
                if asset_path.suffix == '.svg':
                    content_type = 'image/svg+xml'
                elif asset_path.suffix == '.png':
                    content_type = 'image/png'
                elif asset_path.suffix == '.ico':
                    content_type = 'image/x-icon'
                else:
                    content_type = 'application/octet-stream'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
            return

        if path == '/api/report':
            self.send_json_response(self.build_report())
            return

        if path == '/api/config':
            global _app_instance
            if _app_instance:
                config = _app_instance.config.copy()
            else:
                config = load_config()
            self.send_json_response(config)
            return

        if path == '/api/today':
            self.send_json_response(self.build_today())
            return

        if path == '/api/debug/menu':
            app = _app_instance
            if not app:
                self.send_json_response({"error": "app not running"}, status=503)
                return
            titles = {
                "status": app.status_item.title,
                "today": app.today_item.title,
                "limit": app.limit_item.title,
                "stats_header": app.stats_header_item.title,
            }
            for i, item in enumerate(app.stats_day_items):
                titles[f"stats_day_{i}"] = item.title
            titles["stand_up_menu"] = app.stand_up_menu.title
            titles["stand_up_toggle"] = app.stand_up_toggle_item.title
            titles["stand_up_enabled"] = app.stand_up_enabled
            titles["stand_up_last_time"] = app.stand_up_last_reminder_time
            titles["countdown_menu"] = app.countdown_menu.title
            titles["countdown_action"] = app.countdown_action_item.title
            titles["countdown_duration_minutes"] = app.countdown_duration_minutes
            titles["countdown_state"] = app.countdown_state
            titles["menubar_title"] = app.title
            self.send_json_response(titles)
            return

        if path.startswith('/api/daily/'):
            date_str = path[len('/api/daily/'):]
            self.send_json_response(self.build_daily(date_str))
            return

        if path == '/api/summary':
            try:
                days = int(query.get('days', ['7'])[0])
            except ValueError:
                days = 7
            days = max(1, min(days, 365))  # 限制在 1-365 天
            self.send_json_response(self.build_summary(days))
            return

        if path == '/api/summary/monthly':
            try:
                months = int(query.get('months', ['12'])[0])
            except ValueError:
                months = 12
            months = max(1, min(months, 120))
            self.send_json_response(self.build_monthly_summary(months))
            return

        if path == '/api/summary/yearly':
            try:
                years = int(query.get('years', ['3'])[0])
            except ValueError:
                years = 3
            years = max(1, min(years, 20))
            self.send_json_response(self.build_yearly_summary(years))
            return

        if path == '/api/generate-image':
            # 生成分享图片
            import os
            # 临时清除代理
            for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
                os.environ.pop(k, None)

            try:
                from playwright.sync_api import sync_playwright

                image_type = query.get('type', ['share'])[0]  # share, card

                # 强制禁用代理
                browser_args = [
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--proxy-server=direct://',
                    '--no-proxy-server=127.0.0.1,localhost',
                ]

                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=browser_args,
                    )
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{REPORT_PORT}/share.html', timeout=30000)
                    page.wait_for_timeout(3000)

                    if image_type == 'card':
                        card = page.locator('#shareCard')
                        screenshot = card.screenshot(scale='device', type='png')
                    else:
                        screenshot = page.screenshot(scale='device', type='png')

                    browser.close()

                # 返回图片
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(screenshot)))
                self.send_header('Content-Disposition', f'attachment; filename="ai-time-guard-{date.today()}.png"')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(screenshot)
                return
            except Exception as e:
                import traceback
                self.send_json_response({"success": False, "error": str(e) + "\n" + traceback.format_exc()[:500]}, status=500)
                return

        self.send_error(404)

    def do_OPTIONS(self):
        """处理 CORS 预检请求"""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def do_POST(self):
        if self.path == '/api/config':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                new_config = json.loads(body)
                
                # 加载现有配置并更新
                config = load_config()
                config.update(new_config)
                save_config(config)
                
                # 更新运行中的配置
                global _app_instance
                if _app_instance:
                    _app_instance.apply_config(config)
                
                self.send_json_response({"success": True, "config": config})
            except Exception as e:
                self.send_json_response({"success": False, "error": str(e)}, status=400)
            return

        self.send_error(404)

    def build_today(self):
        """构建今日数据摘要（轻量级端点）"""
        global _app_instance
        config = _app_instance.config.copy() if _app_instance else load_config()
        today_key = str(date.today())
        today_seconds = 0
        today_tools = {}
        is_active = False
        detail = ""

        if _app_instance:
            today_seconds = round(_app_instance.today_seconds, 1)
            today_tools = {k: round(v, 1) for k, v in _app_instance.today_tools.items()}
            is_active = _app_instance.is_ai_active
            detail = _app_instance.active_detail
        else:
            history = load_history()
            day_data = history.get(today_key, {"total": 0, "tools": {}})
            if isinstance(day_data, (int, float)):
                today_seconds = round(day_data, 1)
                today_tools = {}
            else:
                today_seconds = round(day_data.get("total", 0), 1)
                today_tools = {k: round(v, 1) for k, v in day_data.get("tools", {}).items()}

        return {
            "date": today_key,
            "total_seconds": today_seconds,
            "tools": today_tools,
            "limit_minutes": config.get("daily_limit_minutes", 180),
            "is_active": is_active,
            "detail": detail,
        }

    def build_daily(self, date_str):
        """构建指定日期的数据"""
        # 验证日期格式
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return {"error": "Invalid date format. Use YYYY-MM-DD."}

        global _app_instance
        config = _app_instance.config.copy() if _app_instance else load_config()
        history = dict(_app_instance.history) if _app_instance else load_history()
        today_key = str(date.today())

        # 如果查询的是今天，优先使用实时数据
        if date_str == today_key and _app_instance:
            total = round(_app_instance.today_seconds, 1)
            tools = {k: round(v, 1) for k, v in _app_instance.today_tools.items()}
        else:
            day_data = history.get(date_str, {"total": 0, "tools": {}})
            if isinstance(day_data, (int, float)):
                total = round(day_data, 1)
                tools = {}
            else:
                total = round(day_data.get("total", 0), 1)
                tools = {k: round(v, 1) for k, v in day_data.get("tools", {}).items()}

        return {
            "date": date_str,
            "total_seconds": total,
            "tools": tools,
            "limit_minutes": config.get("daily_limit_minutes", 180),
        }

    def build_summary(self, days):
        """构建多日汇总摘要"""
        global _app_instance
        config = _app_instance.config.copy() if _app_instance else load_config()
        history = dict(_app_instance.history) if _app_instance else load_history()
        today_obj = date.today()
        today_key = str(today_obj)
        limit_minutes = config.get("daily_limit_minutes", 180)

        # 确保 history 有实时的今日数据
        if _app_instance:
            history[today_key] = {
                "total": _app_instance.today_seconds,
                "tools": _app_instance.today_tools,
            }

        # 收集指定天数的数据
        total_seconds = 0
        active_days = 0
        max_day = {"date": "", "seconds": 0}
        all_tools = {}
        daily = []

        for i in range(days - 1, -1, -1):  # 从最早到最近
            d = today_obj - timedelta(days=i)
            d_str = str(d)
            day_data = history.get(d_str, {"total": 0, "tools": {}})

            if isinstance(day_data, (int, float)):
                day_total = day_data
                day_tools = {}
            else:
                day_total = day_data.get("total", 0)
                day_tools = day_data.get("tools", {})

            total_seconds += day_total
            if day_total > 60:
                active_days += 1
            if day_total > max_day["seconds"]:
                max_day = {"date": d_str, "seconds": round(day_total, 1)}

            for tool_name, secs in day_tools.items():
                all_tools[tool_name] = all_tools.get(tool_name, 0) + secs

            daily.append({
                "date": d_str,
                "total": round(day_total, 1),
                "tools": {k: round(v, 1) for k, v in day_tools.items()},
            })

        start_date = str(today_obj - timedelta(days=days - 1))
        end_date = today_key

        return {
            "start_date": start_date,
            "end_date": end_date,
            "days": days,
            "total_seconds": round(total_seconds, 1),
            "daily_avg_seconds": round(total_seconds / days, 1) if days > 0 else 0,
            "active_days": active_days,
            "max_day": max_day,
            "tools": {k: round(v, 1) for k, v in sorted(all_tools.items(), key=lambda x: -x[1])},
            "daily": daily,
            "limit_minutes": limit_minutes,
        }

    def build_monthly_summary(self, months):
        """按月聚合统计（用于 1-2 年趋势）"""
        global _app_instance
        config = _app_instance.config.copy() if _app_instance else load_config()
        history = dict(_app_instance.history) if _app_instance else load_history()
        today_obj = date.today()
        today_key = str(today_obj)
        limit_seconds = config.get("daily_limit_minutes", 180) * 60

        if _app_instance:
            history[today_key] = {
                "total": _app_instance.today_seconds,
                "tools": _app_instance.today_tools,
            }

        monthly = []
        all_tools = {}
        total_seconds = 0
        max_month = {"month": "", "seconds": 0}

        for idx in range(months - 1, -1, -1):
            month_start = shift_months(today_obj, -idx)
            month_end = shift_months(month_start, 1) - timedelta(days=1)
            if month_end > today_obj:
                month_end = today_obj

            cur = month_start
            month_total = 0
            active_days = 0
            over_limit_days = 0
            month_tools = {}

            while cur <= month_end:
                day_data = history.get(str(cur), {"total": 0, "tools": {}})
                if isinstance(day_data, (int, float)):
                    day_total = float(day_data)
                    day_tools = {}
                else:
                    day_total = float(day_data.get("total", 0))
                    day_tools = day_data.get("tools", {})

                month_total += day_total
                if day_total > 60:
                    active_days += 1
                if day_total > limit_seconds:
                    over_limit_days += 1
                for tool_name, secs in day_tools.items():
                    month_tools[tool_name] = month_tools.get(tool_name, 0) + secs
                cur += timedelta(days=1)

            month_key = f"{month_start.year}-{month_start.month:02d}"
            total_seconds += month_total
            if month_total > max_month["seconds"]:
                max_month = {"month": month_key, "seconds": round(month_total, 1)}
            for tool_name, secs in month_tools.items():
                all_tools[tool_name] = all_tools.get(tool_name, 0) + secs

            days_count = (month_end - month_start).days + 1 if month_end >= month_start else 0
            monthly.append({
                "month": month_key,
                "total_seconds": round(month_total, 1),
                "avg_daily_seconds": round(month_total / days_count, 1) if days_count > 0 else 0,
                "days_count": days_count,
                "active_days": active_days,
                "over_limit_days": over_limit_days,
                "tools": {k: round(v, 1) for k, v in sorted(month_tools.items(), key=lambda x: -x[1])},
            })

        start_month = monthly[0]["month"] if monthly else ""
        end_month = monthly[-1]["month"] if monthly else ""
        return {
            "months": months,
            "start_month": start_month,
            "end_month": end_month,
            "total_seconds": round(total_seconds, 1),
            "avg_monthly_seconds": round(total_seconds / months, 1) if months > 0 else 0,
            "max_month": max_month,
            "tools": {k: round(v, 1) for k, v in sorted(all_tools.items(), key=lambda x: -x[1])},
            "monthly": monthly,
            "limit_minutes": config.get("daily_limit_minutes", 180),
        }

    def build_yearly_summary(self, years):
        """按年聚合统计（用于多年趋势）"""
        global _app_instance
        config = _app_instance.config.copy() if _app_instance else load_config()
        history = dict(_app_instance.history) if _app_instance else load_history()
        today_obj = date.today()
        today_key = str(today_obj)
        limit_seconds = config.get("daily_limit_minutes", 180) * 60

        if _app_instance:
            history[today_key] = {
                "total": _app_instance.today_seconds,
                "tools": _app_instance.today_tools,
            }

        yearly = []
        all_tools = {}
        total_seconds = 0
        max_year = {"year": "", "seconds": 0}

        for year in range(today_obj.year - years + 1, today_obj.year + 1):
            start_day = date(year, 1, 1)
            end_day = date(year, 12, 31)
            if year == today_obj.year:
                end_day = today_obj

            cur = start_day
            year_total = 0
            active_days = 0
            over_limit_days = 0
            year_tools = {}
            days_count = 0

            while cur <= end_day:
                days_count += 1
                day_data = history.get(str(cur), {"total": 0, "tools": {}})
                if isinstance(day_data, (int, float)):
                    day_total = float(day_data)
                    day_tools = {}
                else:
                    day_total = float(day_data.get("total", 0))
                    day_tools = day_data.get("tools", {})

                year_total += day_total
                if day_total > 60:
                    active_days += 1
                if day_total > limit_seconds:
                    over_limit_days += 1
                for tool_name, secs in day_tools.items():
                    year_tools[tool_name] = year_tools.get(tool_name, 0) + secs
                cur += timedelta(days=1)

            total_seconds += year_total
            if year_total > max_year["seconds"]:
                max_year = {"year": str(year), "seconds": round(year_total, 1)}
            for tool_name, secs in year_tools.items():
                all_tools[tool_name] = all_tools.get(tool_name, 0) + secs

            yearly.append({
                "year": str(year),
                "total_seconds": round(year_total, 1),
                "avg_daily_seconds": round(year_total / days_count, 1) if days_count > 0 else 0,
                "days_count": days_count,
                "active_days": active_days,
                "over_limit_days": over_limit_days,
                "tools": {k: round(v, 1) for k, v in sorted(year_tools.items(), key=lambda x: -x[1])},
            })

        return {
            "years": years,
            "start_year": str(today_obj.year - years + 1),
            "end_year": str(today_obj.year),
            "total_seconds": round(total_seconds, 1),
            "avg_yearly_seconds": round(total_seconds / years, 1) if years > 0 else 0,
            "max_year": max_year,
            "tools": {k: round(v, 1) for k, v in sorted(all_tools.items(), key=lambda x: -x[1])},
            "yearly": yearly,
            "limit_minutes": config.get("daily_limit_minutes", 180),
        }

    def build_report(self):
        global _app_instance
        status = {
            "is_active": False,
            "is_monitoring": True,
            "detail": "",
            "today_seconds": 0,
            "today_tools": {},
            "resources": [],
        }
        if _app_instance:
            config = _app_instance.config.copy()
            history = dict(_app_instance.history)
            today_tools = dict(_app_instance.today_tools)
            status["is_active"] = _app_instance.is_ai_active
            status["is_monitoring"] = _app_instance.is_monitoring
            status["detail"] = _app_instance.active_detail
            status["today_seconds"] = _app_instance.today_seconds
            status["today_tools"] = today_tools
            # 获取资源使用情况
            status["resources"] = get_ai_tool_resource_usage()
            # 确保 history 有实时的今日数据
            history[str(date.today())] = {
                "total": _app_instance.today_seconds,
                "tools": today_tools,
            }
        else:
            history = load_history()
            config = load_config()
        return {"history": history, "config": config, "status": status}

    def send_json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默日志


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器，支持同时处理多个请求"""
    daemon_threads = True


def start_report_server():
    """在后台线程启动报告服务器"""
    try:
        server = ThreadingHTTPServer(('127.0.0.1', REPORT_PORT), ReportHandler)
        server.serve_forever()
    except OSError as e:
        print(f"Report server failed to start: {e}")


# ─── 主应用 ───────────────────────────────────────────────

class AITimeGuardApp(rumps.App):
    def __init__(self):
        super().__init__(
            APP_NAME,
            icon=None,
            quit_button=None,
        )
        self.config = load_config()
        self.history = load_history()
        self.today_key = str(date.today())
        today_data = self.history.get(self.today_key, {"total": 0, "tools": {}})
        self.today_seconds = today_data.get("total", 0)
        self.today_tools = today_data.get("tools", {})  # {工具名: 秒数}
        self.is_monitoring = True
        self.is_ai_active = False
        self.active_detail = ""
        self.last_check_time = time.time()
        self.last_warning_time = 0
        self.warning_sent = False
        self.limit_warning_sent = False
        self.last_periodic_alert_usage_seconds = self.today_seconds  # 按活跃时长触发周期提醒
        self.last_history_save_time = time.time()     # 历史数据写入节流

        # 倒计时状态（运行时不依赖 AI 监控是否暂停）
        self.countdown_duration_seconds = int(self.config.get("countdown_duration_seconds", 180))
        self.countdown_duration_minutes = max(1, round(self.countdown_duration_seconds / 60))
        self.countdown_remaining_seconds = 0
        self.countdown_state = "idle"  # idle | running | finished
        self.countdown_timer = None

        # 45 分钟站立提醒状态（独立于 AI 监控，到点系统通知）
        self._restore_stand_up_state()

        # 启动一次性诊断：确认菜单栏状态项是否创建并可见
        self.debug_timer = rumps.Timer(self.debug_status_item, 3)
        self.debug_timer.start()

        # 注册全局引用并启动报告服务器
        global _app_instance
        _app_instance = self
        server_thread = threading.Thread(target=start_report_server, daemon=True)
        server_thread.start()

        # 构建菜单
        self.status_item = rumps.MenuItem("状态: 检测中...")
        self.today_item = rumps.MenuItem(f"今日: {format_duration(self.today_seconds)}")
        self.limit_item = rumps.MenuItem(
            f"限额: {format_minutes(self.config['daily_limit_minutes'])}"
        )

        # 近 7 天 AI 使用量可视化（电量监测样式）
        self.stats_header_item = rumps.MenuItem("📊 近 7 天           0:00")
        self.stats_day_items = []
        for _ in range(7):
            self.stats_day_items.append(rumps.MenuItem("  ·"))

        self.separator1 = rumps.separator

        self.pause_item = rumps.MenuItem("暂停监控", callback=self.toggle_monitoring)
        self.separator2 = rumps.separator

        # 限额设置子菜单
        self.limit_menu = rumps.MenuItem("设置每日限额")
        for mins in [60, 90, 120, 150, 180, 240, 300, 360, 480]:
            label = format_minutes(mins)
            item = rumps.MenuItem(label, callback=self.set_limit)
            item._mins = mins
            if mins == self.config['daily_limit_minutes']:
                item.state = True
            self.limit_menu.add(item)

        # 定时弹窗间隔子菜单
        self.periodic_menu = rumps.MenuItem("定时弹窗提醒间隔")
        for mins in [15, 20, 30, 45, 60, 90, 120]:
            label = format_minutes(mins)
            item = rumps.MenuItem(label, callback=self.set_periodic_interval)
            item._mins = mins
            if mins == self.config.get('periodic_alert_minutes', 30):
                item.state = True
            self.periodic_menu.add(item)

        self.periodic_enabled_item = rumps.MenuItem(
            "启用定时弹窗提醒",
            callback=self.toggle_periodic_alert
        )
        self.periodic_enabled_item.state = self.config.get("periodic_alert_enabled", True)

        self.strict_item = rumps.MenuItem(
            "严格模式（超限持续提醒）",
            callback=self.toggle_strict
        )
        self.strict_item.state = self.config.get("strict_mode", False)

        self.reset_item = rumps.MenuItem("重置今日计时", callback=self.reset_today)
        self.separator3 = rumps.separator

        self.history_item = rumps.MenuItem("最近7天统计", callback=self.show_history)
        self.report_item = rumps.MenuItem("查看使用报告", callback=self.open_report)
        self.separator4 = rumps.separator

        # ── 新增：可配置时长的倒计时 ──
        self.countdown_menu = rumps.MenuItem(f"🕒 {self.countdown_duration_minutes} 分钟倒计时")
        self.countdown_action_item = rumps.MenuItem(
            f"开始 {self.countdown_duration_minutes:02d}:00 倒计时",
            callback=self.toggle_countdown,
        )
        self.countdown_menu.add(self.countdown_action_item)
        self.countdown_duration_submenu = rumps.MenuItem("选择时长")
        for mins in COUNTDOWN_DURATION_CHOICES:
            item = rumps.MenuItem(
                f"{mins} 分钟", callback=self.set_countdown_duration
            )
            item._mins = mins
            item.state = (mins == self.countdown_duration_minutes)
            self.countdown_duration_submenu.add(item)
        self.countdown_menu.add(rumps.separator)
        self.countdown_menu.add(self.countdown_duration_submenu)

        # ── 新增：站立提醒 ──
        self.stand_up_menu = rumps.MenuItem("🧍 站立提醒")
        self.stand_up_toggle_item = rumps.MenuItem(
            "禁用站立提醒" if self.stand_up_enabled else "启用站立提醒",
            callback=self.toggle_stand_up,
        )
        self.stand_up_toggle_item.state = self.stand_up_enabled
        self.stand_up_menu.add(self.stand_up_toggle_item)

        self.stand_up_interval_submenu = rumps.MenuItem("调整间隔")
        for mins in [30, 45, 60, 90, 120]:
            item = rumps.MenuItem(f"{mins} 分钟", callback=self.set_stand_up_interval)
            item._mins = mins
            if mins == self.stand_up_interval_minutes:
                item.state = True
            self.stand_up_interval_submenu.add(item)
        self.stand_up_menu.add(self.stand_up_interval_submenu)

        self.stand_up_test_item = rumps.MenuItem(
            "立即提醒一次", callback=self.test_stand_up_reminder
        )
        self.stand_up_menu.add(self.stand_up_test_item)

        self.stand_up_separator2 = rumps.separator
        self.stand_up_menu.add(self.stand_up_separator2)

        # 菜单栏标题显示方式子菜单
        self.stand_up_title_mode_submenu = rumps.MenuItem("菜单栏显示方式")
        for mode, label in (
            ("final5", "仅剩 5 分钟时显示"),
            ("always", "始终显示倒计时"),
        ):
            item = rumps.MenuItem(label, callback=self.set_stand_up_title_mode)
            item._mode = mode
            if mode == self.stand_up_title_mode:
                item.state = True
            self.stand_up_title_mode_submenu.add(item)
        self.stand_up_menu.add(self.stand_up_title_mode_submenu)

        self.separator5 = rumps.separator

        self.quit_item = rumps.MenuItem("退出", callback=self.quit_app)

        self.menu = [
            self.status_item,
            self.today_item,
            self.limit_item,
            self.stats_header_item,
            *self.stats_day_items,
            self.separator1,
            self.pause_item,
            self.separator2,
            self.limit_menu,
            self.periodic_menu,
            self.periodic_enabled_item,
            self.strict_item,
            self.reset_item,
            self.separator3,
            self.history_item,
            self.report_item,
            self.separator4,
            self.countdown_menu,
            self.stand_up_menu,
            self.separator5,
            self.quit_item,
        ]

        self.update_title()

        # 启动定时检测
        self.timer = rumps.Timer(self.on_tick, self.config["check_interval_seconds"])
        self.timer.start()

    def update_title(self):
        """更新菜单栏显示的标题
        - 倒计时进行中时顶掉所有内容，显示 ⏱MM:SS
        - 站立提醒启用时，把"🧍MM:SS"加在前面作为提醒前缀
        - 其余场景显示 AI 时间状态（PAUSE/OVER/AI/IDLE）
        """
        today_min = self.today_seconds / 60
        limit_min = self.config["daily_limit_minutes"]

        if self.countdown_state == "running":
            mins = self.countdown_remaining_seconds // 60
            secs = self.countdown_remaining_seconds % 60
            self.title = f"⏱{mins:02d}:{secs:02d}"
            return

        # 站立提醒倒计时作为前缀（启用时按 title_mode 规则显示，倒计时不运行时才出现）
        stand_up_prefix = ""
        if self.stand_up_enabled and self.stand_up_last_reminder_time > 0:
            remaining_sec = max(
                0,
                int(self.stand_up_interval_minutes * 60
                    - (time.time() - self.stand_up_last_reminder_time)),
            )
            show_prefix = (
                self.stand_up_title_mode == "always"
                or remaining_sec <= 5 * 60
            )
            if remaining_sec > 0 and show_prefix:
                sm = remaining_sec // 60
                ss = remaining_sec % 60
                stand_up_prefix = f"🧍{sm:02d}:{ss:02d} "
            elif remaining_sec == 0 and self.stand_up_title_mode == "always":
                # 已到点时只在"始终显示"模式下提醒
                stand_up_prefix = "🧍⏰ "

        if not self.is_monitoring:
            self.title = f"{stand_up_prefix}PAUSE {format_duration(self.today_seconds)}"
        elif self.is_ai_active:
            if today_min >= limit_min:
                self.title = f"{stand_up_prefix}OVER {format_duration(self.today_seconds)}"
            else:
                self.title = f"{stand_up_prefix}AI {format_duration(self.today_seconds)}"
        else:
            self.title = f"{stand_up_prefix}IDLE {format_duration(self.today_seconds)}"

    def debug_status_item(self, _):
        """记录一次状态栏项状态，帮助定位不显示问题"""
        try:
            nsapp = getattr(self, "_nsapp", None)
            nsitem = getattr(nsapp, "nsstatusitem", None) if nsapp else None
            if nsitem:
                try:
                    nsitem.setVisible_(True)
                except Exception:
                    pass
                title = nsitem.title()
                visible = nsitem.isVisible() if hasattr(nsitem, "isVisible") else None
                has_image = nsitem.image() is not None
                if not has_image:
                    try:
                        nsitem.setImage_(NSImage.imageNamed_("NSStatusAvailable"))
                        has_image = nsitem.image() is not None
                    except Exception:
                        pass
                debug_log(f"status_item title={title!r} visible={visible} has_image={has_image}")
            else:
                debug_log("status_item missing: nsstatusitem not ready")
        except Exception as e:
            debug_log(f"status_item debug error: {e}")
        finally:
            try:
                self.debug_timer.stop()
            except Exception:
                pass

    def on_tick(self, _):
        """定时检测回调"""
        now = time.time()

        # 每日重置检查
        current_day = str(date.today())
        if current_day != self.today_key:
            self.today_key = current_day
            today_data = self.history.get(self.today_key, {"total": 0, "tools": {}})
            self.today_seconds = today_data.get("total", 0)
            self.today_tools = today_data.get("tools", {})
            self.last_periodic_alert_usage_seconds = self.today_seconds
            self.warning_sent = False
            self.limit_warning_sent = False

        # 站立提醒独立于 AI 监控：即使监控暂停也要到点提醒
        if self.stand_up_enabled:
            self.fire_stand_up_reminder()

        # 刷新菜单栏标题（站立倒计时前缀）和下拉里的近 7 天可视化
        self.update_title()
        self.update_stand_up_menu_title()
        self.update_weekly_stats()

        if not self.is_monitoring:
            self.last_check_time = now
            return

        # 检测 AI 是否在前台活跃使用
        self.is_ai_active, self.active_detail = check_ai_active(self.config)

        # 累计时间
        if self.is_ai_active:
            elapsed = now - self.last_check_time
            # 防止休眠等导致的大跳跃，最多累计 2 倍检测间隔
            max_elapsed = self.config["check_interval_seconds"] * 2
            elapsed = min(elapsed, max_elapsed)
            self.today_seconds += elapsed

            # 按工具名分别记录（从 active_detail 提取工具名）
            tool_name = infer_tool_name(self.active_detail)
            self.today_tools[tool_name] = self.today_tools.get(tool_name, 0) + elapsed

            # 持久化（节流写入，降低磁盘 IO）
            self.history[self.today_key] = {
                "total": self.today_seconds,
                "tools": self.today_tools,
            }
            if now - self.last_history_save_time >= HISTORY_SAVE_INTERVAL_SECONDS:
                save_history(self.history)
                self.last_history_save_time = now

        self.last_check_time = now

        # 更新菜单项
        if self.is_ai_active:
            self.status_item.title = f"状态: 🟢 {self.active_detail}"
        else:
            self.status_item.title = "状态: ⚪ AI 工具未在前台使用"
        self.today_item.title = f"今日已用: {format_duration(self.today_seconds)}"
        self.update_title()

        # 检查是否需要提醒
        today_min = self.today_seconds / 60
        limit_min = self.config["daily_limit_minutes"]
        warn_at = limit_min * self.config["warning_at_percent"] / 100
        remind_interval = self.config["remind_interval_minutes"] * 60  # 转为秒

        # 80% 预警
        if today_min >= warn_at and not self.warning_sent and today_min < limit_min:
            self.warning_sent = True
            remaining = format_minutes(limit_min - today_min)
            self.show_modal_alert(
                "⚠️ AI 使用时间预警",
                f"今日已使用 {format_minutes(today_min)}，"
                f"剩余约 {remaining}。\n请注意控制使用时间！"
            )

        # 达到限额
        if today_min >= limit_min:
            if not self.limit_warning_sent:
                self.limit_warning_sent = True
                self.last_warning_time = now
                self.show_modal_alert(
                    "🔴 AI 使用时间已到！",
                    f"今日已使用 {format_minutes(today_min)}，"
                    f"已达到 {format_minutes(limit_min)} 的限额。\n"
                    "请休息一下，做点别的事情吧！"
                )
            elif (self.config.get("strict_mode") and
                  self.is_ai_active and
                  now - self.last_warning_time >= remind_interval):
                self.last_warning_time = now
                overtime = format_minutes(today_min - limit_min)
                self.show_modal_alert(
                    "🔴 你还在用 AI！",
                    f"已超出限额 {overtime}，请立即休息！\n"
                    "你可以散散步、喝杯水，或者做些不需要 AI 的工作。"
                )

        # ─── 每 N 分钟阻塞式弹窗提醒（必须手动关闭） ───
        periodic_enabled = self.config.get("periodic_alert_enabled", True)
        periodic_interval = self.config.get("periodic_alert_minutes", 30) * 60
        if (periodic_enabled and
            self.is_ai_active and
            self.today_seconds - self.last_periodic_alert_usage_seconds >= periodic_interval):
            self.last_periodic_alert_usage_seconds = self.today_seconds
            self.show_periodic_alert()

    def send_notification(self, title, message):
        """发送 macOS 通知"""
        try:
            rumps.notification(
                title=title,
                subtitle=APP_NAME,
                message=message,
                sound=True,
            )
        except Exception:
            # fallback: 使用 osascript
            try:
                script = (
                    f'display notification "{message}" '
                    f'with title "{title}" '
                    f'subtitle "{APP_NAME}" '
                    f'sound name "Glass"'
                )
                subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass

    def show_modal_alert(self, title, message, ok="知道了"):
        """弹出阻塞式模态框，直到用户点击按钮才关闭

        凡是"必须让用户看到"的提醒都要走这里，不要用 send_notification：
        rumps 走的是已废弃的 NSUserNotification，而本项目跑在没有 bundle ID 的
        Python 进程里，系统没有对应的通知授权记录，横幅会被静默丢弃——通知确实
        进了通知中心（deliveredNotifications 里查得到），但屏幕上不会弹出来。
        模态框不依赖通知授权，是唯一可靠的用户可见提示。
        """
        try:
            rumps.alert(title=title, message=message, ok=ok)
        except Exception:
            # 极端情况下弹窗失败，退回系统通知，至少不静默失败
            self.send_notification(title, message)

    def show_periodic_alert(self):
        """显示阻塞式弹窗，用户必须手动关闭"""
        today_min = self.today_seconds / 60
        limit_min = self.config["daily_limit_minutes"]
        remaining = max(0, limit_min - today_min)
        interval = self.config.get("periodic_alert_minutes", 30)

        if today_min >= limit_min:
            overtime = format_minutes(today_min - limit_min)
            title = "🔴 该休息了！你已经超出限额！"
            msg = (
                f"你已经连续使用 AI 工具很久了！\n\n"
                f"📊 今日已用: {format_duration(self.today_seconds)}\n"
                f"⏰ 每日限额: {format_minutes(limit_min)}\n"
                f"🚨 已超出: {overtime}\n\n"
                f"请立即停下来休息一下！\n"
                f"• 站起来走走，活动身体\n"
                f"• 喝杯水，看看远处\n"
                f"• 想想是否真的需要 AI 来完成这个任务\n\n"
                f"（此弹窗每 {interval} 分钟出现一次，你必须手动关闭）"
            )
        else:
            title = "⏰ 定时提醒：该休息一下了！"
            msg = (
                f"你已经持续使用 AI 工具 {format_duration(self.today_seconds)} 了。\n\n"
                f"📊 今日已用: {format_duration(self.today_seconds)}\n"
                f"⏰ 每日限额: {format_minutes(limit_min)}\n"
                f"⏳ 剩余: {format_minutes(remaining)}\n\n"
                f"休息一下再继续吧！\n"
                f"• 让眼睛休息 20 秒，看看 20 英尺外的东西\n"
                f"• 伸展一下肩膀和手腕\n"
                f"• 深呼吸几次，理清思路\n\n"
                f"（此弹窗每 {interval} 分钟出现一次，你必须手动关闭）"
            )

        # rumps.alert 是阻塞式的，用户必须点击按钮才能关闭
        rumps.alert(
            title=title,
            message=msg,
            ok="知道了，我会休息的",
        )

    def toggle_monitoring(self, sender):
        self.is_monitoring = not self.is_monitoring
        sender.title = "恢复监控" if not self.is_monitoring else "暂停监控"
        self.last_check_time = time.time()
        self.update_title()

    def apply_config(self, new_config):
        """应用配置到运行时状态，必要时重建定时器"""
        old_interval = self.config.get("check_interval_seconds", 10)
        self.config = new_config
        new_interval = self.config.get("check_interval_seconds", 10)
        if new_interval != old_interval:
            try:
                self.timer.stop()
            except Exception:
                pass
            self.timer = rumps.Timer(self.on_tick, new_interval)
            self.timer.start()

    def set_limit(self, sender):
        new_limit = sender._mins
        self.config["daily_limit_minutes"] = new_limit
        save_config(self.config)

        # 更新菜单勾选状态
        for item in self.limit_menu.values():
            if hasattr(item, '_mins'):
                item.state = (item._mins == new_limit)

        self.limit_item.title = f"限额: {format_minutes(new_limit)}"

        # 重置提醒状态，让新限额生效
        self.warning_sent = False
        self.limit_warning_sent = False
        self.update_title()

        self.show_modal_alert(
            "限额已更新",
            f"每日限额已设为 {format_minutes(new_limit)}",
        )

    def toggle_strict(self, sender):
        self.config["strict_mode"] = not self.config.get("strict_mode", False)
        sender.state = self.config["strict_mode"]
        save_config(self.config)

    def set_periodic_interval(self, sender):
        new_interval = sender._mins
        self.config["periodic_alert_minutes"] = new_interval
        save_config(self.config)

        for item in self.periodic_menu.values():
            if hasattr(item, '_mins'):
                item.state = (item._mins == new_interval)

        # 重置计时，从现在开始算下一个周期
        self.last_periodic_alert_usage_seconds = self.today_seconds

        self.show_modal_alert(
            "定时提醒已更新",
            f"每 {format_minutes(new_interval)} 弹窗提醒一次",
        )

    def toggle_periodic_alert(self, sender):
        enabled = not self.config.get("periodic_alert_enabled", True)
        self.config["periodic_alert_enabled"] = enabled
        sender.state = enabled
        save_config(self.config)
        if enabled:
            self.last_periodic_alert_usage_seconds = self.today_seconds

    def reset_today(self, _):
        response = rumps.alert(
            title="确认重置",
            message="确定要重置今日的使用计时吗？\n此操作不可撤销。",
            ok="确定重置",
            cancel="取消",
        )
        if response == 1:  # OK
            self.today_seconds = 0
            self.today_tools = {}
            self.last_periodic_alert_usage_seconds = 0
            self.history[self.today_key] = {"total": 0, "tools": {}}
            save_history(self.history)
            self.warning_sent = False
            self.limit_warning_sent = False
            self.update_title()
            self.today_item.title = f"今日已用: {format_duration(0)}"

    def show_history(self, _):
        history = load_history()
        lines = []
        today = date.today()
        total = 0
        for i in range(6, -1, -1):
            d = date.fromordinal(today.toordinal() - i)
            key = str(d)
            day_data = history.get(key, {"total": 0, "tools": {}})
            if isinstance(day_data, (int, float)):
                secs = day_data
            else:
                secs = day_data.get("total", 0)
            total += secs
            day_label = "今天" if i == 0 else ("昨天" if i == 1 else d.strftime("%m/%d"))
            bar_len = min(30, int(secs / 60 / 10))  # 每10分钟一格
            bar = "█" * bar_len if bar_len > 0 else "·"
            lines.append(f"{day_label:>6}  {format_duration(secs):>8}  {bar}")

        avg_min = (total / 7) / 60
        lines.append(f"\n7日平均: {format_minutes(avg_min)}/天")
        lines.append(f"7日总计: {format_duration(total)}")

        rumps.alert(
            title="最近 7 天 AI 使用统计",
            message="\n".join(lines),
            ok="好的",
        )

    def open_report(self, _):
        """在浏览器中打开使用报告"""
        webbrowser.open(f"http://127.0.0.1:{REPORT_PORT}")

    # ── 倒计时 ─────────────────────────────────────────────
    def toggle_countdown(self, _):
        """根据当前状态切换：开始 / 取消倒计时"""
        if self.countdown_state == "running":
            self.cancel_countdown()
        else:
            self.start_countdown()

    def start_countdown(self):
        """启动一次新的倒计时"""
        if self.countdown_state == "running":
            return
        self.countdown_remaining_seconds = self.countdown_duration_seconds
        self.countdown_state = "running"
        if self.countdown_timer is None:
            self.countdown_timer = rumps.Timer(self.countdown_tick, 1)
        self.countdown_timer.start()
        self.countdown_action_item.title = "取消倒计时"
        mins = self.countdown_remaining_seconds // 60
        secs = self.countdown_remaining_seconds % 60
        self.countdown_menu.title = f"🕒 倒计时 {mins:02d}:{secs:02d}"
        self.update_title()

    def cancel_countdown(self):
        """手动取消倒计时（运行中或已完成都重置为空闲）"""
        if self.countdown_timer is not None:
            try:
                self.countdown_timer.stop()
            except Exception:
                pass
        self.countdown_state = "idle"
        self.countdown_remaining_seconds = 0
        self.refresh_countdown_labels()
        self.update_title()

    def countdown_tick(self, _):
        """每秒回调，更新倒计时显示"""
        self.countdown_remaining_seconds -= 1
        if self.countdown_remaining_seconds <= 0:
            self.countdown_finished()
            return
        mins = self.countdown_remaining_seconds // 60
        secs = self.countdown_remaining_seconds % 60
        self.countdown_menu.title = f"🕒 倒计时 {mins:02d}:{secs:02d}"
        self.update_title()

    def countdown_finished(self):
        """倒计时结束：通知 + 切到 finished 状态"""
        if self.countdown_timer is not None:
            try:
                self.countdown_timer.stop()
            except Exception:
                pass
        self.countdown_state = "finished"
        self.countdown_remaining_seconds = 0
        self.refresh_countdown_labels()
        self.update_title()
        self.show_modal_alert(
            f"🕒 {self.countdown_duration_minutes} 分钟倒计时结束",
            "可以休息一下眼睛、伸个懒腰，或继续专注工作。",
        )

    def set_countdown_duration(self, sender):
        """选择倒计时时长，并记住该选择（下次启动沿用）

        没在倒计时就直接开始，省掉再点一次"开始"；已经在跑了就只改时长，
        不打断当前这次。
        """
        new_minutes = int(sender._mins)
        self.countdown_duration_minutes = new_minutes
        self.countdown_duration_seconds = new_minutes * 60
        self.config["countdown_duration_seconds"] = self.countdown_duration_seconds
        save_config(self.config)

        # 更新勾选状态
        for item in self.countdown_duration_submenu.values():
            if hasattr(item, "_mins"):
                item.state = (item._mins == new_minutes)

        if self.countdown_state == "running":
            self.refresh_countdown_labels()
            return
        self.start_countdown()

    def refresh_countdown_labels(self):
        """把菜单文案刷成当前时长；运行中只更新"取消"，避免打断正在跑的倒计时"""
        mins = self.countdown_duration_minutes
        if self.countdown_state == "running":
            self.countdown_action_item.title = "取消倒计时"
            return
        if self.countdown_state == "finished":
            self.countdown_action_item.title = f"重新开始 {mins:02d}:00 倒计时"
            self.countdown_menu.title = f"🕒 {mins} 分钟倒计时（已完成）"
            return
        self.countdown_action_item.title = f"开始 {mins:02d}:00 倒计时"
        self.countdown_menu.title = f"🕒 {mins} 分钟倒计时"

    # ── 站立提醒 ───────────────────────────────────────────
    def _restore_stand_up_state(self):
        """从配置恢复站立提醒状态

        关键点：配置里为 0（"从未提醒"）时用当前时间兜底，并**立刻落盘**。
        否则配置永远是 0，每次重启都会被重新当成"从未提醒"，
        倒计时每次都从头开始 —— 这正是之前"重启后站立提醒时间被重置"的根因。
        """
        self.stand_up_interval_minutes = int(self.config.get("stand_up_interval_minutes", 45))
        self.stand_up_enabled = bool(self.config.get("stand_up_enabled", False))
        saved_last = float(self.config.get("stand_up_last_reminder_time", 0) or 0)
        if saved_last > 0:
            self.stand_up_last_reminder_time = saved_last
        else:
            self.stand_up_last_reminder_time = time.time()
            self.config["stand_up_last_reminder_time"] = self.stand_up_last_reminder_time
            save_config(self.config)
        # 菜单栏标题显示方式：final5 = 仅剩 5 分钟时显示前缀；always = 始终显示
        self.stand_up_title_mode = self.config.get("stand_up_title_mode", "final5")
        if self.stand_up_title_mode not in ("final5", "always"):
            self.stand_up_title_mode = "final5"

    def toggle_stand_up(self, sender):
        """切换站立提醒开关"""
        self.stand_up_enabled = not self.stand_up_enabled
        sender.state = self.stand_up_enabled
        self.config["stand_up_enabled"] = self.stand_up_enabled
        if self.stand_up_enabled:
            # 启用时把上次提醒时间重置为现在，避免立即触发
            self.stand_up_last_reminder_time = time.time()
            self.config["stand_up_last_reminder_time"] = self.stand_up_last_reminder_time
        save_config(self.config)
        self.update_stand_up_menu_title()
        # 菜单栏标题前缀也要立刻刷，否则要等下一个 on_tick 才看到
        self.update_title()
        # 开关项的标签也跟随状态切换，让用户关闭重开后能立刻看到
        if self.stand_up_enabled:
            self.stand_up_toggle_item.title = "禁用站立提醒"
        else:
            self.stand_up_toggle_item.title = "启用站立提醒"

    def set_stand_up_interval(self, sender):
        """调整站立提醒间隔（分钟）"""
        new_interval = int(sender._mins)
        self.stand_up_interval_minutes = new_interval
        self.config["stand_up_interval_minutes"] = new_interval
        # 调整间隔后把上次提醒时间重置为现在
        self.stand_up_last_reminder_time = time.time()
        self.config["stand_up_last_reminder_time"] = self.stand_up_last_reminder_time
        save_config(self.config)
        # 更新勾选状态
        for item in self.stand_up_interval_submenu.values():
            if hasattr(item, "_mins"):
                item.state = (item._mins == new_interval)
        self.update_stand_up_menu_title()

    def test_stand_up_reminder(self, _):
        """立即触发一次站立提醒（用于测试通知）"""
        # fire_stand_up_reminder 内部已弹模态框，这里不再叠加第二个弹窗
        self.fire_stand_up_reminder(force=True)
        # 菜单标题给可见反馈
        self._show_stand_up_feedback("已发送 ✅")

    def fire_stand_up_reminder(self, force=False):
        """到点时由 on_tick 调用；force=True 表示手动测试"""
        now = time.time()
        elapsed_min = (now - self.stand_up_last_reminder_time) / 60.0
        if not force and elapsed_min < self.stand_up_interval_minutes:
            return
        self.stand_up_last_reminder_time = now
        self.config["stand_up_last_reminder_time"] = now
        save_config(self.config)
        body = (
            f"已经坐了约 {self.stand_up_interval_minutes} 分钟啦，\n"
            "站起来活动肩颈、眺望远处，保护眼睛。"
        )
        self.show_modal_alert(
            "🧍 站起来活动一下",
            body,
        )

    def update_stand_up_menu_title(self):
        """根据开关、间隔、上次提醒时间刷新菜单标题（禁用时也显示间隔）"""
        now = time.time()
        feedback_text = getattr(self, "_stand_up_feedback_text", "")
        feedback_expire = getattr(self, "_stand_up_feedback_expire", 0)
        if feedback_text and feedback_expire > now:
            self.stand_up_menu.title = f"🧍 站立提醒（{feedback_text}）"
            return
        if not self.stand_up_enabled:
            self.stand_up_menu.title = f"🧍 站立提醒（{self.stand_up_interval_minutes} 分钟）"
            return
        elapsed = now - self.stand_up_last_reminder_time
        remaining_sec = max(
            0, int(self.stand_up_interval_minutes * 60 - elapsed)
        )
        if remaining_sec <= 0:
            self.stand_up_menu.title = "🧍 站立提醒（即将提醒…）"
        else:
            mins = remaining_sec // 60
            secs = remaining_sec % 60
            self.stand_up_menu.title = f"🧍 站立提醒（下次 {mins:02d}:{secs:02d}）"

    def _show_stand_up_feedback(self, text, duration=2.5):
        """在菜单标题里临时显示反馈文字，duration 秒后自动还原"""
        self._stand_up_feedback_text = text
        self._stand_up_feedback_expire = time.time() + duration
        self.update_stand_up_menu_title()
        # 用一次性 rumps.Timer 在主线程里清掉反馈
        timer = getattr(self, "_stand_up_feedback_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        new_timer = rumps.Timer(self._clear_stand_up_feedback, duration + 0.5)
        self._stand_up_feedback_timer = new_timer
        new_timer.start()

    def _clear_stand_up_feedback(self, _):
        self._stand_up_feedback_text = ""
        self._stand_up_feedback_expire = 0
        self.update_stand_up_menu_title()


    def set_stand_up_title_mode(self, sender):
        """切换菜单栏标题显示方式：final5 / always"""
        mode = getattr(sender, "_mode", None)
        if mode not in ("final5", "always"):
            return
        self.stand_up_title_mode = mode
        self.config["stand_up_title_mode"] = mode
        save_config(self.config)
        for item in self.stand_up_title_mode_submenu.values():
            if hasattr(item, "_mode"):
                item.state = (item._mode == mode)
        # 立刻刷新菜单栏标题，让用户看到效果
        self.update_title()

    def update_weekly_stats(self):
        """刷新菜单里近 7 天的可视化（电量监测样式 + NSColor 主题色）"""
        today = date.today()
        days = []
        total_secs = 0
        for i in range(6, -1, -1):
            d = date.fromordinal(today.toordinal() - i)
            d_str = str(d)
            day_data = self.history.get(d_str, {"total": 0, "tools": {}})
            if isinstance(day_data, dict):
                secs = day_data.get("total", 0)
            else:
                secs = day_data
            days.append((d, secs))
            total_secs += secs

        # 写入"今日"实时累计，保证今日的条和数字不会因为节流滞后
        if days:
            _, last_secs = days[-1]
            if abs(last_secs - self.today_seconds) > 0.5:
                days[-1] = (days[-1][0], self.today_seconds)
                total_secs = sum(s for _, s in days)

        limit_minutes = self.config.get("daily_limit_minutes", 180)

        # ── 表头 ──
        # 「近 7 天」用 labelColor，合计时长用语义色；两段独立着色，
        # 避免整行染色导致深色窗口下标题看不清。
        _set_segmented_title(
            self.stats_header_item,
            [
                ("  近 7 天", "label", "bold"),
                (" · 合计 ", "gray", None),
                (format_duration(total_secs),
                 _color_name_for_total_hours(total_secs / 3600), "digit"),
            ],
        )

        # ── 柱状图：轨道 + 限额刻度 ──
        # 满格 = 5 小时，10 格；轨道用 ░ 铺满全长，一眼看出"占限额多少"。
        # │ 是每日限额在轨道上的位置，轨道本身代表 5 小时上限。
        bar_unit = 30 * 60          # 1 格 = 30 分钟
        max_bars = 10               # 10 格 = 5 小时
        limit_bars = limit_minutes / 30.0   # 限额落在第几格（可为小数）
        limit_col = max(1, min(max_bars - 1, int(round(limit_bars))))

        for i, (d, secs) in enumerate(days):
            if i == len(days) - 1:
                day_label = "今天"
            elif i == len(days) - 2:
                day_label = "昨天"
            else:
                day_label = d.strftime("%m/%d")

            # 柱子：填满的部分用语义色，剩余轨道用 quaternary（浅色下是浅灰、
            # 深色下是深灰，不会抢戏但能看出边界）
            bar_count = min(max_bars, secs / bar_unit)
            day_color = _color_name_for_day_seconds(secs, limit_minutes)
            filled = "█" * int(bar_count)
            # 不足一格的部分用 ▏ 表示，避免几十分钟的数据看起来是空的
            partial = "▏" if (0 < bar_count - int(bar_count) and not filled) else ""

            # 把 │ 插到限额位置（刻度线独占一列，所以整行恒为 max_bars + 1 列，
            # 所有日期的时长数字才会对齐）
            left_cells = filled + partial
            if len(left_cells) >= limit_col:
                # 柱子已经盖过刻度线：语义色本身已经表达"超限"，刻度线不再画，
                # 但补一格轨道保持行宽一致
                bar_segments = [
                    (left_cells, day_color, "mono"),
                    ("░" * (max_bars - len(left_cells) + 1), "quaternary", "mono"),
                ]
            else:
                bar_segments = [
                    (left_cells, day_color, "mono"),
                    ("░" * (limit_col - len(left_cells)), "quaternary", "mono"),
                    ("│", "gray", "mono"),
                    ("░" * (max_bars - limit_col), "quaternary", "mono"),
                ]

            # 日期与时长都用 labelColor 起步（自动跟随明暗），
            # 0 天的时长用 secondary —— 在深色窗口下依然可读。
            dur_text = format_duration(secs) if secs > 0 else "—"
            dur_color = day_color if secs > 0 else "secondary"
            is_today = i == len(days) - 1

            _set_segmented_title(
                self.stats_day_items[i],
                [
                    ("  ", None, None),
                    (_pad_display(day_label, 5, "right"),
                     "label", "bold" if is_today else None),
                    ("  ", None, None),
                    *bar_segments,
                    ("  ", None, None),
                    # 右补空格到固定宽度，让所有行的时长数字对齐。
                    # 时长段用的是等宽数字字体，按字符数补即可（"—" 在该字体下也是一格），
                    # 不能用 _pad_display：它按东亚字宽算，会给破折号多补一格。
                    (dur_text.rjust(7), dur_color, "digit"),
                ],
            )

    def quit_app(self, _):
        # 保存数据
        self.history[self.today_key] = {"total": self.today_seconds, "tools": self.today_tools}
        save_history(self.history)
        # 保存站立提醒状态，避免下次启动立即触发
        self.config["stand_up_last_reminder_time"] = self.stand_up_last_reminder_time
        save_config(self.config)
        # 清理倒计时定时器
        if self.countdown_timer is not None:
            try:
                self.countdown_timer.stop()
            except Exception:
                pass
        feedback_timer = getattr(self, "_stand_up_feedback_timer", None)
        if feedback_timer is not None:
            try:
                feedback_timer.stop()
            except Exception:
                pass
        rumps.quit_application()


if __name__ == "__main__":
    app = AITimeGuardApp()
    app.run()
