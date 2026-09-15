"""Threaded process lifecycle; UI never waits for a child process."""
from collections import deque
import os
import shutil
import subprocess
import threading

import psutil
from PySide6.QtCore import QObject, Signal, QTimer

from core.log_reader import read_pipe
from core.windows_job import WindowsJob


def terminate_process_tree(pid):
    """Terminate descendants before their parent; handle concurrent exits."""
    try:
        parent = psutil.Process(pid)
        processes = parent.children(recursive=True)[::-1] + [parent]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=5)
    if alive:
        raise RuntimeError("部分子进程未能停止，请检查权限。")


class ProcessManager(QObject):
    status_changed = Signal(str, str)
    log_received = Signal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._configs = {}
        self._processes = {}
        self._jobs = {}
        self._statuses = {}
        self._logs = {}
        self._busy = set()
        self._threads = set()
        self._lock = threading.RLock()
        self._closing = False
        # A bounded queue keeps high-volume stdout from flooding Qt's event queue.
        self._pending_logs = deque(maxlen=5000)
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(50)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start()

    def register(self, config):
        with self._lock:
            if self.is_running(config.id) or config.id in self._busy:
                raise ValueError("请先停止脚本。")
            self._configs[config.id] = config
            self._statuses.setdefault(config.id, "Stopped")
            self._logs.setdefault(config.id, deque(maxlen=5000))

    def unregister(self, script_id):
        with self._lock:
            if self.is_running(script_id) or script_id in self._busy:
                raise ValueError("请先停止脚本。")
            for mapping in (self._configs, self._statuses, self._logs, self._processes):
                mapping.pop(script_id, None)

    def get_status(self, script_id):
        with self._lock:
            return self._statuses.get(script_id, "Stopped")

    def get_pid(self, script_id):
        with self._lock:
            process = self._processes.get(script_id)
            return process.pid if process and process.poll() is None else None

    def is_running(self, script_id):
        return self.get_pid(script_id) is not None

    def get_logs(self, script_id):
        with self._lock:
            return list(self._logs.get(script_id, []))

    def _status(self, script_id, status):
        with self._lock:
            self._statuses[script_id] = status
        self.status_changed.emit(script_id, status)

    def _log(self, script_id, stream, line):
        with self._lock:
            if script_id not in self._configs:
                return
            self._logs.setdefault(script_id, deque(maxlen=5000)).append((stream, line))
            self._pending_logs.append((script_id, stream, line))

    def _flush_logs(self):
        with self._lock:
            batch = [self._pending_logs.popleft() for _ in range(min(500, len(self._pending_logs)))]
        for entry in batch:
            if entry[0] in self._configs:
                self.log_received.emit(*entry)

    def _spawn(self, target, *args):
        def run():
            try:
                target(*args)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def _command(self, config):
        config.validate()
        path = os.path.abspath(config.path)
        suffix = os.path.splitext(path)[1].lower()
        if suffix in (".py", ".js"):
            interpreter = config.interpreter or shutil.which("python.exe" if suffix == ".py" else "node.exe")
            if not interpreter:
                raise ValueError("找不到解释器，请在编辑脚本中指定 Python / Node 的绝对路径。")
            return [interpreter, *(["-u"] if suffix == ".py" else []), path, *config.args]
        if suffix in (".bat", ".cmd"):
            # cmd has a separate quoting grammar. Forbid expansion/metacharacters
            # rather than accidentally executing a second command from arguments.
            values = [path, *config.args]
            if any(any(c in value for c in '\"% !&|<>^\r\n'.replace(' ', '')) for value in values):
                raise ValueError("BAT/CMD 路径或参数不支持引号及 % ! & | < > ^ 等命令字符。")
            command = '"' + ' '.join('"' + value + '"' for value in values) + '"'
            # Pass cmd's own quote syntax verbatim, not through CRT list2cmdline.
            executable = os.environ.get("COMSPEC", "cmd.exe")
            return f'"{executable}" /d /s /c {command}'
        return [path, *config.args]

    def _launch(self, script_id):
        process = None
        job = None
        try:
            config = self._configs[script_id]
            command = self._command(config)
            env = os.environ.copy()
            env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            job = WindowsJob()
            process = subprocess.Popen(command, cwd=config.working_directory or os.path.dirname(os.path.abspath(config.path)),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       bufsize=0, env=env, creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            job.assign_and_resume(process)
            with self._lock:
                previous_job = self._jobs.pop(script_id, None)
                if previous_job:
                    previous_job.close()
                self._processes[script_id] = process
                self._jobs[script_id] = job
            readers = []
            for stream, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = self._spawn(read_pipe, pipe, lambda line, s=stream: self._log(script_id, s, line))
                readers.append(thread)
            self._status(script_id, "Running")
            self._spawn(self._watch, script_id, process, readers)
        except Exception as error:
            if process is not None:
                process.kill()
                process.wait()
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()
            if job is not None:
                job.close()
            self._log(script_id, "stderr", str(error))
            self._status(script_id, "Failed")

    def _watch(self, script_id, process, readers):
        code = process.wait()
        with self._lock:
            if self._processes.get(script_id) is process:
                job = self._jobs.pop(script_id, None)
                if job:
                    job.close()
        for reader in readers:
            reader.join(timeout=1)
        with self._lock:
            if self._processes.get(script_id) is not process or script_id in self._busy:
                return
            self._status(script_id, "Stopped" if code == 0 else "Failed")
        self._log(script_id, "system", f"进程退出，退出码 {code}")

    def _schedule(self, script_id, action):
        with self._lock:
            if self._closing or script_id in self._busy or script_id not in self._configs:
                return
            if action == "start" and self.is_running(script_id):
                return
            self._busy.add(script_id)
            if action != "stop":
                self._status(script_id, "Starting")
        def work():
            try:
                if action in ("stop", "restart"):
                    pid = self.get_pid(script_id)
                    if pid:
                        terminate_process_tree(pid)
                    with self._lock:
                        job = self._jobs.pop(script_id, None)
                        if job:
                            job.terminate()
                            job.close()
                    self._status(script_id, "Stopped")
                if action in ("start", "restart"):
                    self._launch(script_id)
            except Exception as error:
                self._log(script_id, "stderr", str(error))
                self._status(script_id, "Failed")
            finally:
                with self._lock:
                    self._busy.discard(script_id)
                    process = self._processes.get(script_id)
                    if process and process.poll() is not None and action != "stop" and self.get_status(script_id) == "Running":
                        self._status(script_id, "Stopped" if process.returncode == 0 else "Failed")
        self._spawn(work)

    def start(self, script_id):
        self._schedule(script_id, "start")

    def stop(self, script_id):
        self._schedule(script_id, "stop")

    def restart(self, script_id):
        self._schedule(script_id, "restart")

    def shutdown(self):
        # Called off the GUI thread on explicit application exit.
        self._closing = True
        while True:
            with self._lock:
                busy = bool(self._busy)
            if not busy:
                break
            threading.Event().wait(.05)
        errors = []
        for script_id in list(self._configs):
            pid = self.get_pid(script_id)
            if pid:
                try:
                    terminate_process_tree(pid)
                except Exception as error:
                    errors.append(str(error))
            with self._lock:
                job = self._jobs.pop(script_id, None)
                if job:
                    try:
                        job.terminate()
                        job.close()
                    except Exception as error:
                        errors.append(str(error))
        with self._lock:
            remaining = list(self._threads)
        for thread in remaining:
            thread.join(timeout=6)
        if any(thread.is_alive() for thread in remaining):
            errors.append("后台进程读取线程未完成退出。")
        if errors:
            self._closing = False
            raise RuntimeError("\n".join(errors))

    terminate_process_tree = staticmethod(terminate_process_tree)
