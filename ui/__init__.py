"""ScriptDock's small, native PySide6 user interface.

The imports are lazy to keep ``ui.script_dialog`` and ``ui.main_window``
independently importable (which is useful to lightweight tooling and tests).
"""

__all__ = ["MainWindow", "ScriptDialog"]


def __getattr__(name):
    if name == "MainWindow":
        from ui.main_window import MainWindow

        return MainWindow
    if name == "ScriptDialog":
        from ui.script_dialog import ScriptDialog

        return ScriptDialog
    raise AttributeError(name)
