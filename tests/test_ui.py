"""Offscreen UI tests for the ScriptDock MVP.

The tests use small QObject doubles instead of launching real child processes.
This keeps the checks deterministic while exercising the signals and public
constructor contracts used by ``main.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ScriptDock is a Windows desktop application, but Qt can render these tests
# without a display server. Set this before importing any Qt module.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from models import ScriptConfig
from ui.main_window import MainWindow
from ui.script_dialog import ScriptDialog


@pytest.fixture(scope="session")
def qapp():
    """Provide one offscreen QApplication for the complete test session."""

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def close_widgets():
    """Close test windows and drain deferred QObject deletion callbacks."""

    windows = []
    yield windows
    for window in windows:
        timer = getattr(window, "_log_timer", None)
        if timer is not None:
            timer.stop()
        window.close()
        window.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


class FakeManager(QObject):
    """Minimal ProcessManager-shaped double with real Qt signals."""

    status_changed = Signal(str, str)
    log_received = Signal(str, str, str)

    def __init__(self, scripts=()):
        super().__init__()
        self.statuses = {script.id: "Stopped" for script in scripts}
        self.logs = {script.id: [] for script in scripts}
        self.calls = []

    def register(self, config):
        self.calls.append(("register", config.id))
        self.statuses.setdefault(config.id, "Stopped")
        self.logs.setdefault(config.id, [])

    def unregister(self, script_id):
        self.calls.append(("unregister", script_id))
        self.statuses.pop(script_id, None)
        self.logs.pop(script_id, None)

    def get_status(self, script_id):
        return self.statuses.get(script_id, "Stopped")

    def is_running(self, script_id):
        return self.get_status(script_id) == "Running"

    def get_logs(self, script_id):
        return list(self.logs.get(script_id, []))

    def start(self, script_id):
        self.calls.append(("start", script_id))

    def stop(self, script_id):
        self.calls.append(("stop", script_id))

    def restart(self, script_id):
        self.calls.append(("restart", script_id))

    def set_status(self, script_id, status):
        self.statuses[script_id] = status
        self.status_changed.emit(script_id, status)

    def emit_log(self, script_id, stream, line):
        self.logs.setdefault(script_id, []).append((stream, line))
        self.log_received.emit(script_id, stream, line)


class FakeConfigManager:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.saves = []

    def save(self, scripts):
        self.saves.append(list(scripts))
        if self.fail:
            raise OSError("disk full")


def make_config(
    script_id="demo",
    name="演示脚本",
    path=r"C:\Scripts\demo.py",
    *,
    args=None,
    working_directory="",
    interpreter=None,
):
    return ScriptConfig(
        id=script_id,
        name=name,
        type="python" if path.lower().endswith(".py") else "javascript",
        path=path,
        args=list(args or []),
        working_directory=working_directory,
        interpreter=interpreter,
    )


def patch_message_boxes(monkeypatch, *, question_answer=None):
    """Prevent a QMessageBox from entering a nested event loop in tests."""

    shown = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *args, **kwargs: shown.append(("critical", args))),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda *args, **kwargs: shown.append(("information", args))),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: shown.append(("warning", args))),
    )
    if question_answer is not None:
        monkeypatch.setattr(
            QMessageBox,
            "question",
            staticmethod(lambda *args, **kwargs: question_answer),
        )
    return shown


def test_script_dialog_required_name_and_path_validation(qapp):
    dialog = ScriptDialog()
    try:
        with pytest.raises(ValueError):
            dialog.get_config()

        dialog.name_edit.setText("必填测试")
        with pytest.raises(ValueError):
            dialog.get_config()

        dialog.path_edit.setText(r"C:\Scripts\not-supported.txt")
        with pytest.raises(ValueError):
            dialog.get_config()

        dialog.path_edit.setText(r"C:\Scripts\required.py")
        config = dialog.get_config()
        assert config.name == "必填测试"
        assert config.type == "python"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_script_dialog_windows_args_whitespace_chinese_and_quotes_roundtrip(qapp):
    args = [
        "--title",
        "你好 世界",
        "--path",
        r"C:\Program Files\工具\runner.py",
        "--empty",
        "",
        "--quote",
        '引号"值',
        "--trailing-slash",
        r"C:\Program Files\工具\\",
    ]
    original = make_config(args=args)
    dialog = ScriptDialog(config=original)
    try:
        # The edit form serializes list[str] to a Windows command line and
        # get_config() parses it back to the model representation.
        assert dialog.get_config().args == args
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_script_dialog_path_change_updates_type_interpreter_and_default_workdir(
    qapp, monkeypatch, tmp_path
):
    python_path = str(tmp_path / "one.py")
    javascript_path = str(tmp_path / "nested" / "two.js")
    python_exe = str(tmp_path / "Python" / "python.exe")
    node_exe = str(tmp_path / "Node" / "node.exe")

    def fake_which(name):
        return {
            "python.exe": python_exe,
            "python": python_exe,
            "node.exe": node_exe,
            "node": node_exe,
        }.get(name)

    monkeypatch.setattr("ui.script_dialog.shutil.which", fake_which)
    dialog = ScriptDialog()
    try:
        dialog.path_edit.setText(python_path)
        assert dialog.type_combo.currentData() == "python"
        assert Path(dialog.working_directory_edit.text()) == Path(python_path).parent
        assert dialog.interpreter_edit.text() == python_exe

        # Selecting a different interpreter-backed type should not leave the
        # old Python executable in the form.
        dialog.path_edit.setText(javascript_path)
        assert dialog.type_combo.currentData() == "javascript"
        assert Path(dialog.working_directory_edit.text()) == Path(javascript_path).parent
        assert dialog.interpreter_edit.text() == node_exe
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


class StubDialog:
    """Dialog double used to exercise MainWindow's CRUD transactions."""

    next_config = None
    opened = []

    def __init__(self, parent=None, config=None):
        self.config = config
        self.opened.append((parent, config))

    def exec(self):
        return QDialog.DialogCode.Accepted

    def get_config(self):
        return self.next_config


