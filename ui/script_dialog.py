"""Add/edit dialog for a ScriptDock script.

The dialog deliberately keeps the data model small.  It presents the command
line arguments as one Windows command-line string and turns that string into
the model's ``list[str]`` representation when the user accepts the dialog.
"""

from __future__ import annotations

import ctypes
import os
import shlex
import shutil
import uuid
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from models.script_config import ScriptConfig, infer_type


_TYPE_LABELS = {
    "python": "Python (.py)",
    "bat": "Windows Batch (.bat / .cmd)",
    "cmd": "Windows Batch (.bat / .cmd)",
    "javascript": "JavaScript (.js)",
    "js": "JavaScript (.js)",
    "exe": "Executable (.exe)",
    "executable": "Executable (.exe)",
}
_TYPE_ALIASES = {
    "cmd": "bat",
    "batch": "bat",
    "javascript": "javascript",
    "js": "javascript",
    "executable": "exe",
}
_SUPPORTED_SUFFIXES = {".py", ".bat", ".cmd", ".js", ".exe"}


def _normalise_type(value: object) -> str:
    """Return the type spelling used by the model where possible."""

    text = str(value or "").strip().lower()
    return _TYPE_ALIASES.get(text, text)


def _type_for_path(path: str) -> str:
    """Infer a model type while remaining useful with older model versions."""

    try:
        inferred = infer_type(path)
    except (TypeError, ValueError):
        inferred = None
    if inferred:
        return str(inferred)
    suffix = Path(path).suffix.lower()
    fallback = {
        ".py": "python",
        ".bat": "bat",
        ".cmd": "bat",
        ".js": "javascript",
        ".exe": "exe",
    }
    if suffix in fallback:
        return fallback[suffix]
    raise ValueError("脚本路径必须使用 .py、.bat、.cmd、.js 或 .exe 扩展名。")


def _command_line_to_argv_windows(value: str) -> list[str]:
    """Parse a Windows command-line string with CommandLineToArgvW.

    ``shlex`` is used as a deterministic fallback on non-Windows hosts so the
    dialog remains testable and importable there.  The native parser is what is
    used by the Windows application and handles backslashes/quotes correctly.
    """

    text = str(value or "").strip()
    if not text:
        return []
    if os.name == "nt":
        try:
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            argc = ctypes.c_int()
            shell32.CommandLineToArgvW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
            # CommandLineToArgvW applies special quoting rules to argv[0].
            # Prefix a harmless executable and discard it so the text in the
            # form is parsed as ordinary arguments (and list2cmdline round
            # trips correctly for paths beginning with a backslash/quote).
            command_line = "ScriptDock.exe " + text
            argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
            if not argv:
                raise ValueError("无法解析启动参数。")
            try:
                return [argv[index] for index in range(1, argc.value)]
            finally:
                # LocalFree accepts the pointer returned by CommandLineToArgvW.
                kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                kernel32.LocalFree.restype = ctypes.c_void_p
                kernel32.LocalFree(argv)
        except (AttributeError, OSError, TypeError, ValueError):
            # A fallback is preferable to making the dialog unusable on a
            # restricted Windows runtime where the API cannot be loaded.
            pass
    try:
        return shlex.split(text, posix=False)
    except ValueError as error:
        raise ValueError(f"参数引号不完整：{error}") from error


# Public aliases make the parser easy to exercise without opening a dialog.
command_line_to_argv = _command_line_to_argv_windows
parse_windows_command_line = _command_line_to_argv_windows


def _command_line_from_args(args: Iterable[object]) -> str:
    """Quote an argument list using the Windows command-line convention."""

    # list2cmdline is part of the standard library and follows the quoting
    # rules expected by CommandLineToArgvW for the common script arguments.
    import subprocess

    return subprocess.list2cmdline([str(argument) for argument in args])


