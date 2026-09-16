"""Main ScriptDock window.

The window owns presentation state only.  Starting, stopping and reading
processes is delegated to ``ProcessManager``; configuration writes go through
``ConfigManager`` before the in-memory list or manager is changed.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QAbstractItemView,
    QFrame,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from models.script_config import ScriptConfig
from ui.script_dialog import ScriptDialog


_STATUS_COLORS = {
    "Running": "#18864b",
    "Starting": "#9a6700",
    "Stopped": "#64748b",
    "Failed": "#c83b46",
}
_TYPE_LABELS = {
    "python": "Python",
    "bat": "BAT/CMD",
    "cmd": "BAT/CMD",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "exe": "Executable",
    "executable": "Executable",
}


class MainWindow(QMainWindow):
    """Script list and live output viewer for the MVP application."""

    quit_requested = Signal()

    MAX_LOG_LINES = 5000
    WEB_REFRESH_INTERVAL_MS = 1000
    ACTIVE_STATUSES = frozenset({"Running", "Starting", "Stopping"})

    def __init__(self, manager, config_manager, scripts: Iterable[ScriptConfig] | None = None):
        super().__init__()
        self.manager = manager
        self.config_manager = config_manager
        self.scripts = list(scripts or [])
        # These aliases keep the small public surface convenient for callers.
        self.script_configs = self.scripts
        self._logs: dict[str, deque[tuple[str, str]]] = {}
        self._dirty_log_ids: set[str] = set()
        self._row_controls: dict[str, dict[str, QPushButton]] = {}
        self._selected_id: str | None = None

        self.setWindowTitle("ScriptDock · 脚本托盘管理器")
        self.setMinimumSize(900, 620)
        self.resize(1080, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._build_ui()

        status_signal = getattr(self.manager, "status_changed", None)
        if status_signal is not None:
            status_signal.connect(self._on_status_changed)
        log_signal = getattr(self.manager, "log_received", None)
        if log_signal is not None:
            log_signal.connect(self._on_log_received)

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(100)
        self._log_timer.timeout.connect(self._flush_dirty_logs)
        self._log_timer.start()
        self._web_timer = QTimer(self)
        self._web_timer.setInterval(self.WEB_REFRESH_INTERVAL_MS)
        self._web_timer.timeout.connect(self._refresh_web_buttons)
        self._web_timer.start()
        self._rebuild_table()

    # ---- construction -------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("mainRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        title = QLabel("脚本管理")
        title.setObjectName("pageTitle")
        title_font = QFont(title.font())
        title_font.setPointSize(title_font.pointSize() + 5)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QLabel("在后台安静地运行脚本，随时查看状态与输出")
        subtitle.setObjectName("pageSubtitle")
        title_row.addWidget(title)
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.add_button = QPushButton("添加脚本")
        self.add_button.setObjectName("primaryButton")
        self.edit_button = QPushButton("编辑")
        self.delete_button = QPushButton("删除")
        self.edit_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_script)
        self.edit_button.clicked.connect(self.edit_script)
        self.delete_button.clicked.connect(self.delete_script)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.script_table = QTableWidget(0, 5, self)
        self.script_table.setObjectName("scriptTable")
        self.script_table.setHorizontalHeaderLabels(["名称", "类型", "状态", "自启动", "操作"])
        self.script_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.script_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.script_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.script_table.setAlternatingRowColors(True)
        self.script_table.setShowGrid(False)
        self.script_table.verticalHeader().setVisible(False)
        header = self.script_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.script_table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.script_table, 3)
        # Compatibility aliases for simple integrations/tests.
        self.table = self.script_table
        self.tableWidget = self.script_table

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        log_header = QHBoxLayout()
        self.log_title = QLabel("输出日志")
        self.log_title.setObjectName("logTitle")
        self.log_hint = QLabel("选择脚本查看 stdout / stderr")
        self.log_hint.setObjectName("logHint")
        log_header.addWidget(self.log_title)
        log_header.addWidget(self.log_hint)
        log_header.addStretch(1)
        self.log_filter = QComboBox(self)
        self.log_filter.setObjectName("logFilter")
        self.log_filter.addItems(["std", "stdout", "stderr"])
        self.log_filter.setToolTip("std 显示全部日志；stdout / stderr 仅显示对应输出")
        self.log_filter.currentIndexChanged.connect(self._on_log_filter_changed)
        log_header.addWidget(self.log_filter)
        layout.addLayout(log_header)

        self.log_view = QPlainTextEdit(self)
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setPlaceholderText("尚无输出")
        self.log_view.setMaximumBlockCount(self.MAX_LOG_LINES)
        log_font = QFont("Consolas")
        log_font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(log_font)
        layout.addWidget(self.log_view, 2)
        self.logs_view = self.log_view
        self.log_text = self.log_view

        self.setStyleSheet(
            """
            #mainRoot { background: #f7f8fa; }
            #pageTitle { color: #1f2937; }
            #pageSubtitle, #logHint, #formHint { color: #64748b; }
            QPushButton { min-height: 30px; padding: 3px 13px; }
            QPushButton#primaryButton { color: white; background: #2563eb; border: 0; border-radius: 5px; }
            QPushButton#primaryButton:hover { background: #1d4ed8; }
            QTableWidget, QPlainTextEdit { background: white; border: 1px solid #d8dee8; border-radius: 6px; }
            QTableWidget { selection-background-color: #dbeafe; selection-color: #172033; }
            QHeaderView::section { background: #eef1f5; color: #475569; border: 0; padding: 7px; font-weight: 600; }
            #logTitle { color: #334155; font-weight: 600; }
            QToolButton { border: 1px solid #d1d9e5; border-radius: 4px; padding: 3px 7px; }
            QToolButton:hover { background: #eef4ff; }
            """
        )

    # ---- rows and selection ------------------------------------------

    @staticmethod
    def _script_id(config: ScriptConfig) -> str:
        return str(getattr(config, "id", "") or "")

    def _find_row(self, script_id: str) -> int:
        for row in range(self.script_table.rowCount()):
            item = self.script_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == script_id:
                return row
        return -1

    def _rebuild_table(self, preserve_id: str | None = None) -> None:
        old_id = preserve_id if preserve_id is not None else self._selected_id
        self._row_controls.clear()
        self.script_table.setUpdatesEnabled(False)
        try:
            self.script_table.clearContents()
            self.script_table.setRowCount(len(self.scripts))
            for row, config in enumerate(self.scripts):
                script_id = self._script_id(config)
                name_item = QTableWidgetItem(str(getattr(config, "name", "") or script_id))
                name_item.setData(Qt.ItemDataRole.UserRole, script_id)
                name_item.setToolTip(str(getattr(config, "path", "") or ""))
                self.script_table.setItem(row, 0, name_item)

                type_name = str(getattr(config, "type", "") or "").lower()
                type_item = QTableWidgetItem(_TYPE_LABELS.get(type_name, type_name.upper()))
                self.script_table.setItem(row, 1, type_item)

                status = self._status(script_id)
                status_item = QTableWidgetItem()
                self._style_status_item(status_item, status)
                self.script_table.setItem(row, 2, status_item)

                auto_start = bool(getattr(config, "auto_start", False))
                auto_start_item = QTableWidgetItem("是" if auto_start else "否")
                auto_start_item.setData(Qt.ItemDataRole.UserRole, auto_start)
                self.script_table.setItem(row, 3, auto_start_item)

                controls = QWidget(self.script_table)
                controls_layout = QHBoxLayout(controls)
                controls_layout.setContentsMargins(3, 1, 3, 1)
                controls_layout.setSpacing(4)
                buttons: dict[str, QPushButton] = {}
                for action, label in (
                    ("start", "启动"),
                    ("stop", "停止"),
                    ("restart", "重启"),
                    ("web", "打开网页"),
                ):
                    button = QPushButton(label, controls)
                    button.setObjectName(f"{action}Button")
                    button.setToolTip("打开网页" if action == "web" else action.title())
                    if action == "web":
                        button.clicked.connect(
                            lambda _checked=False, sid=script_id: self._open_web(sid)
                        )
                        button.setEnabled(self._web_url_for_script(script_id) is not None)
                    else:
                        button.clicked.connect(
                            lambda _checked=False, sid=script_id, a=action: self._run_action(sid, a)
                        )
                    controls_layout.addWidget(button)
                    buttons[action] = button
                self._row_controls[script_id] = buttons
                self.script_table.setCellWidget(row, 4, controls)

                self.script_table.setRowHeight(row, 42)
                self._update_row_actions(script_id)
        finally:
            self.script_table.setUpdatesEnabled(True)

        target = self._find_row(old_id) if old_id else -1
        if target < 0 and self.scripts:
            target = 0
        if target >= 0:
            self.script_table.selectRow(target)
            self._select_script(self._script_id(self.scripts[target]))
        else:
            self._selected_id = None
            self._update_log_view([])
        self._update_selection_buttons()

    def _select_script(self, script_id: str) -> None:
        row = self._find_row(script_id)
        if row >= 0:
            self.script_table.selectRow(row)
        self._selected_id = script_id
        config = next((item for item in self.scripts if self._script_id(item) == script_id), None)
        self.log_title.setText(f"输出日志 · {getattr(config, 'name', script_id)}" if config else "输出日志")
        self._load_logs(script_id)
        self._update_selection_buttons()

    def _on_selection_changed(self) -> None:
        row = self.script_table.currentRow()
        if row < 0 or row >= len(self.scripts):
            self._selected_id = None
            self.log_title.setText("输出日志")
            self._update_log_view([])
            self._update_selection_buttons()
            return
        item = self.script_table.item(row, 0)
        if item is not None:
            self._select_script(str(item.data(Qt.ItemDataRole.UserRole)))

    def _update_selection_buttons(self) -> None:
        selected = self._selected_config()
        active = self._selected_id is not None and self._is_active(self._selected_id)
        self.edit_button.setEnabled(selected is not None and not active)
        self.delete_button.setEnabled(selected is not None and not active)

    def _selected_config(self):
        if not self._selected_id:
            return None
        return next((item for item in self.scripts if self._script_id(item) == self._selected_id), None)

    # ---- process state and logs --------------------------------------

    def _status(self, script_id: str) -> str:
        try:
            value = self.manager.get_status(script_id)
        except Exception:
            value = "Stopped"
        return str(value or "Stopped")

    def _is_active(self, script_id: str) -> bool:
        status = self._status(script_id)
        if status in self.ACTIVE_STATUSES:
            return True
        try:
            return bool(self.manager.is_running(script_id))
        except (AttributeError, RuntimeError):
            return False

    @staticmethod
    def _style_status_item(item: QTableWidgetItem, status: str) -> None:
        status = str(status)
        item.setText(status)
        item.setData(Qt.ItemDataRole.UserRole, status)
        item.setForeground(QColor(_STATUS_COLORS.get(status, "#475569")))
        item.setToolTip(status)

    def _on_status_changed(self, script_id: str, status: str) -> None:
        script_id = str(script_id)
        row = self._find_row(script_id)
        if row >= 0:
            item = self.script_table.item(row, 2)
            if item is None:
                item = QTableWidgetItem()
                self.script_table.setItem(row, 2, item)
            self._style_status_item(item, str(status))
        self._update_row_actions(script_id)
        self._update_web_button(script_id)
        if script_id == self._selected_id:
            self._update_selection_buttons()

    def _update_row_actions(self, script_id: str) -> None:
        buttons = self._row_controls.get(script_id)
        if not buttons:
            return
        status = self._status(script_id)
        busy = status in {"Starting", "Stopping"}
        running = status == "Running"
        try:
            running = running or bool(self.manager.is_running(script_id))
        except (AttributeError, RuntimeError):
            pass
        buttons["start"].setEnabled(not running and not busy)
        buttons["stop"].setEnabled(running and not busy)
        buttons["restart"].setEnabled(not busy)

    @staticmethod
    def _split_log_text(value: object) -> list[str]:
        text = str(value if value is not None else "")
        # splitlines handles CRLF, CR and LF from Windows and preserves an
        # explicit empty output as one line.
        pieces = text.splitlines()
        return pieces or [""]

    def _normalise_logs(self, entries) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for entry in entries or []:
            try:
                stream, line = entry
            except (TypeError, ValueError):
                stream, line = "system", entry
            for part in self._split_log_text(line):
                result.append((str(stream), part))
        return result[-self.MAX_LOG_LINES :]

    def _load_logs(self, script_id: str, preserve_scroll: bool = False) -> None:
        try:
            entries = self.manager.get_logs(script_id)
        except Exception:
            entries = []
        self._logs[script_id] = deque(self._normalise_logs(entries), maxlen=self.MAX_LOG_LINES)
        self._dirty_log_ids.discard(script_id)
        if script_id == self._selected_id:
            self._update_log_view(self._logs[script_id], preserve_scroll=preserve_scroll)

    def _on_log_received(self, script_id: str, _stream: str, _line: str) -> None:
        # ProcessManager exposes the authoritative bounded log deque.  Batch
        # signals are coalesced here so a selection replay cannot duplicate
        # lines already delivered by the manager.
        self._dirty_log_ids.add(str(script_id))

    def _flush_dirty_logs(self) -> None:
        dirty = tuple(self._dirty_log_ids)
        self._dirty_log_ids.clear()
        for script_id in dirty:
            self._load_logs(script_id, preserve_scroll=True)

    @staticmethod
    def _format_log(stream: str, line: str) -> str:
        label = {"stdout": "stdout", "stderr": "stderr", "system": "system"}.get(stream, stream)
        return f"[{label}] {line}"

    def _on_log_filter_changed(self, _index):
        if self._selected_id:
            self._load_logs(self._selected_id)
        else:
            self._update_log_view([])

    def _update_log_view(self, entries, preserve_scroll: bool = False) -> None:
        selected_stream = self.log_filter.currentText()
        if selected_stream != "std":
            entries = [(stream, line) for stream, line in entries if stream == selected_stream]
        scrollbar = self.log_view.verticalScrollBar()
        old_value = scrollbar.value()
        was_at_bottom = old_value >= scrollbar.maximum() - 2
        self.log_view.setPlainText("\n".join(self._format_log(stream, line) for stream, line in entries))
        if not preserve_scroll or was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(min(old_value, scrollbar.maximum()))

    # ---- process actions ---------------------------------------------

    def _run_action(self, script_id: str, action: str) -> None:
        try:
            getattr(self.manager, action)(script_id)
        except Exception as error:
            self._show_error("操作失败", error)

    def _start_script(self, script_id: str) -> None:
        self._run_action(script_id, "start")

    def _stop_script(self, script_id: str) -> None:
        self._run_action(script_id, "stop")

    def _restart_script(self, script_id: str) -> None:
        self._run_action(script_id, "restart")

    # ---- web links ---------------------------------------------------

    @staticmethod
    def _web_url(value: object) -> QUrl | None:
        """Return a usable HTTP(S) URL, or ``None`` for an invalid value."""

        if isinstance(value, QUrl):
            url = value
            if (
                not url.isValid()
                or url.scheme().lower() not in {"http", "https"}
                or not url.host()
                or url.authority().endswith(":")
            ):
                return None
            return url
        text = str(value or "").strip()
        if not text:
            return None
        if any(character.isspace() for character in text):
            return None
        url = QUrl(text, QUrl.ParsingMode.StrictMode)
        if (
            not url.isValid()
            or url.scheme().lower() not in {"http", "https"}
            or not url.host()
            or url.authority().endswith(":")
        ):
            return None
        return url

    def _is_running(self, script_id: str) -> bool:
        if self._status(script_id) != "Running":
            return False
        try:
            return bool(self.manager.is_running(script_id))
        except Exception:
            return True

    def _web_url_for_script(self, script_id: str) -> QUrl | None:
        """Return the manager-detected URL for a currently running script."""

        if not self._is_running(script_id):
            return None
        try:
            value = self.manager.get_web_url(script_id)
        except Exception:
            return None
        return self._web_url(value)

    def _update_web_button(self, script_id: str) -> None:
        buttons = self._row_controls.get(str(script_id))
        if buttons and "web" in buttons:
            buttons["web"].setEnabled(self._web_url_for_script(str(script_id)) is not None)

    def _refresh_web_buttons(self) -> None:
        for script_id in tuple(self._row_controls):
            self._update_web_button(script_id)

    def _open_web(self, script_id: str) -> bool:
        # Resolve the URL again at click time: the detected port can change
        # after the button was enabled by the periodic refresh.
        url = self._web_url_for_script(str(script_id))
        if url is None:
            QMessageBox.warning(self, "打开网页失败", "脚本未运行或尚未检测到监听端口。")
            return False
        try:
            opened = QDesktopServices.openUrl(url)
        except Exception as error:
            QMessageBox.warning(self, "打开网页失败", str(error))
            return False
        if opened is False:
            QMessageBox.warning(self, "打开网页失败", "系统无法打开该网页。")
            return False
        return True

    # Explicit alias for integrations that use a descriptive slot name.
    _open_webpage = _open_web

    # ---- configuration transactions ---------------------------------

    def _validate_config(self, config: ScriptConfig) -> None:
        validator = getattr(config, "validate", None)
        if callable(validator):
            result = validator()
            if result is False:
                raise ValueError("脚本配置无效。")

    def _save(self, scripts: list[ScriptConfig]) -> None:
        result = self.config_manager.save(list(scripts))
        if result is False:
            raise OSError("保存配置失败。")

    def _show_error(self, title: str, error: object) -> None:
        QMessageBox.critical(self, title, str(error))

    def _commit(self, candidate: list[ScriptConfig], manager_operation, old_scripts: list[ScriptConfig]) -> bool:
        try:
            self._save(candidate)
        except Exception as error:
            self._show_error("保存失败", error)
            return False
        try:
            manager_operation()
        except Exception as error:
            # Keep persistence and the running manager transactional from the
            # user's point of view if registration/unregistration rejects it.
            try:
                self._save(old_scripts)
            except Exception as rollback_error:
                error = f"{error}\n回滚配置也失败：{rollback_error}"
            self._show_error("操作失败", error)
            return False
        self.scripts[:] = candidate
        self._rebuild_table(preserve_id=self._selected_id)
        return True

    def add_script(self) -> None:
        dialog = ScriptDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            config = dialog.get_config()
            self._validate_config(config)
            if any(self._script_id(item) == self._script_id(config) for item in self.scripts):
                raise ValueError("脚本 ID 已存在，请重新添加。")
        except Exception as error:
            self._show_error("配置无效", error)
            return
        old_scripts = list(self.scripts)
        candidate = old_scripts + [config]
        self._commit(candidate, lambda: self.manager.register(config), old_scripts)

    def edit_script(self) -> None:
        old_config = self._selected_config()
        if old_config is None or self._is_active(self._script_id(old_config)):
            if old_config is not None:
                QMessageBox.information(self, "脚本运行中", "请先停止脚本，再编辑其配置。")
            return
        dialog = ScriptDialog(self, old_config)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            new_config = dialog.get_config()
            self._validate_config(new_config)
            old_id = self._script_id(old_config)
            if any(self._script_id(item) == self._script_id(new_config) and self._script_id(item) != old_id for item in self.scripts):
                raise ValueError("脚本 ID 已存在，请使用其他配置。")
        except Exception as error:
            self._show_error("配置无效", error)
            return
        old_scripts = list(self.scripts)
        candidate = [new_config if item is old_config or self._script_id(item) == self._script_id(old_config) else item for item in old_scripts]

        def replace_manager_config() -> None:
            old_id = self._script_id(old_config)
            if self._script_id(new_config) == old_id:
                self.manager.register(new_config)
                return
            self.manager.unregister(old_id)
            try:
                self.manager.register(new_config)
            except Exception:
                self.manager.register(old_config)
                raise

        self._commit(candidate, replace_manager_config, old_scripts)

    def delete_script(self) -> None:
        config = self._selected_config()
        if config is None:
            return
        script_id = self._script_id(config)
        if self._is_active(script_id):
            QMessageBox.information(self, "脚本运行中", "请先停止脚本，再删除它。")
            return
        if QMessageBox.question(
            self,
            "删除脚本",
            f"确定删除“{getattr(config, 'name', script_id)}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        old_scripts = list(self.scripts)
        candidate = [item for item in old_scripts if self._script_id(item) != script_id]
        if self._commit(candidate, lambda: self.manager.unregister(script_id), old_scripts):
            self._logs.pop(script_id, None)
            self._dirty_log_ids.discard(script_id)

    # Commonly convenient slot aliases.
    on_add_script = add_script
    on_edit_script = edit_script
    on_delete_script = delete_script

    def request_quit(self) -> None:
        self.quit_requested.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        event.ignore()
        self.hide()


__all__ = ["MainWindow"]
