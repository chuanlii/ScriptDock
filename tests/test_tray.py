import pytest
from PySide6.QtWidgets import QSystemTrayIcon
from main import handle_tray_activation
from main import ScriptNotifications, start_auto_scripts
from types import SimpleNamespace
import threading
import time
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication


@pytest.mark.parametrize('reason,expected', [
    (QSystemTrayIcon.ActivationReason.Trigger, 1),
    (QSystemTrayIcon.ActivationReason.DoubleClick, 1),
    (QSystemTrayIcon.ActivationReason.Context, 0),
    (QSystemTrayIcon.ActivationReason.MiddleClick, 0),
    (QSystemTrayIcon.ActivationReason.Unknown, 0),
])
def test_tray_click_opens_manager(reason, expected):
    calls = []
    handle_tray_activation(reason, lambda: calls.append(True))
    assert len(calls) == expected


def test_auto_start_only_enabled_scripts():
    calls = []
    manager = SimpleNamespace(start=calls.append)
    scripts = [SimpleNamespace(id="yes", auto_start=True),
               SimpleNamespace(id="no", auto_start=False)]
    start_auto_scripts(manager, scripts)
    assert calls == ["yes"]


def test_abnormal_notification_names_script_and_can_be_suppressed():
    calls = []
    tray = SimpleNamespace(showMessage=lambda *args: calls.append(args))
    configs = {"script": SimpleNamespace(name="测试服务")}
    manager = SimpleNamespace(get_config=configs.get)
    notifications = ScriptNotifications(tray, manager)
    notifications.abnormal_exit("script", 7)
    assert len(calls) == 1
    assert "测试服务 脚本发生异常退出" in calls[0][1]
    assert "7" in calls[0][1]
    notifications.enabled = False
    notifications.abnormal_exit("script", 9)
    notifications.enabled = True
    notifications.abnormal_exit("deleted", 9)
    assert len(calls) == 1


def test_worker_notification_is_delivered_on_gui_thread():
    app = QApplication.instance() or QApplication([])
    class Events(QObject):
        abnormal_exit = Signal(str, object)
    events = Events()
    calls = []
    tray = SimpleNamespace(showMessage=lambda *args: calls.append((threading.get_ident(), args)))
    config = SimpleNamespace(name="后台服务")
    notifications = ScriptNotifications(tray, SimpleNamespace(get_config=lambda _: config))
    events.abnormal_exit.connect(notifications.abnormal_exit)
    thread = threading.Thread(target=lambda: events.abnormal_exit.emit("service", 0xC0000005))
    thread.start()
    thread.join()
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert len(calls) == 1
    assert calls[0][0] == threading.get_ident()
    assert "3221225477" in calls[0][1][1]
