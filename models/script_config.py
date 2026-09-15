"""The persisted configuration model for one managed script."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# The values in the design document use the language name for Python and
# JavaScript, while the file extensions are often used as the type value by
# callers.  Keep one canonical value per extension and accept both spellings
# when validating data read from an existing configuration file.
_EXTENSION_TO_TYPE = {
    ".py": "python",
    ".bat": "bat",
    ".cmd": "cmd",
    ".js": "javascript",
    ".exe": "exe",
}
_TYPE_ALIASES = {
    "py": "python",
    "python": "python",
    "bat": "bat",
    "cmd": "cmd",
    "js": "javascript",
    "javascript": "javascript",
    "exe": "exe",
}

# Public enough for UI validation and useful to callers without making the
# serialized representation depend on an implementation detail.
SUPPORTED_TYPES = frozenset(_TYPE_ALIASES)


def infer_type(path: str) -> str | None:
    """Return the ScriptDock type for a supported script path.

    The result is canonical (``python``/``javascript`` for the two
    interpreter-backed formats) and is case-insensitive with respect to the
    path extension.  Unsupported paths return ``None`` so callers can choose
    the appropriate user-facing error.
    """

    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    return _EXTENSION_TO_TYPE.get(Path(path).suffix.lower())


def _require_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串。")
    if "\x00" in value:
        raise ValueError(f"{field_name} 不能包含 NUL 字符。")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    return value


@dataclass
class ScriptConfig:
    """Configuration for a single locally managed script.

    ``working_directory`` and ``interpreter`` retain the exact values used in
    the JSON file.  An empty working directory means "the script directory"
    to the process layer, and a ``None`` interpreter means auto-discovery.
    """

    id: str
    name: str
    type: str
    path: str
    args: list[str] = field(default_factory=list)
    working_directory: str = ""
    interpreter: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScriptConfig":
        """Build and validate a model from its persisted JSON mapping."""

        if not isinstance(data, Mapping):
            raise ValueError("脚本配置必须是对象。")

        missing = [key for key in ("id", "name", "type", "path") if key not in data]
        if missing:
            raise ValueError(f"脚本配置缺少字段：{', '.join(missing)}。")

        raw_args = data.get("args", [])
        if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
            raise ValueError("args 必须是字符串数组。")

        working_directory = data.get("working_directory", "")
        interpreter = data.get("interpreter")
        config = cls(
            id=data["id"],
            name=data["name"],
            type=data["type"],
            path=data["path"],
            args=list(raw_args),
            working_directory=working_directory,
            interpreter=interpreter,
        )
        config.validate()
        return config

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by ConfigManager."""

        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "args": list(self.args),
            "working_directory": self.working_directory,
            "interpreter": self.interpreter,
        }

    def validate(self) -> None:
        """Validate field types and ensure the type matches the file suffix.

        Validation deliberately does not require the script or working
        directory to exist.  A saved configuration can be edited while a
        removable drive is disconnected; the process layer reports a missing
        path when the user later starts that script.
        """

        _require_text(self.id, "id")
        _require_text(self.name, "name")
        raw_type = _require_text(self.type, "type").strip().lower()
        _require_text(self.path, "path")
        _require_text(self.working_directory, "working_directory", allow_empty=True)

        if not isinstance(self.args, list) or any(not isinstance(item, str) for item in self.args):
            raise ValueError("args 必须是字符串数组。")
        if any("\x00" in item for item in self.args):
            raise ValueError("参数不能包含 NUL 字符。")
        if self.interpreter is not None:
            _require_text(self.interpreter, "interpreter")

        canonical_type = _TYPE_ALIASES.get(raw_type)
        if canonical_type is None:
            supported = ", ".join(sorted(SUPPORTED_TYPES))
            raise ValueError(f"不支持的脚本类型：{self.type}（支持：{supported}）。")

        inferred = infer_type(self.path)
        if inferred is None:
            raise ValueError("脚本路径必须以 .py、.bat、.cmd、.js 或 .exe 结尾。")
        if canonical_type != inferred:
            raise ValueError(f"脚本类型 {self.type} 与路径扩展名不匹配。")


__all__ = ["ScriptConfig", "SUPPORTED_TYPES", "infer_type"]
