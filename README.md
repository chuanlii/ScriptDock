# ScriptDock

Windows 10 / 11 本地脚本托盘管理器。支持 Python、BAT/CMD、JavaScript 和 EXE，后台无控制台启动，实时查看 stdout / stderr，并统一启动、停止、重启。

## 直接使用

本仓库只包含源码、测试、文档及 SVG 图标源文件，不提交 EXE、生成的 ICO 图标、需求 Word 文档、截图、日志或个人配置。首次使用请按下方步骤安装依赖并运行源码，或自行打包；图标由 `tools/build_icon.py` 生成。

双击 `dist/ScriptDock.exe`。添加脚本时选择本地文件，填写名称；参数使用命令行形式，例如 `--port 8080 --title "我的服务"`。工作目录默认是脚本所在目录。Python / Node 从 PATH 查找，也可指定解释器绝对路径（建议选择 `python.exe`，不要选择 `pythonw.exe`）。打包的管理器不替代脚本所需的 Python / Node 环境。

点击列表中的脚本名称即可切换最近 5,000 行输出，无需单独的日志按钮。日志下拉选项默认 `std` 显示全部日志（含 system），`stdout` / `stderr` 仅显示对应输出；切换筛选不会删除原始日志。脚本状态统一使用英文：`Starting`、`Running`、`Stopped`、`Failed`。错误信息在日志中。脚本自行非零退出时发送 Windows 托盘消息，指出脚本名称和退出码；正常退出、手动停止/重启及退出管理器不会触发异常退出通知。Windows 通知设置可能影响消息显示。运行中的脚本需要先停止才能编辑或删除。

添加或编辑脚本时可勾选“随 ScriptDock 自动启动”，默认不勾选。列表“自启动”列显示是/否。此选项仅在启动 ScriptDock 时运行已勾选脚本，不会设置 Windows 开机启动，也不会在保存新脚本后立即运行。

“打开网页”无需配置网址。ScriptDock 会定期检查运行脚本及其全部子进程实际监听的 TCP 端口；检测到端口后启用按钮，并使用系统默认浏览器打开本机地址。未运行、尚未开始监听或无法读取端口时按钮置灰；若一个进程树监听多个端口，则按固定顺序选择一个。端口检测只能确认 TCP 监听，不能保证该端口一定提供 HTTP 服务。

异常退出以管理的入口进程退出码为准。BAT/CMD 若在子程序报错后仍返回 0，不会触发异常通知；建议让入口脚本传递子程序的非零退出码。stderr 中的普通警告不等同于崩溃。

主窗口不提供退出按钮，右上角 X 仅隐藏窗口；单击或双击右下角托盘图标重新打开。需要退出时，使用托盘菜单“退出（停止全部脚本）”，确认后会停止所有脚本及子进程再退出。重复启动管理器会打开已有窗口，不会启动第二套脚本。

配置自动保存到 `%APPDATA%\ScriptDock\config.json`，仅保存脚本设置，不保存 PID、运行状态或日志。新增、编辑、删除后立即原子保存。旧配置兼容加载，默认自启动关闭；旧版网页地址字段会保留，但不再用于打开网页。

仅运行可信的本地脚本，管理器不是沙箱。控制台窗口会隐藏，但 EXE 或脚本自行创建的图形窗口不会被隐藏。停止为强制终止，请先保存脚本正在处理的重要数据。BAT/CMD 参数和路径不支持引号及 `% ! & | < > ^` 等命令字符，会提示错误而不会执行；普通空格和中文路径支持。输出按 UTF-8 解码，非 UTF-8 脚本请调整其输出编码（BAT 可使用 `chcp 65001 >nul`）。

## 开发运行

安装 Python 3.12 或更新版本（64 位），在项目目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\pythonw.exe main.py
```

开发调试可用 `python.exe main.py` 查看诊断；该方式的启动终端由调用者提供，正式 EXE 使用 Windows GUI 子系统，无控制台。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools/build_icon.py
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm ScriptDock.spec
```

输出为 `dist/ScriptDock.exe`，无需安装器。推荐使用已验证的 `ScriptDock.spec`：它避免将开发环境自带的 ICU / 旧 API 转发库错误打包，防止遮蔽 Windows 系统运行库。使用标准、干净的 CPython 环境时，也可采用基础命令（从项目根目录执行）：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconsole --onefile --name ScriptDock --icon assets/tray.ico --add-data "assets/tray.svg;assets" main.py
```

## 项目结构

`main.py` 管理应用、托盘和退出；`models/script_config.py` 定义配置；`core/config_manager.py` 负责 JSON 持久化；`core/process_manager.py` 统一进程管理；`core/log_reader.py` 异步读取日志；`core/windows_job.py` 用 Windows Job Object 管理后代进程，防止父进程退出后的孤儿进程；`ui/` 提供主窗口和添加/编辑窗口；`tests/` 提供自动化验收。

不包含定时任务、Windows 开机启动、异常自动重启、数据库、Web UI、远程控制、插件、更新或持久化日志。