class ScriptDialog(QDialog):
    """Native add/edit form for :class:`models.script_config.ScriptConfig`."""

    def __init__(self, parent=None, config: ScriptConfig | None = None):
        super().__init__(parent)
        self.config = config
        self.setModal(True)
        self.setWindowTitle("编辑脚本" if config is not None else "添加脚本")
        self.setMinimumWidth(560)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("例如：AI 数据看板")
        self.name_edit.setClearButtonEnabled(True)

        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText("选择 .py / .bat / .cmd / .js / .exe 文件")
        self.path_edit.setClearButtonEnabled(True)
        self.path_browse_button = QPushButton("浏览…", self)
        self.path_browse_button.setAutoDefault(False)

        self.type_combo = QComboBox(self)
        self.type_combo.setObjectName("scriptTypeCombo")
        self.type_combo.setToolTip("脚本类型根据扩展名自动判断")
        self.type_combo.setEnabled(False)
        # Keep the model-facing values in the combo so tests and accessibility
        # tooling can inspect them without parsing labels.
        for type_name, label in (
            ("python", _TYPE_LABELS["python"]),
            ("bat", _TYPE_LABELS["bat"]),
            ("javascript", _TYPE_LABELS["javascript"]),
            ("exe", _TYPE_LABELS["exe"]),
        ):
            self.type_combo.addItem(label, type_name)

        self.args_edit = QLineEdit(self)
        self.args_edit.setPlaceholderText('例如：--port 8080 --label "我的服务"')
        self.args_edit.setClearButtonEnabled(True)

        self.working_directory_edit = QLineEdit(self)
        self.working_directory_edit.setPlaceholderText("留空则使用脚本所在目录")
        self.working_directory_edit.setClearButtonEnabled(True)
        self.working_directory_browse_button = QPushButton("浏览…", self)
        self.working_directory_browse_button.setAutoDefault(False)

        self.interpreter_edit = QLineEdit(self)
        self.interpreter_edit.setPlaceholderText("Python / Node 可留空自动从 PATH 查找")
        self.interpreter_edit.setClearButtonEnabled(True)
        self.interpreter_browse_button = QPushButton("浏览…", self)
        self.interpreter_browse_button.setAutoDefault(False)

        self.auto_start_checkbox = QCheckBox("随 ScriptDock 自动启动", self)
        self.auto_start_checkbox.setObjectName("autoStartCheckBox")
        self.auto_start_checkbox.setChecked(False)

        # Friendly aliases used by a few integrations and by older previews.
        self.script_path_edit = self.path_edit
        self.arguments_edit = self.args_edit
        self.workdir_edit = self.working_directory_edit
        self.interpreter_path_edit = self.interpreter_edit
        self.name_input = self.name_edit
        self.path_input = self.path_edit
        self.args_input = self.args_edit
        self.working_directory_input = self.working_directory_edit
        self.interpreter_input = self.interpreter_edit
        self.auto_start_input = self.auto_start_checkbox

        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.path_browse_button)

        workdir_row = QHBoxLayout()
        workdir_row.setContentsMargins(0, 0, 0, 0)
        workdir_row.addWidget(self.working_directory_edit, 1)
        workdir_row.addWidget(self.working_directory_browse_button)

        interpreter_row = QHBoxLayout()
        interpreter_row.setContentsMargins(0, 0, 0, 0)
        interpreter_row.addWidget(self.interpreter_edit, 1)
        interpreter_row.addWidget(self.interpreter_browse_button)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setVerticalSpacing(12)
        form.addRow("名称 *", self.name_edit)
        form.addRow("脚本路径 *", path_row)
        form.addRow("类型", self.type_combo)
        form.addRow("启动参数", self.args_edit)
        form.addRow("工作目录", workdir_row)
        form.addRow("解释器", interpreter_row)
        form.addRow("启动选项", self.auto_start_checkbox)

        hint = QLabel("名称必填；路径扩展名决定脚本类型。参数按 Windows 命令行规则解析。", self)
        hint.setObjectName("formHint")
        hint.setWordWrap(True)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(self.button_box)

        self.path_browse_button.clicked.connect(self._browse_path)
        self.working_directory_browse_button.clicked.connect(self._browse_working_directory)
        self.interpreter_browse_button.clicked.connect(self._browse_interpreter)
        self.path_edit.textChanged.connect(self._path_changed)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        self.button_box.accepted.connect(self._accept)
        self.button_box.rejected.connect(self.reject)

        self._initial_path = ""
        self._auto_interpreter_value: str | None = None
        self._auto_working_directory_value: str | None = None
        self._setting_auto_interpreter = False
        self._setting_auto_working_directory = False
        self.interpreter_edit.textChanged.connect(self._interpreter_changed)
        self.working_directory_edit.textChanged.connect(self._working_directory_changed)
        self._populate(config)

    def _populate(self, config: ScriptConfig | None) -> None:
        if config is None:
            return
        self.name_edit.setText(str(getattr(config, "name", "") or ""))
        path = str(getattr(config, "path", "") or "")
        self._initial_path = path
        self.path_edit.setText(path)
        self.args_edit.setText(_command_line_from_args(getattr(config, "args", []) or []))
        self.working_directory_edit.setText(str(getattr(config, "working_directory", "") or ""))
        self.interpreter_edit.setText(str(getattr(config, "interpreter", "") or ""))
        self.auto_start_checkbox.setChecked(bool(getattr(config, "auto_start", False)))
        self._set_type_display(getattr(config, "type", ""), path)

    def _set_type_display(self, value: object, path: str = "") -> None:
        type_name = _normalise_type(value)
        if path:
            try:
                type_name = _normalise_type(_type_for_path(path))
            except ValueError:
                pass
        index = self.type_combo.findData(type_name)
        if index < 0 and type_name == "js":
            index = self.type_combo.findData("javascript")
        if index >= 0:
            self.type_combo.setCurrentIndex(index)

    def _path_changed(self, path: str) -> None:
        if not path:
            return
        try:
            inferred = _type_for_path(path)
        except ValueError:
            self.type_combo.setToolTip("暂不支持此扩展名")
            return
        self._set_type_display(inferred)
        self.type_combo.setToolTip(_TYPE_LABELS.get(str(inferred).lower(), str(inferred)))
        # A blank work directory follows the selected script, while an
        # explicit user choice is never overwritten.
        new_working_directory = str(Path(path).expanduser().resolve().parent)
        current_working_directory = self.working_directory_edit.text().strip()
        if not current_working_directory or current_working_directory == self._auto_working_directory_value:
            self._setting_auto_working_directory = True
            try:
                self.working_directory_edit.setText(new_working_directory)
            finally:
                self._setting_auto_working_directory = False
            self._auto_working_directory_value = new_working_directory
        self._set_interpreter_from_path(inferred)

    def _type_changed(self, _index: int) -> None:
        if not self.interpreter_edit.text().strip():
            self._set_interpreter_from_path(self.type_combo.currentData())

    def _set_interpreter_from_path(self, type_name: object) -> None:
        normalized = _normalise_type(type_name)
        if normalized == "python":
            found = shutil.which("python.exe") or shutil.which("python")
        elif normalized in {"javascript", "js"}:
            found = shutil.which("node.exe") or shutil.which("node")
        else:
            found = None
        current = self.interpreter_edit.text().strip()
        if current and current != self._auto_interpreter_value:
            # The user supplied an explicit interpreter; type/path changes
            # must not silently replace it.
            return
        self._setting_auto_interpreter = True
        try:
            self.interpreter_edit.setText(found or "")
        finally:
            self._setting_auto_interpreter = False
        self._auto_interpreter_value = found

    def _interpreter_changed(self, value: str) -> None:
        if not self._setting_auto_interpreter and value.strip() != (self._auto_interpreter_value or ""):
            self._auto_interpreter_value = None

    def _working_directory_changed(self, value: str) -> None:
        if not self._setting_auto_working_directory and value.strip() != (self._auto_working_directory_value or ""):
            self._auto_working_directory_value = None

    def _browse_path(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self,
            "选择脚本",
            self.path_edit.text().strip() or str(Path.home()),
            "支持的脚本 (*.py *.bat *.cmd *.js *.exe);;Python (*.py);;Batch (*.bat *.cmd);;JavaScript (*.js);;Executable (*.exe);;所有文件 (*.*)",
        )
        if filename:
            self.path_edit.setText(filename)

    def _browse_working_directory(self) -> None:
        start = self.working_directory_edit.text().strip() or self.path_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "选择工作目录", start)
        if directory:
            self.working_directory_edit.setText(directory)

    def _browse_interpreter(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self,
            "选择解释器",
            self.interpreter_edit.text().strip() or str(Path.home()),
            "可执行文件 (*.exe);;所有文件 (*.*)",
        )
        if filename:
            self.interpreter_edit.setText(filename)

    def _validate_fields(self) -> tuple[str, str, str, list[str], str, str, bool]:
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("请输入脚本名称。")
        path = self.path_edit.text().strip()
        if not path:
            raise ValueError("请选择脚本文件。")
        if Path(path).suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise ValueError("脚本路径必须使用 .py、.bat、.cmd、.js 或 .exe 扩展名。")
        type_name = _type_for_path(path)
        args = _command_line_to_argv_windows(self.args_edit.text())
        workdir = self.working_directory_edit.text().strip()
        interpreter = self.interpreter_edit.text().strip()
        auto_start = self.auto_start_checkbox.isChecked()
        return name, path, type_name, args, workdir, interpreter, auto_start

    def get_config(self) -> ScriptConfig:
        """Return a validated model object from the current form values.

        Validation errors are raised for programmatic callers and are shown in
        a small warning dialog when the user presses Save.
        """

        name, path, type_name, args, workdir, interpreter, auto_start = self._validate_fields()
        script_id = str(getattr(self.config, "id", "") or uuid.uuid4().hex)
        # ``web_url`` remains a model field for loading old configurations,
        # but is intentionally not editable.  Preserve it during edits so
        # changing an unrelated setting does not silently discard old data.
        web_url = str(getattr(self.config, "web_url", "") or "") if self.config is not None else ""
        config = ScriptConfig(
            id=script_id,
            name=name,
            type=type_name,
            path=path,
            args=args,
            working_directory=workdir,
            interpreter=interpreter or None,
            auto_start=auto_start,
            web_url=web_url,
        )
        validator = getattr(config, "validate", None)
        if callable(validator):
            result = validator()
            if result is False:
                raise ValueError("脚本配置无效。")
        return config

    def _accept(self) -> None:
        try:
            self.get_config()
        except (TypeError, ValueError, OSError) as error:
            QMessageBox.warning(self, "配置无效", str(error))
            return
        self.accept()


__all__ = [
    "ScriptDialog",
    "command_line_to_argv",
    "parse_windows_command_line",
]
