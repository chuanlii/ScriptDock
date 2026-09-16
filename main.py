"""ScriptDock Windows tray application entry point."""
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot, Qt, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from core.config_manager import ConfigManager
from core.process_manager import ProcessManager
from ui.main_window import MainWindow


class ExitBridge(QObject):
    finished = Signal(str)


class ScriptNotifications(QObject):
    """Receive worker signals on the GUI thread before showing tray messages."""

    def __init__(self, tray, manager, parent=None):
        super().__init__(parent)
        self.tray = tray
        self.manager = manager
        self.enabled = True

    @Slot(str, object)
    def abnormal_exit(self, script_id, code):
        if not self.enabled:
            return
        config = self.manager.get_config(script_id)
        if config is None:
            return
        self.tray.showMessage(
            "ScriptDock · 脚本异常退出",
            f"{config.name} 脚本发生异常退出（退出码 {code}），请查看输出日志。",
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )


def start_auto_scripts(manager, scripts):
    for script in scripts:
        if script.auto_start:
            manager.start(script.id)


def handle_tray_activation(reason, show_window):
    if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                  QSystemTrayIcon.ActivationReason.DoubleClick):
        show_window()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ScriptDock")
    app.setQuitOnLastWindowClosed(False)
    if sys.platform != "win32":
        QMessageBox.critical(None, "ScriptDock", "本应用仅支持 Windows 10 / 11。")
        return 1
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "ScriptDock", "系统托盘不可用，无法安全隐藏窗口。")
        return 1
    # A second launch opens the existing manager instead of owning duplicate services.
    server_name = "ScriptDock-" + os.environ.get("USERNAME", "user")
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(500):
        socket.write(b"show")
        socket.waitForBytesWritten(500)
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    if not server.listen(server_name):
        QMessageBox.critical(None, "ScriptDock", "无法建立单实例连接。")
        return 1
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    icon = QIcon(str(base / "assets" / "tray.svg"))
    app.setWindowIcon(icon)
    config_manager = ConfigManager()
    scripts = config_manager.load()
    manager = ProcessManager()
    for script in scripts:
        manager.register(script)
    window = MainWindow(manager, config_manager, scripts)
    tray = QSystemTrayIcon(icon, app)
    notifications = ScriptNotifications(tray, manager, app)
    manager.abnormal_exit.connect(notifications.abnormal_exit)
    tray.setToolTip("ScriptDock · 脚本托盘管理器")
    menu = QMenu()
    def show_window():
        window.showNormal()
        window.raise_()
        window.activateWindow()
    open_action = QAction("打开管理器", menu)
    open_action.triggered.connect(show_window)
    menu.addAction(open_action)
    menu.addSeparator()
    exit_action = QAction("退出（停止全部脚本）", menu)
    menu.addAction(exit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: handle_tray_activation(reason, show_window))
    bridge = ExitBridge()
    exiting = False
    def request_exit():
        nonlocal exiting
        if exiting:
            return
        if QMessageBox.question(window, "退出 ScriptDock", "退出将停止所有管理的脚本及子进程。是否继续？",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        exiting = True
        notifications.enabled = False
        window.setEnabled(False)
        exit_action.setEnabled(False)
        def finish():
            try:
                manager.shutdown()
                bridge.finished.emit("")
            except Exception as error:
                bridge.finished.emit(str(error))
        threading.Thread(target=finish, daemon=True).start()
    def exit_finished(error):
        nonlocal exiting
        if error:
            exiting = False
            notifications.enabled = True
            window.setEnabled(True)
            exit_action.setEnabled(True)
            show_window()
            QMessageBox.critical(window, "停止失败", error)
        else:
            tray.hide()
            server.close()
            app.quit()
    bridge.finished.connect(exit_finished)
    exit_action.triggered.connect(request_exit)
    window.quit_requested.connect(request_exit)
    def new_connection():
        connection = server.nextPendingConnection()
        if connection:
            show_window()
            connection.disconnectFromServer()
            connection.deleteLater()
    server.newConnection.connect(new_connection)
    tray.show()
    show_window()
    QTimer.singleShot(0, lambda: start_auto_scripts(manager, scripts))
    if config_manager.last_error:
        QTimer.singleShot(0, lambda: QMessageBox.warning(window, "配置加载提醒", config_manager.last_error))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
