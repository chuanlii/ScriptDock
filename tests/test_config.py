import json

import pytest

from core.config_manager import ConfigManager
from models import ScriptConfig, infer_type


def make_config(path="scripts/example.py"):
    return ScriptConfig(
        id="example",
        name="Example",
        type="python",
        path=path,
        args=["--port", "8000"],
        working_directory="",
        interpreter=None,
    )


def test_model_round_trip_and_supported_type_inference():
    script = make_config()
    assert infer_type("example.py") == "python"
    assert infer_type("example.BAT") == "bat"
    assert infer_type("example.cmd") == "cmd"
    assert infer_type("example.JS") == "javascript"
    assert infer_type("example.exe") == "exe"
    assert infer_type("example.txt") is None
    assert ScriptConfig.from_dict(script.to_dict()) == script


def test_model_accepts_extension_aliases():
    script = make_config()
    script.type = "py"
    script.validate()
    script.path = "scripts/example.js"
    script.type = "js"
    script.validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"args": ["--ok", 1]},
        {"type": "python", "path": "example.txt"},
        {"type": "bat", "path": "example.py"},
        {"type": "shell", "path": "example.py"},
    ],
)
def test_model_validation_rejects_invalid_data(changes):
    data = make_config().to_dict()
    data.update(changes)
    with pytest.raises(ValueError):
        ScriptConfig.from_dict(data)


def test_config_manager_round_trip_and_parent_creation(tmp_path):
    path = tmp_path / "nested" / "config.json"
    manager = ConfigManager(path)
    scripts = [make_config()]
    manager.save(scripts)
    assert manager.load() == scripts
    assert json.loads(path.read_text(encoding="utf-8")) == {"scripts": [scripts[0].to_dict()]}


def test_config_manager_corrupt_file_sets_error_and_uses_backup(tmp_path):
    path = tmp_path / "config.json"
    manager = ConfigManager(path)
    original = [make_config()]
    manager.save(original)
    manager.save([make_config("scripts/updated.py")])
    path.write_text("{not valid json", encoding="utf-8")

    loaded = manager.load()
    assert loaded == original
    assert manager.last_error


def test_config_manager_missing_file_is_first_run(tmp_path):
    manager = ConfigManager(tmp_path / "config.json")
    assert manager.load() == []
    assert manager.last_error is None