def build_window(qapp, scripts, *, save_fails=False, close_widgets=None):
    manager = FakeManager(scripts)
    config_manager = FakeConfigManager(fail=save_fails)
    window = MainWindow(manager, config_manager, scripts)
    if close_widgets is not None:
        close_widgets.append(window)
    qapp.processEvents()
    return window, manager, config_manager


def select_first(window, qapp):
    window.script_table.selectRow(0)
    qapp.processEvents()


def test_main_window_add_edit_delete_updates_rows_and_persists(
    qapp, monkeypatch, close_widgets
):
    original = make_config()
    added = make_config("added", "新增脚本", r"C:\Scripts\added.py")
    edited = make_config("demo", "修改后的脚本", r"C:\Scripts\edited.py")
    window, manager, config_manager = build_window(qapp, [original], close_widgets=close_widgets)
    patch_message_boxes(monkeypatch, question_answer=QMessageBox.StandardButton.Yes)
    monkeypatch.setattr("ui.main_window.ScriptDialog", StubDialog)
    StubDialog.opened = []

    StubDialog.next_config = added
    window.add_script()
    qapp.processEvents()
    assert [script.id for script in window.scripts] == ["demo", "added"]
    assert window.script_table.rowCount() == 2
    assert ("register", "added") in manager.calls

    select_first(window, qapp)
    StubDialog.next_config = edited
    window.edit_script()
    qapp.processEvents()
    assert window.scripts[0].name == "修改后的脚本"
    assert window.script_table.item(0, 0).text() == "修改后的脚本"
    assert ("register", "demo") in manager.calls

    # Select the added row before deleting it.
    window.script_table.selectRow(1)
    qapp.processEvents()
    window.delete_script()
    qapp.processEvents()
    assert [script.id for script in window.scripts] == ["demo"]
    assert window.script_table.rowCount() == 1
    assert ("unregister", "added") in manager.calls
    assert len(config_manager.saves) == 3


@pytest.mark.parametrize("operation", ["add", "edit", "delete"])
def test_main_window_save_failure_does_not_lose_configuration(
    qapp, monkeypatch, close_widgets, operation
):
    original = make_config()
    replacement = make_config("demo", "不应丢失", r"C:\Scripts\replacement.py")
    added = make_config("added", "保存失败新增", r"C:\Scripts\added.py")
    window, manager, config_manager = build_window(
        qapp, [original], save_fails=True, close_widgets=close_widgets
    )
    patch_message_boxes(monkeypatch, question_answer=QMessageBox.StandardButton.Yes)
    monkeypatch.setattr("ui.main_window.ScriptDialog", StubDialog)
    StubDialog.next_config = added if operation == "add" else replacement

    if operation == "add":
        window.add_script()
    elif operation == "edit":
        select_first(window, qapp)
        window.edit_script()
    else:
        select_first(window, qapp)
        window.delete_script()
    qapp.processEvents()

    assert window.scripts == [original]
    assert window.script_table.rowCount() == 1
    assert window.script_table.item(0, 0).text() == original.name
    assert manager.calls == []
    assert len(config_manager.saves) == 1


