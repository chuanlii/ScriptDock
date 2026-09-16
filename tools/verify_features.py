"""Isolated application startup/notification check without user configuration."""
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

import main as entry
from core.config_manager import ConfigManager
from models.script_config import ScriptConfig
from ui.main_window import MainWindow


def verify():
    with tempfile.TemporaryDirectory(prefix="scriptdock-features-") as directory:
        root = Path(directory)
        worker = root / "worker.py"
        worker.write_text(
            "import socket, time\n"
            "server = socket.socket()\n"
            "server.bind(('127.0.0.1', 0))\n"
            "server.listen()\n"
            "print(server.getsockname()[1], flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        crash = root / "crash.py"
        crash.write_text("raise SystemExit(7)\n", encoding="utf-8")
        config = ConfigManager(root / "config.json")
        config.save([
            ScriptConfig("enabled", "自启动服务", "python", str(worker),
                         interpreter=sys.executable, auto_start=True),
            ScriptConfig("disabled", "手动服务", "python", str(worker), interpreter=sys.executable),
            ScriptConfig("crash", "异常服务", "python", str(crash), interpreter=sys.executable, auto_start=True),
        ])
        entry.os.environ["USERNAME"] = "ScriptDock-features-" + uuid.uuid4().hex
        entry.ConfigManager = lambda: config
        entry.QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        messages = []
        failures = []
        checks = []
        original_exec = QApplication.exec

        class VerifyTray(QSystemTrayIcon):
            def showMessage(self, *args):
                messages.append(args)

        class VerifyApplication(QApplication):
            def exec(self):
                timer = QTimer(self)
                attempts = 0

                def check():
                    nonlocal attempts
                    attempts += 1
                    window = next(w for w in self.topLevelWidgets() if isinstance(w, MainWindow))
                    manager = window.manager
                    ready = (manager.get_status("enabled") == "Running"
                             and manager.get_status("crash") == "Failed"
                             and messages
                             and window._row_controls["enabled"]["web"].isEnabled())
                    if not ready and attempts < 100:
                        return
                    timer.stop()
                    try:
                        assert ready, "自启动/异常退出超时"
                        assert manager.get_status("disabled") == "Stopped"
                        assert manager.get_pid("disabled") is None
                        assert len(messages) == 1 and "异常服务" in messages[0][1]
                        assert window.script_table.columnCount() == 5
                        assert window._row_controls["enabled"]["web"].isEnabled()
                        assert not window._row_controls["disabled"]["web"].isEnabled()
                        checks.append(True)
                    except Exception as error:
                        failures.append(str(error))
                    window.quit_requested.emit()

                timer.timeout.connect(check)
                timer.start(100)
                QTimer.singleShot(15000, lambda: (failures.append("退出超时"), self.quit()))
                return original_exec()

        entry.QSystemTrayIcon = VerifyTray
        entry.QApplication = VerifyApplication
        code = entry.main()
        assert checks and not failures and code == 0, failures
        print("PASS: selected auto-start, crash notification, detected listening port web button, shutdown")


if __name__ == "__main__":
    verify()
