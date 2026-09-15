import pytest
from PySide6.QtWidgets import QSystemTrayIcon
from main import handle_tray_activation


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