def test_running_script_disables_edit_delete_and_row_controls(
    qapp, monkeypatch, close_widgets
):
    script = make_config()
    window, manager, config_manager = build_window(qapp, [script], close_widgets=close_widgets)
    shown = patch_message_boxes(monkeypatch)
    monkeypatch.setattr("ui.main_window.ScriptDialog", StubDialog)
    StubDialog.opened = []
    select_first(window, qapp)

    manager.set_status(script.id, "Running")
    qapp.processEvents()
    controls = window._row_controls[script.id]
    assert not window.edit_button.isEnabled()
    assert not window.delete_button.isEnabled()
    assert not controls["start"].isEnabled()
    assert controls["stop"].isEnabled()
    assert controls["restart"].isEnabled()

    # Direct calls are guarded too; disabled buttons alone are not sufficient
    # protection for integrations invoking the slots programmatically.
    window.edit_script()
    window.delete_script()
    assert StubDialog.opened == []
    assert window.scripts == [script]
    assert config_manager.saves == []
    assert [kind for kind, _args in shown] == ["information", "information"]


def test_status_signal_updates_status_cell_and_row_button_states(
    qapp, close_widgets
):
    script = make_config()
    window, manager, _config_manager = build_window(qapp, [script], close_widgets=close_widgets)
    controls = window._row_controls[script.id]

    manager.set_status(script.id, "Stopped")
    qapp.processEvents()
    assert window.script_table.item(0, 2).text() == "Stopped"
    assert controls["start"].isEnabled()
    assert not controls["stop"].isEnabled()
    assert controls["restart"].isEnabled()

    manager.set_status(script.id, "Starting")
    qapp.processEvents()
    assert window.script_table.item(0, 2).text() == "Starting"
    assert not controls["start"].isEnabled()
    assert not controls["stop"].isEnabled()
    assert not controls["restart"].isEnabled()

    manager.set_status(script.id, "Running")
    qapp.processEvents()
    assert window.script_table.item(0, 2).text() == "Running"
    assert not controls["start"].isEnabled()
    assert controls["stop"].isEnabled()
    assert controls["restart"].isEnabled()


def test_log_filter_and_name_selection(qapp, close_widgets):
    first, second = make_config(), make_config(script_id="second", name="Second")
    window, manager, _ = build_window(qapp, [first, second], close_widgets=close_widgets)
    manager.logs[first.id] = [("stdout", "normal"), ("stderr", "error"), ("system", "exit")]
    manager.logs[second.id] = [("stdout", "second normal"), ("stderr", "second error")]
    window.script_table.selectRow(0)
    window._load_logs(first.id)
    assert window.script_table.columnCount() == 4
    assert window.log_filter.currentText() == "std"
    assert "normal" in window.log_view.toPlainText() and "error" in window.log_view.toPlainText()
    assert "exit" in window.log_view.toPlainText()
    window.log_filter.setCurrentText("stderr")
    assert window.log_view.toPlainText() == "[stderr] error"
    window.script_table.selectRow(1)
    assert window.log_view.toPlainText() == "[stderr] second error"
    manager.logs[second.id].append(("stderr", "new error"))
    manager.log_received.emit(second.id, "stderr", "new error")
    window._flush_dirty_logs()
    assert "new error" in window.log_view.toPlainText()
    window.log_filter.setCurrentText("stdout")
    assert window.log_view.toPlainText() == "[stdout] second normal"
    window.log_filter.setCurrentText("std")
    assert "new error" in window.log_view.toPlainText()
    assert len(manager.logs[first.id]) == 3


def test_logs_are_bounded_to_5000_lines_and_selected_view_refreshes(
    qapp, close_widgets
):
    script = make_config()
    manager = FakeManager([script])
    manager.logs[script.id] = [("stdout", f"line-{index}") for index in range(5001)]
    config_manager = FakeConfigManager()
    window = MainWindow(manager, config_manager, [script])
    close_widgets.append(window)
    qapp.processEvents()

    assert len(window._logs[script.id]) == 5000
    assert window.log_view.document().blockCount() == 5000
    assert window.log_view.toPlainText().splitlines()[0] == "[stdout] line-1"
    assert window.log_view.toPlainText().splitlines()[-1] == "[stdout] line-5000"

    # Incoming signal + timer flush should reload the bounded authoritative
    # manager log and keep the selected script's view in sync.
    manager.logs[script.id].append(("stderr", "最新中文输出"))
    manager.log_received.emit(script.id, "stderr", "最新中文输出")
    window._flush_dirty_logs()
    qapp.processEvents()
    assert len(window._logs[script.id]) == 5000
    assert window.log_view.toPlainText().splitlines()[-1] == "[stderr] 最新中文输出"


def test_close_event_only_hides_window_and_keeps_state(
    qapp, close_widgets
):
    script = make_config()
    window, _manager, _config_manager = build_window(qapp, [script], close_widgets=close_widgets)
    window.show()
    qapp.processEvents()
    assert window.isVisible()

    window.close()
    qapp.processEvents()
    assert window.isHidden()
    assert not window.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert window.script_table.rowCount() == 1
    assert window.scripts == [script]
