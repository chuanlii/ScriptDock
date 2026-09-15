"""Isolated source-app lifecycle check; never touches the user's configuration."""
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

import main as entry
from core.config_manager import ConfigManager
from ui.main_window import MainWindow

temporary = tempfile.TemporaryDirectory()
entry.os.environ['USERNAME'] = 'ScriptDock-verify-' + uuid.uuid4().hex
entry.ConfigManager = lambda: ConfigManager(Path(temporary.name) / 'config.json')
entry.QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes
failures = []
checks = []
original_exec = QApplication.exec


class VerifyApplication(QApplication):
    def exec(self):
        def check():
            try:
                window = next(widget for widget in self.topLevelWidgets() if isinstance(widget, MainWindow))
                tray = self.findChild(QSystemTrayIcon)
                assert window.isVisible() and tray.isVisible()
                window.close()
                assert not window.isVisible() and tray.isVisible()
                tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
                assert window.isVisible()
                window.close()
                tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
                assert window.isVisible()
                checks.append(True)
                window.quit_requested.emit()
            except Exception as error:
                failures.append(str(error))
                self.quit()
        QTimer.singleShot(200, check)
        QTimer.singleShot(10000, lambda: (failures.append('退出超时'), self.quit()))
        return original_exec()


entry.QApplication = VerifyApplication
code = entry.main()
assert not failures, failures
assert checks, '生命周期检查未执行'
assert code == 0
print('PASS: tray visible, close hides, single/double click restores, explicit exit completes')
