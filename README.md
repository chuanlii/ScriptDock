# ScriptDock

Windows 10 / 11 本地脚本托盘管理器。支持 Python、BAT/CMD、JavaScript 和 EXE，后台无控制台启动，实时查看 stdout / stderr，并统一启动、停止、重启。

## 直接使用

本仓库只包含源码、测试、文档及 SVG 图标源文件，不提交 EXE、生成的 ICO 图标、需求 Word 文档、截图、日志或个人配置。首次使用请按下方步骤安装依赖并运行源码，或自行打包；图标由 `tools/build_icon.py` 生成。

双击 `dist/ScriptDock.exe`。添加脚本时选择本地文件，填写名称；参数使用命令行形式，例如 `--port 8080 --title "我的服务"`。工作目录默认是脚本所在目录。Python / Node 从 PATH 查找，也可指定解释器绝对路径（建议选择 `python.exe`，不要选择 `pythonw.exe`）。打包的管理器不替代脚本所需的 Python / Node 环境。

点击列表中的脚本名称即可切换最近 5,000 行输出，无需单独的日志按钮。日志下拉选项默认 `std` 显示全部日志（含 system），`stdout` / `stderr` 仅显示对应输出；切换筛选不会删除原始日志。正常退出显示 Stopped，启动失败或非零退出码显示 Failed，错误信息在日志中。运行中的脚本需要先停止才能编辑或删除。

主窗口右上角 X 仅隐藏窗口；单击或双击右下角托盘图标重新打开。托盘菜单“退出（停止全部脚本）”确认后停止所有脚本及子进程再退出。重复启动管理器会打开已有窗口，不会启动第二套脚本。

配置自动保存到 `%APPDATA%\ScriptDock\config.json`，仅保存脚本设置，不保存 PID、运行状态或日志。新增、编辑、删除后立即原子保存。脚本列表恢复后不会自动运行脚本。

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

MVP 不包含定时任务、自动启动/重启、数据库、Web UI、远程控制、插件、更新或持久化日志。
