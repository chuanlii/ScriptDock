"""Atomic JSON persistence for ScriptDock script configurations."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from models.script_config import ScriptConfig


def _default_config_path() -> Path:
    """Return the Windows per-user configuration path.

    ``APPDATA`` is used when available (the normal Windows environment).  The
    fallback keeps development and tests deterministic on non-Windows hosts.
    """

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ScriptDock" / "config.json"
    return Path.home() / "AppData" / "Roaming" / "ScriptDock" / "config.json"


class ConfigManager:
    """Load and save the list of :class:`ScriptConfig` records."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else _default_config_path()
        self.last_error: str | None = None

    @property
    def backup_path(self) -> Path:
        """Path used for the previous successfully written JSON document."""

        return Path(f"{self.path}.bak")

    @staticmethod
    def _read(path: Path) -> list[ScriptConfig]:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict):
            raise ValueError("配置根节点必须是对象。")
        records = payload.get("scripts")
        if not isinstance(records, list):
            raise ValueError("配置必须包含 scripts 数组。")
        scripts = [ScriptConfig.from_dict(record) for record in records]
        if len({script.id for script in scripts}) != len(scripts):
            raise ValueError("脚本 id 不能重复。")
        return scripts

    def load(self) -> list[ScriptConfig]:
        """Load configurations, falling back to the last atomic-save backup.

        Missing configuration is a normal first-run state.  A malformed or
        unreadable file returns an empty list unless a valid ``.bak`` exists;
        in either error case ``last_error`` is populated for the UI warning.
        """

        self.last_error = None
        try:
            return self._read(self.path)
        except FileNotFoundError:
            # A backup can still provide a useful recovery if the primary was
            # interrupted or removed after a previous successful save.
            try:
                recovered = self._read(self.backup_path)
            except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                return []
            self.last_error = "配置文件不存在，已从备份恢复。"
            return recovered
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            try:
                recovered = self._read(self.backup_path)
            except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                self.last_error = f"配置文件损坏或无法读取：{error}"
                return []
            self.last_error = f"配置文件损坏，已从备份恢复：{error}"
            return recovered

    def save(self, scripts: Iterable[ScriptConfig]) -> None:
        """Persist scripts using a same-directory temporary file and replace.

        Every record is validated before the existing file is touched.  The
        old file is copied to ``config.json.bak`` as a recovery point, then a
        flushed and fsynced temporary file is atomically replaced into place.
        """

        if isinstance(scripts, (str, bytes, dict)):
            raise TypeError("scripts 必须是 ScriptConfig 序列。")
        try:
            records = list(scripts)
        except TypeError as error:
            raise TypeError("scripts 必须是 ScriptConfig 序列。") from error

        serialized: list[dict] = []
        ids = set()
        for script in records:
            if not isinstance(script, ScriptConfig):
                raise TypeError("scripts 中的每一项都必须是 ScriptConfig。")
            script.validate()
            if script.id in ids:
                raise ValueError("脚本 id 不能重复。")
            ids.add(script.id)
            serialized.append(script.to_dict())

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        fd: int | None = None
        try:
            if self.path.exists():
                # Backup is deliberately best-effort: it improves recovery,
                # but failure to copy must not turn an atomic save into a
                # failed save when the target itself is writable.
                try:
                    # Do not replace a healthy recovery point with corrupted JSON.
                    self._read(self.path)
                    shutil.copy2(self.path, self.backup_path)
                except (OSError, ValueError, TypeError):
                    pass

            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(parent)
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                fd = None
                json.dump(
                    {"scripts": serialized},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            self.last_error = None
        except Exception as error:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            self.last_error = f"配置保存失败：{error}"
            raise


__all__ = ["ConfigManager"]
