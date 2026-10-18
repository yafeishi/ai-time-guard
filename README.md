# AI Time Guard

> A local-first macOS menu bar app that measures time spent in AI coding tools,
> adds a daily limit, and turns the result into a clear dashboard.

![AI Time Guard dashboard](assets/dashboard.png)

AI Time Guard helps you notice when an AI-assisted coding session has quietly
become an all-day session. It detects supported desktop apps and terminal tools
only while they are actively in use, keeps all data on your Mac, and provides a
dashboard plus a shareable report card.

## Highlights

- Tracks active time across Codex, Claude Code, Cursor, CodeBuddy, WorkBuddy,
  Antigravity, Kimi, ZCode, MiniMax Code, Windsurf, Trae, Zed, and selected
  AI-assisted note apps.
- Sets a daily time budget with warning, periodic reminder, and strict modes.
- Shows daily, monthly, and yearly trends in a responsive local dashboard.
- Produces a share card without sending usage history to a remote service.
- Stores configuration and history locally in `~/.ai-time-guard/`.

![Shareable AI usage card](assets/share-card.png)

## Requirements

- macOS 12 or later
- Python 3.9 or later
- Accessibility permission for detecting the frontmost app
- Optional: Playwright and Chromium for PNG export

## Install and run

```bash
git clone https://github.com/yafeishi/ai-time-guard.git
cd ai-time-guard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python ai_time_guard.py
```

Open <http://127.0.0.1:19527> for the dashboard. You can also double-click the
included `AI Time Guard.app` after the virtual environment has been created in
the repository directory.

For image export, install the optional browser dependency:

```bash
pip install playwright
playwright install chromium
```

## How tracking works

AI Time Guard combines the current frontmost macOS application with a lightweight
process check. GUI tools count only while their app is in the foreground. CLI
tools count only while a supported terminal is in the foreground and the tool's
process is running. Checks run every 10 seconds by default.

The app records only duration and tool category. It does **not** capture prompts,
source code, keystrokes, screenshots, or window contents.

## Local API

