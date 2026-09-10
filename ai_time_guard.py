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
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from AppKit import NSWorkspace, NSApplication, NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular, NSImage
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

DEFAULT_CONFIG = {
    "daily_limit_minutes": 180,       # 每日限额（分钟）
    "warning_at_percent": 80,         # 使用达到百分比时首次提醒
    "remind_interval_minutes": 15,    # 超限后每隔多久再次提醒
    "periodic_alert_minutes": 30,     # 每隔多久弹一次阻塞式提醒（必须手动关闭）
    "periodic_alert_enabled": True,   # 是否启用定时弹窗
    "check_interval_seconds": 10,     # 检测进程间隔（秒）
    "strict_mode": False,             # 严格模式：超限后尝试发送通知并持续提醒
    "theme": "dark",                  # 主题: dark | tencent-blue
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


def check_terminal_has_ai_tool():
    """检查终端中是否有活跃的 AI CLI 工具进程，返回工具名或 None"""
    now = time.time()
    if now - _terminal_ai_cache["last_check"] < _terminal_ai_cache["ttl"]:
        return _terminal_ai_cache["tool"]

    tool = None
    for proc in psutil.process_iter(['name', 'cmdline', 'ppid', 'status']):
        try:
            cmdline = proc.info.get('cmdline') or []
            cmd_str = ' '.join(cmdline).lower()
            if 'ai_time_guard' in cmd_str:
                continue

            # 检查是否有 tty（前台交互式进程）
            has_tty = False
            try:
                terminal = proc.terminal()
                if terminal:
                    has_tty = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            name = (proc.info.get('name') or '').lower()

            # Claude CLI (claude)
            if 'claude' in cmd_str and 'internal' not in cmd_str:
                if has_tty or (name == 'node' and len(cmdline) > 0 and cmdline[0].lower() == 'claude'):
                    tool = "Claude Code"
                    break

            # Claude Internal CLI (claude-internal)
            if 'claude-internal' in cmd_str or 'claude internal' in cmd_str:
                if has_tty:
                    tool = "Claude Internal"
                    break

            # Gemini CLI (gemini)
            if 'gemini' in cmd_str and 'internal' not in cmd_str:
                if has_tty:
                    tool = "Gemini"
                    break

            # Gemini Internal CLI (gemini-internal)
            if 'gemini-internal' in cmd_str or 'gemini internal' in cmd_str:
                if has_tty:
                    tool = "Gemini Internal"
                    break

            # Codex Internal CLI (codex-internal)
            if 'codex-internal' in cmd_str or 'codex internal' in cmd_str:
                if has_tty:
                    tool = "Codex Internal"
                    break

            # WorkBuddy CLI (workbuddy)
            if 'workbuddy' in cmd_str or 'work buddy' in cmd_str:
                if has_tty:
                    tool = "WorkBuddy"
                    break

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

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
                
            # 匹配 AI 工具
            for tool_name, patterns in ai_tool_patterns:
                matched = False
                for pattern in patterns:
                    if pattern in cmd_str or pattern in name:
                        matched = True
                        break
                
                if matched:
                    seen_pids.add(pid)
                    # 使用非阻塞方式获取 CPU（首次可能为 0，但响应快）
                    try:
                        proc_obj = psutil.Process(pid)
                        cpu_percent = proc_obj.cpu_percent(interval=None)  # 非阻塞
                        memory_mb = proc_obj.memory_info().rss / 1024 / 1024
                        memory_percent = proc_obj.memory_percent()
                        
                        resources.append({
                            'tool': tool_name,
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

        self.quit_item = rumps.MenuItem("退出", callback=self.quit_app)

        self.menu = [
            self.status_item,
            self.today_item,
            self.limit_item,
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
            self.quit_item,
        ]

        self.update_title()

        # 启动定时检测
        self.timer = rumps.Timer(self.on_tick, self.config["check_interval_seconds"])
        self.timer.start()

    def update_title(self):
        """更新菜单栏显示的标题"""
        today_min = self.today_seconds / 60
        limit_min = self.config["daily_limit_minutes"]

        if not self.is_monitoring:
            self.title = f"PAUSE {format_duration(self.today_seconds)}"
        elif self.is_ai_active:
            if today_min >= limit_min:
                self.title = f"OVER {format_duration(self.today_seconds)}"
            else:
                self.title = f"AI {format_duration(self.today_seconds)}"
        else:
            self.title = f"IDLE {format_duration(self.today_seconds)}"

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
            self.send_notification(
                "⚠️ AI 使用时间预警",
                f"今日已使用 {format_minutes(today_min)}，"
                f"剩余约 {remaining}。\n请注意控制使用时间！"
            )

        # 达到限额
        if today_min >= limit_min:
            if not self.limit_warning_sent:
                self.limit_warning_sent = True
                self.last_warning_time = now
                self.send_notification(
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
                self.send_notification(
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

        rumps.notification(
            title="限额已更新",
            subtitle=APP_NAME,
            message=f"每日限额已设为 {format_minutes(new_limit)}",
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

        rumps.notification(
            title="定时提醒已更新",
            subtitle=APP_NAME,
            message=f"每 {format_minutes(new_interval)} 弹窗提醒一次",
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

    def quit_app(self, _):
        # 保存数据
        self.history[self.today_key] = {"total": self.today_seconds, "tools": self.today_tools}
        save_history(self.history)
        rumps.quit_application()


if __name__ == "__main__":
    app = AITimeGuardApp()
    app.run()
