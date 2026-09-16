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
    # ``object`` deliberately keeps the full Windows DWORD exit code (for
    # example 0xC0000005 == 3221225477) instead of narrowing it through a
    # signed Qt integer conversion.
    abnormal_exit = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._configs = {}
        self._processes = {}
        self._jobs = {}
        # A process can outlive the mapping entry that points at it (for
        # example while a restart replaces it).  Keep per-process lifecycle
        # state so an old watcher can never mutate the replacement's status.
        self._process_states = {}
        self._next_generation = 0
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

    def get_config(self, script_id):
        """Return the registered configuration for ``script_id`` safely."""
        with self._lock:
            return self._configs.get(script_id)

    def get_pid(self, script_id):
        with self._lock:
            process = self._processes.get(script_id)
            return process.pid if process and process.poll() is None else None

    def get_web_url(self, script_id) -> str | None:
        """Return a deterministic localhost URL for a running process tree.

        The managed subprocess is the entry point for a script.  Inspect it
        and all of its recursive descendants so a script that delegates its
        HTTP server to a worker process is still discoverable.  Socket
        inspection is deliberately best-effort: processes can disappear or
        become inaccessible while the tree is being sampled.
        """
        with self._lock:
            process = self._processes.get(script_id)
            if process is None:
                return None
            try:
                if process.poll() is not None:
                    return None
                pid = process.pid
            except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
                return None

        try:
            root = psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
            ports = set()
            for tree_process in processes:
                try:
                    connections = tree_process.net_connections(kind="tcp")
                except (psutil.Error, OSError, ProcessLookupError, TypeError, ValueError):
                    # A descendant can exit or become inaccessible while the
                    # process tree is being sampled.  Keep ports found on the
                    # remaining processes instead of failing the whole scan.
                    continue
                for connection in connections:
                    if getattr(connection, "status", None) != psutil.CONN_LISTEN:
                        continue
                    # A LISTEN socket normally has no remote address.  Keep
                    # this guard explicit so an unusual psutil record cannot
                    # make an established/remote connection look like a URL.
                    if getattr(connection, "raddr", None):
                        continue
                    local_address = getattr(connection, "laddr", None)
                    if local_address is None:
                        continue
                    port = getattr(local_address, "port", None)
                    if port is None:
                        try:
                            port = local_address[1]
                        except (IndexError, KeyError, TypeError):
                            continue
                    try:
                        port = int(port)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= port <= 65535:
                        ports.add(port)
        except (psutil.Error, OSError, ProcessLookupError, TypeError, ValueError):
            return None

        if not ports:
            return None

        # The process may have exited (or been replaced) during psutil's
        # snapshot.  Do not return a stale URL for a stopped/restarted entry.
        with self._lock:
            if self._processes.get(script_id) is not process:
                return None
            try:
                if process.poll() is not None:
                    return None
            except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
                return None
        return f"http://127.0.0.1:{min(ports)}"

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
        """Create one process and install its watcher.

        A process can finish before this method returns.  The per-process
        state and startup event let the watcher defer exit handling until the
        launch operation has released ``_busy`` without losing the exit.
        """
        process = None
        job = None
        state = None
        readers = []
        stale_job = None
        try:
            with self._lock:
                config = self._configs.get(script_id)
            if config is None:
                raise ValueError("脚本不存在。")

            # A previous fast-exiting process may still be awaiting its
            # watcher while this new start is scheduled.  Detach that dead
            # mapping before attempting the new launch so a launch failure
            # cannot later be overwritten by the old watcher's status.
            with self._lock:
                previous_process = self._processes.get(script_id)
                if previous_process is not None and previous_process.poll() is not None:
                    self._processes.pop(script_id, None)
                    stale_job = self._jobs.pop(script_id, None)
            if stale_job:
                stale_job.close()

            command = self._command(config)
            env = os.environ.copy()
            env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            job = WindowsJob()
            process = subprocess.Popen(
                command,
                cwd=config.working_directory or os.path.dirname(os.path.abspath(config.path)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4,
            )
            job.assign_and_resume(process)

            with self._lock:
                self._next_generation += 1
                state = {
                    "process": process,
                    "job": job,
                    "generation": self._next_generation,
                    "suppress_exit": False,
                    "startup_failed": False,
                    "exit_handled": False,
                    "startup_done": threading.Event(),
                }
                previous_job = self._jobs.pop(script_id, None)
                self._processes[script_id] = process
                self._jobs[script_id] = job
                self._process_states[id(process)] = state
            # The previous job belongs to a stale/dead process.  Its watcher
            # checks identity and will not close this newly installed job.
            if previous_job:
                previous_job.close()

            for stream, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = self._spawn(
                    read_pipe,
                    pipe,
                    lambda line, s=stream: self._log(script_id, s, line),
                )
                readers.append(thread)
            self._status(script_id, "Running")
            self._spawn(self._watch, script_id, process, readers, state)
            # Set this only after all launch plumbing is installed.  If setup
            # fails, the exception path sets it with startup_failed=True.
            state["startup_done"].set()
        except Exception as error:
            if state is not None:
                with self._lock:
                    state["startup_failed"] = True
                    state["suppress_exit"] = True
                    state["startup_done"].set()
                    if self._processes.get(script_id) is process:
                        self._processes.pop(script_id, None)
                    if self._jobs.get(script_id) is job:
                        self._jobs.pop(script_id, None)
                    self._process_states.pop(id(process), None)
            if process is not None:
                try:
                    process.kill()
                    process.wait()
                except (OSError, ProcessLookupError):
                    pass
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        try:
                            pipe.close()
                        except (OSError, ValueError):
                            pass
            if job is not None:
                try:
                    job.close()
                except Exception:
                    pass
            self._log(script_id, "stderr", str(error))
            self._status(script_id, "Failed")

    def _wait_for_operation(self, script_id):
        """Wait until the start/stop/restart operation has settled."""
        while True:
            with self._lock:
                if script_id not in self._busy:
                    return
            # Do not hold the manager lock while yielding to the operation.
            threading.Event().wait(.01)

    def _watch(self, script_id, process, readers, state=None):
        """Record one process exit and optionally notify the GUI.

        Exactly one watcher owns exit logging, final natural-exit status, and
        abnormal-exit notification.  Manual termination marks the state as
        suppressed, while identity checks prevent an old watcher from
        changing a replacement process's status.
        """
        if state is None:
            with self._lock:
                state = self._process_states.get(id(process))
            if state is None:
                state = {
                    "process": process,
                    "generation": None,
                    "suppress_exit": False,
                    "startup_failed": False,
                    "exit_handled": False,
                    "startup_done": threading.Event(),
                }
                state["startup_done"].set()

        code = process.wait()
        # A fast process may exit while _launch is still setting up readers.
        # Waiting for this event keeps setup failures from being reported as
        # abnormal exits and guarantees the normal launch path is complete.
        state["startup_done"].wait()
        self._wait_for_operation(script_id)

        # Close the job before joining pipe readers.  Keep the process and
        # state mappings intact for now: a restart may install a replacement
        # while readers drain, and the final identity check below must then
        # avoid emitting a stale status for that replacement.
        with self._lock:
            if state["exit_handled"]:
                return
            state["exit_handled"] = True
            job = state.get("job")
            if job is not None and self._jobs.get(script_id) is job:
                self._jobs.pop(script_id, None)

        # Close the Job Object before joining pipe readers.  A child that
        # inherited stdout/stderr can otherwise keep those readers blocked
        # until the join timeout, and descendants would remain alive longer
        # than the parent exit event.
        if job:
            job.close()
        for reader in readers:
            reader.join(timeout=1)

        while True:
            with self._lock:
                # Re-check after reader draining.  A new start/restart may
                # have begun between the first idle wait and this final lock,
                # before it installs its replacement process.  Defer the
                # decision until that operation settles instead of emitting a
                # stale Failed/Stopped status over Starting.
                if script_id in self._busy:
                    retry = True
                else:
                    retry = False
                    current = self._processes.get(script_id) is process
                    suppress = bool(state["suppress_exit"] or state["startup_failed"] or self._closing)
                    if current:
                        self._processes.pop(script_id, None)
                        final_status = None if suppress else ("Stopped" if code == 0 else "Failed")
                        if final_status is not None:
                            self._statuses[script_id] = final_status
                    else:
                        final_status = None
                    config_exists = script_id in self._configs
                    self._process_states.pop(id(process), None)

                    # Keep the exit record even when a restart has already
                    # installed a newer process; it is useful history and
                    # does not alter new state.
                    self._log(script_id, "system", f"进程退出，退出码 {code}")
                    # Emit while holding the same lock as the identity check.
                    # A new operation cannot enqueue Starting/Running ahead
                    # of this event and then have the old event overwrite the
                    # UI state.
                    if final_status is not None:
                        self.status_changed.emit(script_id, final_status)
                    if code != 0 and not suppress and config_exists:
                        self.abnormal_exit.emit(script_id, code)
            if not retry:
                return
            self._wait_for_operation(script_id)

    def _schedule(self, script_id, action):
        with self._lock:
            if self._closing or script_id in self._busy or script_id not in self._configs:
                return
            if action == "start" and self.is_running(script_id):
                return
            if action in ("stop", "restart"):
                # Set suppression before the worker gets a chance to kill the
                # process.  A watcher that wins the exit race will therefore
                # still classify this as an intentional termination.
                process = self._processes.get(script_id)
                if process is not None:
                    state = self._process_states.get(id(process))
                    if state is not None:
                        state["suppress_exit"] = True
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
        self._spawn(work)

    def start(self, script_id):
        self._schedule(script_id, "start")

    def stop(self, script_id):
        self._schedule(script_id, "stop")

    def restart(self, script_id):
        self._schedule(script_id, "restart")

    def shutdown(self):
        # Called off the GUI thread on explicit application exit.
        with self._lock:
            self._closing = True
            # Shutdown is an explicit, intentional termination for every
            # current process, including one that exits while _busy drains.
            for process in self._processes.values():
                state = self._process_states.get(id(process))
                if state is not None:
                    state["suppress_exit"] = True
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
