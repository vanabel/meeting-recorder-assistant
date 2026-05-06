# 自动会议录制助手 MVP

这是一个 Windows 桌面自动化 MVP，用来按计划启动嗨格式录屏大师并打开腾讯会议链接。它不强行接管两个商业桌面软件的全部 UI，而是先走最稳定的路线：

1. 本工具先检测嗨格式录屏大师是否已经运行；如果没有运行，可按配置启动它。
2. 本工具打开腾讯会议邀请链接或 `wemeet://` 链接。
3. 按配置执行开始录制命令。
4. 打开会议链接。
5. 会议结束后按配置执行停止录制命令，并可选执行“离开会议”命令。

录制会议前请确认已获得必要授权。

## 快速开始

需要 Python 3.11 或更高版本。

```powershell
cd E:\development\meeting-recorder-assistant
Copy-Item config.example.json config.json
python -m venv .venv
.\.venv\Scripts\python.exe meeting_recorder.py --config config.json validate
.\.venv\Scripts\python.exe meeting_recorder.py --config config.json list
```

启动 GUI：

```powershell
.\.venv\Scripts\python.exe meeting_recorder_gui.py
```

Windows 双击启动：

```text
Meeting Recorder GUI.lnk
```

或：

```text
Meeting Recorder GUI.vbs
```

这两个入口会通过 `pythonw.exe` 启动 GUI，不会保留命令行窗口，并会请求管理员权限。

调试备用入口：

```text
Start Meeting Recorder GUI.bat
```

`.bat` 文件主要用于调试，可能会短暂显示命令行窗口。嗨格式录屏大师需要提权时，控制热键的 GUI 也必须以管理员权限运行。

执行下一场待开始会议：

```powershell
.\.venv\Scripts\python.exe meeting_recorder.py --config config.json run-next
```

持续守护并自动执行所有待开始会议：

```powershell
.\.venv\Scripts\python.exe meeting_recorder.py --config config.json watch
```

测试某个任务但不真正启动软件：

```powershell
.\.venv\Scripts\python.exe meeting_recorder.py --config config.json --dry-run run demo-20260510
```

## GUI 功能

GUI 支持：

- 查看、添加、编辑、删除会议任务。
- 将会议链接按会议号自动生成或同步，避免 `meeting_code` 和 `meeting_url` 不一致。
- 编辑录屏软件路径、进程名、开始/停止录制命令。
- 编辑默认提前入会时间和录制尾巴时间。
- 设置 watcher 最大迟到启动时间，避免自动执行很久以前已经错过的任务。
- 保存并校验 `config.json`。
- 保存配置后重启 GUI。
- 以管理员身份重启 GUI，用于控制需要提权的录屏软件。
- 运行选中任务、执行 1 分钟测试任务、运行下一场任务、启动 watcher、停止当前运行任务。
- 在窗口内查看运行日志。

`Stop Current` 会停止当前 watcher、测试任务或手动运行任务。若任务已经开始录制，它会先执行 `recorder.stop_command`，再结束任务。

如果嗨格式没有自动启动，检查 GUI 的 Recorder 页：

- `Path` 必须是实际可执行文件路径，例如 `C:\Program Files\Auntec\HiRecMaster\HiRecMaster.exe`。
- `Process Names` 必须匹配启动后的进程名，例如 `HiRecMaster.exe`。
- `Launch if not running` 必须勾选。

启动后日志会显示是否检测到录屏进程。如果显示未检测到，通常是 `Process Names` 和实际进程名不一致。

如果日志显示录屏器需要管理员权限，必须让本工具本身以管理员权限运行。GUI 会在执行任务时自动打开管理员版 GUI；也可以手动点击 `Restart as Admin`。管理员版 GUI 打开后，需要重新点击 `Test 1 Min`、`Run Selected` 或 `Start Watcher`。

Watcher 默认只自动执行距离计划录制开始时间不超过 10 分钟的任务。超过该窗口的 enabled 任务会被跳过，日志会显示 `Skipping stale task`。需要手动执行时，使用 `Run Selected` 或 `Test 1 Min`。