The server listens on `127.0.0.1:19527` and exposes these endpoints:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/report` | GET | Full history, configuration, and current status |
| `/api/today` | GET | Lightweight summary for today |
| `/api/daily/YYYY-MM-DD` | GET | One day's usage |
| `/api/summary?days=N` | GET | Daily aggregation, up to 365 days |
| `/api/summary/monthly?months=N` | GET | Monthly aggregation, up to 120 months |
| `/api/summary/yearly?years=N` | GET | Yearly aggregation, up to 20 years |
| `/api/config` | GET, POST | Read or update local settings |
| `/api/generate-image?type=share\|card` | GET | Export a PNG with Playwright |

Example:

```bash
curl -s http://127.0.0.1:19527/api/today | python3 -m json.tool
curl -s "http://127.0.0.1:19527/api/generate-image?type=card" -o card.png
```

The API is intentionally unauthenticated for local integrations. Do not expose
port `19527` to the internet. See [SECURITY.md](SECURITY.md) for details.

## Data and configuration

| File | Description |
| --- | --- |
| `~/.ai-time-guard/config.json` | Limits, reminder behavior, theme, and interval |
| `~/.ai-time-guard/history.json` | Daily totals and per-tool durations |
| `~/.ai-time-guard/debug.log` | Local diagnostic log |

Delete the directory to remove all local AI Time Guard data.

## Development

```bash
python -m unittest discover -s tests -v
```

The interface is implemented with dependency-free HTML, CSS, and JavaScript;
the menu bar service and local API live in `ai_time_guard.py`.

## Limitations

- App and process names are heuristic and may change between tool releases.
- The included `.app` bundle is an unsigned launcher, not a notarized build.
- Sleep, crashes, or force-quitting between history flushes can lose up to one
  minute of the latest session.
- AI Time Guard is a personal awareness tool, not employee-monitoring software.

## License

[MIT](LICENSE)

### 站立提醒与记录

站立弹窗提供「我已经站立」和「知道了」两个按钮。确认已经站立后记录一次实际站立，并从确认时间开始下一轮倒计时；选择「知道了」会保留待站立状态，每 5 分钟再次提醒。关闭弹窗后，可从「站立提醒 → 我已经站立（记录一次）」补记。

报告显示今日提醒次数、实际站立次数和最近 7 天的每日统计。每次提醒和站立确认的时间保存在 `~/.ai-time-guard/stand-up.json`，应用重启后仍然保留。手动「立即提醒一次」也会计入提醒次数。旧版本没有采集这些事件，因此无法补回此前的站立记录。

站立提醒支持暂停 1 小时或 2 小时。统一从「暂停站立提醒」子菜单选择时长，到期自动恢复并弹窗提醒；也可随时立即恢复。暂停截止时间会保存，重启不会延长暂停，已经累计的站立计时继续保留。

菜单提供「下班／上班」按钮。下班停止站立提醒并取消临时暂停的自动恢复，AI 使用统计及其提醒继续；上班从当前时间开始新一轮站立计时，不计作实际站立。上下班状态在重启后保留，跨日不会自动上班。


### 统一事件提醒

菜单「事件提醒 → 添加／管理提醒」打开报告页面，可添加、编辑和删除多个并行事件。支持单次倒计时、完成后重复、固定间隔重复，独立暂停／恢复、稍后提醒，以及暂停 1／2 小时后自动恢复。编辑不重置本轮计时，重新开始后采用新时长。

每个事件可设为仅上班期间或全天。下班只停止工作范围事件的提醒；上班为已启动的工作范围事件开始新一轮，全天事件不受影响。同一时刻到期的确认弹窗事件合并显示，可全部确认，也可稍后在各自菜单中分别确认。普通通知需要在菜单或配置页记录完成。

现有倒计时时长迁移为「握力训练」（初始未开始），站立状态和截止时间迁移为「站立活动」；旧站立记录继续保留。统一事件从迁移时起分别记录到点提醒、追提醒和完成次数。事件及记录保存在 `~/.ai-time-guard/events.json`，重启恢复，不包含多设备同步。默认只创建上述两个事件，用药事件需自行确认间隔并开始。

事件菜单和管理页面支持「全屏专注」。点击全屏开始后启动新一轮，大号显示计时，支持暂停／继续和退出；Esc 退出展示不会取消计时。专注期间暂缓应用弹窗，页面失联后约 15 秒恢复提醒。单次倒计时到点记录本轮完成，重复事件需要实际完成后确认。此功能不锁定系统，也不屏蔽其他应用的通知。

菜单顶层直接显示各事件的状态和主要动作，无需先进入事件子菜单。点击未开始的单次事件打开全屏专注；点击暂停事件恢复；点击「我已完成」记录实际完成。限时暂停、取消和重新开始在各事件的「更多操作」中，配置在「管理事件与记录」页面；AI 统计与设置集中到独立子菜单。

macOS 的全屏专注入口现使用独立原生窗口覆盖鼠标所在屏幕，隐藏菜单栏和 Dock，不再依赖浏览器。Esc、退出按钮或切换到其他应用可结束专注展示，事件计时继续。浏览器全屏页面保留作为兼容入口。

精简版菜单只显示单次专注入口、重复事件状态、上下班、设置和退出。常规重复事件运行时是只读状态；待完成时可回应，未开始时可启动。辅助操作统一移到设置页面。原生单次专注画面只显示大号剩余时间和退出按钮，到点显示「完成」，不再展示暂停、再来一轮等按钮。重新进入运行中的专注会延续原来的计时。

站立活动的主按钮现在为「站立 1 分钟」。点击后启动原生一分钟倒计时，到点才记录站立并开始下一轮 45 分钟提醒；提前退出取消本次站立倒计时，不产生完成记录。设置中的手动补记入口仍保留。

可启用工作日自动时段（`event_schedule_enabled`）：北京时间周一至周五 08:00 开启，次日 01:00 暂停全部事件，包括全天事件；周五延续到周六 01:00，周末其余时间暂停。剩余时间保留，重启、休眠唤醒后自动核对当前时段；手动上下班可临时覆盖，直到下一个计划边界。未启动的单次专注不会自动开始。AI 使用统计独立运行。
