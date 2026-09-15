import json

import pytest

from core.config_manager import ConfigManager
from models.script_config import ScriptConfig


def config(name='test'):
    return ScriptConfig('same', name, 'python', r'C:\scripts\test.py')


def test_duplicate_ids_rejected_before_save(tmp_path):
    manager = ConfigManager(tmp_path / 'config.json')
    with pytest.raises(ValueError, match='id'):
        manager.save([config(), config('other')])
    assert not manager.path.exists()


def test_duplicate_ids_in_file_reported(tmp_path):
    manager = ConfigManager(tmp_path / 'config.json')
    manager.path.write_text(json.dumps({'scripts': [config().to_dict()] * 2}), encoding='utf-8')
    assert manager.load() == []
    assert manager.last_error


def test_corruption_does_not_overwrite_valid_backup(tmp_path):
    manager = ConfigManager(tmp_path / 'config.json')
    manager.save([config('old')])
    manager.save([config('new')])
    manager.path.write_text('broken', encoding='utf-8')
    assert manager.load()[0].name == 'old'
    manager.save([config('replacement')])
    assert json.loads(manager.backup_path.read_text(encoding='utf-8'))['scripts'][0]['name'] == 'old'


def test_nul_argument_rejected():
    script = config()
    script.args = ['bad\x00arg']
    with pytest.raises(ValueError):
        script.validate()
