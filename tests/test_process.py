"""Integration tests for the Windows process manager.

The process manager deliberately owns the complete child process lifecycle, so
these tests exercise it with real scripts instead of mocking subprocess.
They are skipped on non-Windows hosts because WindowsJob uses the Win32 Job
Object API and the production application is Windows-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="ScriptDock targets Windows")

# Set the platform before creating QApplication. The environment variable is
# harmless on Windows and lets the same test module run in headless CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.process_manager import ProcessManager


@dataclass
class TestConfig:
    """Small test double for the process manager's documented config contract."""

    __test__ = False

    id: str
    path: str
    args: list[str] = field(default_factory=list)
    working_directory: str | None = None
    interpreter: str | None = None
    name: str = "test script"
    type: str = "python"

    def validate(self) -> None:
        if not self.id or not self.path:
            raise ValueError("id and path are required")


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def manager(qapp):
    process_manager = ProcessManager()
    yield process_manager
    # A failed assertion must not leave a service process behind and affect a
    # later test. shutdown() also closes any remaining Job Object handles.
    try:
        process_manager.shutdown()
    except Exception:
        # Preserve the test's original failure; this is only a last-resort
        # cleanup guard.
        pass
    qapp.processEvents()


def wait_for(qapp, predicate, timeout: float = 15.0, message: str = "condition not met"):
    """Pump Qt events while waiting for a background manager operation."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    qapp.processEvents()
    assert predicate(), message


def write_script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8", newline="\n")
    return path


def python_config(script: Path, script_id: str = "script", *, args=None, cwd=None) -> TestConfig:
    return TestConfig(
        id=script_id,
        path=str(script),
        args=list(args or []),
        working_directory=str(cwd or script.parent),
        interpreter=sys.executable,
        type="python",
    )


def register(manager: ProcessManager, config: TestConfig) -> None:
    manager.register(config)


def logs_for(manager: ProcessManager, script_id: str, stream: str | None = None):
    logs = manager.get_logs(script_id)
    return logs if stream is None else [line for kind, line in logs if kind == stream]


def pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def pid_file_ready(path: Path) -> bool:
    """Avoid observing a pid file between its create and write syscalls."""

    try:
        return path.exists() and path.read_text(encoding="ascii").strip().isdigit()
    except (OSError, UnicodeError):
        return False


def test_python_stdout_stderr_normal_exit_and_no_console_flags(tmp_path, qapp, manager, monkeypatch):
    """Python output is captured by stream and Popen requests a hidden window."""

    script = write_script(
        tmp_path,
        "normal.py",
        "import sys\n"
        "print('normal stdout', flush=True)\n"
        "print('normal stderr', file=sys.stderr, flush=True)\n",
    )
    config = python_config(script, "normal")
    register(manager, config)

    captured = []
    real_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        captured.append(kwargs.get("creationflags", 0))
        return real_popen(*args, **kwargs)

    # Keep the production WindowsJob path intact; this records the flags on
    # the exact Popen call used by ProcessManager.
    monkeypatch.setattr("core.process_manager.subprocess.Popen", capture_popen)
    manager.start("normal")
    wait_for(qapp, lambda: manager.get_status("normal") in {"Stopped", "Failed"})

    assert manager.get_status("normal") == "Stopped"
    assert captured, "ProcessManager did not invoke subprocess.Popen"
    assert captured[0] & subprocess.CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
    assert logs_for(manager, "normal", "stdout") == ["normal stdout"]
    assert logs_for(manager, "normal", "stderr") == ["normal stderr"]


def test_python_nonzero_exit_is_failed_and_records_exit_code(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "failed.py",
        "import sys\n"
        "print('before failure', flush=True)\n"
        "print('failure detail', file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n",
    )
    register(manager, python_config(script, "failed"))

    manager.start("failed")
    wait_for(qapp, lambda: manager.get_status("failed") in {"Stopped", "Failed"})

    assert manager.get_status("failed") == "Failed"
    assert "before failure" in logs_for(manager, "failed", "stdout")
    assert "failure detail" in logs_for(manager, "failed", "stderr")
    assert any("退出码 7" in line for line in logs_for(manager, "failed", "system"))


def test_fast_nonzero_exit_emits_one_abnormal_event_per_run(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "fast_failure.py",
        "import sys\n"
        "print('fast failure', flush=True)\n"
        "raise SystemExit(7)\n",
    )
    register(manager, python_config(script, "fast-failure"))
    events = []
    manager.abnormal_exit.connect(lambda script_id, code: events.append((script_id, code)))

    manager.start("fast-failure")
    wait_for(qapp, lambda: manager.get_status("fast-failure") == "Failed")
    wait_for(qapp, lambda: len(events) == 1)
    assert events == [("fast-failure", 7)]

    # A second real launch gets a distinct watcher and exactly one additional
    # event; the first launch must not be replayed by a busy-period race.
    manager.start("fast-failure")
    wait_for(qapp, lambda: len(events) == 2)
    assert events == [("fast-failure", 7), ("fast-failure", 7)]
    assert len([line for line in logs_for(manager, "fast-failure", "system") if "退出码 7" in line]) == 2


def test_normal_exit_does_not_emit_abnormal_event(tmp_path, qapp, manager):
    script = write_script(tmp_path, "normal_no_alert.py", "print('normal', flush=True)\n")
    register(manager, python_config(script, "normal-no-alert"))
    events = []
    manager.abnormal_exit.connect(lambda script_id, code: events.append((script_id, code)))

    manager.start("normal-no-alert")
    wait_for(qapp, lambda: manager.get_status("normal-no-alert") == "Stopped")
    # Pump the queued signal path once more so a delayed abnormal event cannot
    # hide behind the status transition assertion.
    wait_for(qapp, lambda: any(stream == "system" for stream, _ in manager.get_logs("normal-no-alert")))
    assert events == []


def test_get_web_url_detects_listeners_and_chooses_lowest_port(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "listeners.py",
        "import socket, time\n"
        "listeners = []\n"
        "for _ in range(2):\n"
        "    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    listener.bind(('127.0.0.1', 0))\n"
        "    listener.listen(1)\n"
        "    listeners.append(listener)\n"
        "print('ports=' + ','.join(str(item.getsockname()[1]) for item in listeners), flush=True)\n"
        "time.sleep(600)\n",
    )
    register(manager, python_config(script, "listeners"))

    manager.start("listeners")
    wait_for(qapp, lambda: manager.get_status("listeners") == "Running")
    wait_for(qapp, lambda: bool(logs_for(manager, "listeners", "stdout")))
    line = logs_for(manager, "listeners", "stdout")[0]
    ports = [int(value) for value in line.removeprefix("ports=").split(",")]
    expected = f"http://127.0.0.1:{min(ports)}"
    wait_for(qapp, lambda: manager.get_web_url("listeners") == expected)
    assert manager.get_web_url("listeners") == expected


def test_get_web_url_detects_recursive_child_listener(tmp_path, qapp, manager):
    port_file = tmp_path / "listener-port.txt"
    child_code = (
        "import socket, time\n"
        "from pathlib import Path\n"
        "listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen(1)\n"
        f"Path({str(port_file)!r}).write_text(str(listener.getsockname()[1]), encoding='ascii')\n"
        "time.sleep(600)\n"
    )
    script = write_script(
        tmp_path,
        "child_listener.py",
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(600)\n",
    )
    register(manager, python_config(script, "child-listener"))

    manager.start("child-listener")
    wait_for(qapp, lambda: manager.get_status("child-listener") == "Running")
    wait_for(qapp, lambda: pid_file_ready(port_file), message="child listener did not start")
    port = int(port_file.read_text(encoding="ascii"))
    expected = f"http://127.0.0.1:{port}"
    wait_for(qapp, lambda: manager.get_web_url("child-listener") == expected)
    assert manager.get_web_url("child-listener") == expected


def test_get_web_url_is_none_when_not_listening_or_stopped(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "not_listening.py",
        "import time\n"
        "time.sleep(600)\n",
    )
    register(manager, python_config(script, "not-listening"))

    manager.start("not-listening")
    wait_for(qapp, lambda: manager.get_status("not-listening") == "Running")
    assert manager.get_web_url("not-listening") is None

    manager.stop("not-listening")
    wait_for(qapp, lambda: manager.get_status("not-listening") == "Stopped")
    wait_for(qapp, lambda: manager.get_web_url("not-listening") is None)
    assert manager.get_web_url("not-listening") is None


def test_stop_restart_and_shutdown_do_not_emit_abnormal_event(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "intentional_stop.py",
        "import time\n"
        "print('ready', flush=True)\n"
        "time.sleep(600)\n",
    )
    register(manager, python_config(script, "intentional-stop"))
    events = []
    manager.abnormal_exit.connect(lambda script_id, code: events.append((script_id, code)))

    manager.start("intentional-stop")
    wait_for(qapp, lambda: manager.get_status("intentional-stop") == "Running")
    first_pid = manager.get_pid("intentional-stop")
    manager.stop("intentional-stop")
    wait_for(qapp, lambda: manager.get_status("intentional-stop") == "Stopped")
    wait_for(qapp, lambda: not pid_is_alive(first_pid))
    assert events == []

    manager.restart("intentional-stop")
    wait_for(qapp, lambda: manager.get_status("intentional-stop") == "Running")
    second_pid = manager.get_pid("intentional-stop")
    assert second_pid and second_pid != first_pid
    assert events == []

    manager.restart("intentional-stop")
    wait_for(qapp, lambda: manager.get_status("intentional-stop") == "Running")
    third_pid = manager.get_pid("intentional-stop")
    assert third_pid and third_pid != second_pid
    assert events == []

    manager.shutdown()
    assert not pid_is_alive(third_pid)
    qapp.processEvents()
    assert events == []


def test_log_buffer_keeps_only_latest_5000_lines(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "many_lines.py",
        "for i in range(6000):\n"
        "    print(f'line-{i}', flush=True)\n",
    )
    register(manager, python_config(script, "many-lines"))

    manager.start("many-lines")
    wait_for(qapp, lambda: manager.get_status("many-lines") in {"Stopped", "Failed"}, timeout=20)
    wait_for(qapp, lambda: len(manager.get_logs("many-lines")) == 5000, timeout=5)

    logs = manager.get_logs("many-lines")
    stdout = [line for stream, line in logs if stream == "stdout"]
    assert len(logs) == 5000
    # The system exit record occupies the final slot, so 4,999 stdout lines
    # survive and the oldest surviving line is line-1001.
    assert len(stdout) == 4999
    assert stdout[0] == "line-1001"
    assert stdout[-1] == "line-5999"
    assert any(stream == "system" and "退出码 0" in line for stream, line in logs)


def test_duplicate_start_stop_and_restart_have_single_live_process(tmp_path, qapp, manager):
    script = write_script(
        tmp_path,
        "long_running.py",
        "import time\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(.1)\n",
    )
    register(manager, python_config(script, "long-running"))

    manager.start("long-running")
    # Calling start again while the first operation is still being scheduled
    # must not create a second process.
    manager.start("long-running")
    wait_for(qapp, lambda: manager.get_status("long-running") == "Running")
    first_pid = manager.get_pid("long-running")
    assert pid_is_alive(first_pid)
    wait_for(qapp, lambda: "ready" in logs_for(manager, "long-running", "stdout"))
    assert manager.get_pid("long-running") == first_pid

    manager.stop("long-running")
    wait_for(qapp, lambda: manager.get_status("long-running") == "Stopped")
    wait_for(qapp, lambda: not pid_is_alive(first_pid))
    assert manager.get_pid("long-running") is None

    manager.restart("long-running")
    wait_for(qapp, lambda: manager.get_status("long-running") == "Running")
    second_pid = manager.get_pid("long-running")
    assert second_pid is not None and pid_is_alive(second_pid)
    assert second_pid != first_pid

    manager.stop("long-running")
    wait_for(qapp, lambda: manager.get_status("long-running") == "Stopped")
    wait_for(qapp, lambda: not pid_is_alive(second_pid))


def test_stop_terminates_real_parent_and_child_process_tree(tmp_path, qapp, manager):
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
        "time.sleep(600)\n"
    )
    parent = write_script(
        tmp_path,
        "parent_tree.py",
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "print('parent-ready', flush=True)\n"
        "time.sleep(600)\n",
    )
    register(manager, python_config(parent, "tree"))

    manager.start("tree")
    wait_for(qapp, lambda: manager.get_status("tree") == "Running")
    wait_for(qapp, lambda: pid_file_ready(child_pid_file), message="child did not start")
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    parent_pid = manager.get_pid("tree")
    assert parent_pid and pid_is_alive(parent_pid)
    assert pid_is_alive(child_pid)

    manager.stop("tree")
    wait_for(qapp, lambda: manager.get_status("tree") == "Stopped")
    wait_for(qapp, lambda: not pid_is_alive(parent_pid) and not pid_is_alive(child_pid))
    assert not pid_is_alive(parent_pid)
    assert not pid_is_alive(child_pid)


def test_parent_exit_closes_job_and_does_not_orphan_child(tmp_path, qapp, manager):
    """A naturally exiting parent must still take its Job Object descendants down."""

    child_pid_file = tmp_path / "orphan.pid"
    child_code = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
        "time.sleep(600)\n"
    )
    parent = write_script(
        tmp_path,
        "parent_exits.py",
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"deadline = time.monotonic() + 5\n"
        f"while not __import__('os').path.exists({str(child_pid_file)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(.01)\n"
        "print('parent-exit', flush=True)\n",
    )
    register(manager, python_config(parent, "parent-exits"))

    manager.start("parent-exits")
    wait_for(qapp, lambda: pid_file_ready(child_pid_file), message="child did not start")
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    wait_for(qapp, lambda: manager.get_status("parent-exits") in {"Stopped", "Failed"})
    assert manager.get_status("parent-exits") == "Stopped"
    wait_for(qapp, lambda: not pid_is_alive(child_pid), timeout=10, message="job left an orphan child")
    assert not pid_is_alive(child_pid)


@pytest.mark.parametrize("suffix", [".bat", ".cmd"])
def test_batch_and_cmd_scripts_work_when_path_contains_spaces(tmp_path, qapp, manager, suffix):
    script_dir = tmp_path / "batch folder with spaces"
    script_dir.mkdir()
    script = write_script(
        script_dir,
        f"hello world{suffix}",
        "@echo off\n"
        "echo batch stdout\n"
        "echo batch stderr 1>&2\n",
    )
    config = TestConfig(
        id=f"batch-{suffix[1:]}",
        path=str(script),
        working_directory=str(script_dir),
        type="bat",
    )
    register(manager, config)

    manager.start(config.id)
    wait_for(qapp, lambda: manager.get_status(config.id) in {"Stopped", "Failed"})

    assert manager.get_status(config.id) == "Stopped"
    assert "batch stdout" in logs_for(manager, config.id, "stdout")
    assert any(line.strip() == "batch stderr" for line in logs_for(manager, config.id, "stderr"))


def test_batch_arguments_with_spaces_are_passed_as_single_arguments(tmp_path, qapp, manager):
    script_dir = tmp_path / "batch args folder with spaces"
    script_dir.mkdir()
    script = write_script(
        script_dir,
        "echo args.bat",
        "@echo off\n"
        "echo first=[%~1]\n"
        "echo second=[%~2]\n",
    )
    config = TestConfig(
        id="batch-args",
        path=str(script),
        args=["first value", "second value"],
        working_directory=str(script_dir),
        type="bat",
    )
    register(manager, config)

    manager.start(config.id)
    wait_for(qapp, lambda: manager.get_status(config.id) in {"Stopped", "Failed"})

    assert manager.get_status(config.id) == "Stopped"
    stdout = logs_for(manager, config.id, "stdout")
    assert any(line.strip() == "first=[first value]" for line in stdout)
    assert any(line.strip() == "second=[second value]" for line in stdout)


def test_batch_script_to_python_child_is_stopped_as_one_tree(tmp_path, qapp, manager):
    script_dir = tmp_path / "batch tree folder with spaces"
    script_dir.mkdir()
    child_pid_file = script_dir / "batch-child.pid"
    worker = write_script(
        script_dir,
        "worker process.py",
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')\n"
        "print('worker-ready', flush=True)\n"
        "time.sleep(600)\n",
    )
    batch = write_script(
        script_dir,
        "launch worker.bat",
        "@echo off\n"
        f'"{sys.executable}" "{worker}" "{child_pid_file}"\n',
    )
    config = TestConfig(
        id="batch-tree",
        path=str(batch),
        working_directory=str(script_dir),
        type="bat",
    )
    register(manager, config)

    manager.start(config.id)
    wait_for(qapp, lambda: manager.get_status(config.id) == "Running")
    wait_for(qapp, lambda: pid_file_ready(child_pid_file), message="batch child did not start")
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    batch_pid = manager.get_pid(config.id)
    assert batch_pid and pid_is_alive(batch_pid)
    assert pid_is_alive(child_pid)

    manager.stop(config.id)
    wait_for(qapp, lambda: manager.get_status(config.id) == "Stopped")
    wait_for(qapp, lambda: not pid_is_alive(batch_pid) and not pid_is_alive(child_pid))
    assert not pid_is_alive(batch_pid)
    assert not pid_is_alive(child_pid)


def test_javascript_script_when_node_is_available(tmp_path, qapp, manager):
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    script = write_script(
        tmp_path,
        "hello.js",
        "console.log('js stdout');\nconsole.error('js stderr');\n",
    )
    config = TestConfig(
        id="javascript",
        path=str(script),
        working_directory=str(tmp_path),
        interpreter=node,
        type="js",
    )
    register(manager, config)

    manager.start("javascript")
    wait_for(qapp, lambda: manager.get_status("javascript") in {"Stopped", "Failed"})

    assert manager.get_status("javascript") == "Stopped"
    assert "js stdout" in logs_for(manager, "javascript", "stdout")
    assert "js stderr" in logs_for(manager, "javascript", "stderr")


def test_executable_script_can_be_launched_directly(tmp_path, qapp, manager):
    # Python's interpreter is a real .exe and exits cleanly with stdin set to
    # DEVNULL, allowing the direct-executable branch to be tested without a
    # compiler or a checked-in binary fixture.
    executable = Path(sys.executable)
    assert executable.suffix.lower() == ".exe"
    config = TestConfig(
        id="executable",
        path=str(executable),
        working_directory=str(tmp_path),
        type="exe",
    )
    register(manager, config)

    manager.start("executable")
    wait_for(qapp, lambda: manager.get_status("executable") in {"Stopped", "Failed"})

    assert manager.get_status("executable") == "Stopped"
    assert manager.get_pid("executable") is None