## 配置说明

`recorder.path` 是嗨格式录屏大师可执行文件路径。不同安装版本路径可能不同，需要按本机实际情况修改。

`recorder.start_delay_seconds` 是启动录屏软件后等待多少秒再继续。录屏软件需要一点时间加载主窗口。

`recorder.process_names` 是用于检测嗨格式是否已经运行的进程名列表。当前安装版通常可以用 `HiRecMaster.exe`。

`recorder.launch_if_not_running` 控制检测不到嗨格式进程时是否自动启动 `recorder.path`。

`recorder.prepare_command` 是开始录制前的准备动作。嗨格式需要先选择“全屏录制”时，推荐配置为 `mode:fullscreen`。工具会先尝试查找并点击“全屏录制”文字；如果控件文字不可识别，会按嗨格式主窗口里的全屏录制按钮位置进行兜底点击。

`recorder.start_delay_seconds` 是嗨格式启动并出现窗口后，再等待多少秒才继续选择录制模式。嗨格式需要加载首页时，建议设为 `10`。

`recorder.prepare_delay_seconds` 是点击“全屏录制”后等待多少秒再发送开始录制快捷键。当前默认是 `0`，因为主要等待已经放在启动嗨格式之后、点击“全屏录制”之前。

`recorder.start_command` 是触发“开始录制”的命令。如果嗨格式已经随系统启动但不自动录制，可以配置为 `hotkey:alt+1`。工具会先尝试让嗨格式成为真正前台窗口，等待 `start_delay_seconds`，点击“全屏录制”，再发送热键；随后才打开腾讯会议，避免腾讯会议抢走焦点。

`recorder.stop_command` 是会议结束后停止录屏的命令。如果嗨格式快捷键是 `Alt+2` 结束录制，可以配置为 `hotkey:alt+2`。

如果嗨格式快捷键是 `Alt+1` 开始/暂停录制、`Alt+2` 结束录制，并且你希望“会议开始才录”，推荐关闭嗨格式的“启动软件后自动录制”，使用：

```json
"process_names": ["HiRecMaster.exe"],
"launch_if_not_running": true,
"prepare_command": "mode:fullscreen",
"prepare_delay_seconds": 0,
"start_command": "hotkey:alt+1",
"stop_command": "hotkey:alt+2"
```

`tencent_meeting.open_delay_seconds` 是打开会议链接后等待腾讯会议窗口出现的时间。`focus_after_join` 为 `true` 时，工具会在等待后把腾讯会议窗口置前。

`defaults.join_early_minutes` 控制自动化流程比会议开始提前多少分钟启动。例如会议 `15:00-16:00`，默认 `1` 表示大约 `14:59` 开始启动嗨格式、选择全屏录制、开始录制并打开会议。

如果仍然开启“启动软件后自动录制”，不要把 `start_command` 配成 `Alt+1`，否则软件启动并自动开始录制后，额外发送一次 `Alt+1` 可能会暂停录制。

`tencent_meeting.leave_command` 是离开当前会议的命令。它应只离开会议，不退出腾讯会议 App。没有可靠命令时保持为 `null`。

`tencent_meeting.close_command` 是退出腾讯会议 App 的命令。若只想离开会议而保留 App 运行，应保持为 `null`。

任务字段：

- `id`：任务唯一标识。
- `title`：日志中显示的会议名称。
- `meeting_code`：会议号，作为备用信息。
- `meeting_url`：优先打开的会议链接。建议使用腾讯会议邀请链接或本机验证可用的 `wemeet://` 链接。
- `start_time` / `end_time`：本机时间，格式为 `YYYY-MM-DD HH:MM`。
- `enabled`：是否启用。

## 日志

运行日志默认写入：

```text
logs/meeting-recorder.log
```

## 后续扩展

这个 MVP 已经把调度和动作编排分开。后续可以逐步加入：

- `pywinauto` 输入会议号、密码和昵称。
- 失败截图。
- SQLite 保存会议计划。
- 托盘应用。
- OBS WebSocket 控制。
