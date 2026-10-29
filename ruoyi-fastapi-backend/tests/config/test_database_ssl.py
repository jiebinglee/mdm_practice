import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.database import _driver_adapter
from config.env import AppSettings, DataSourceSettings, GetConfig


def _source(**overrides: object) -> DataSourceSettings:
    values = {
        'db_type': 'postgresql',
        'db_host': 'db.example.test',
        'db_port': 5432,
        'db_username': 'app',
        'db_password': 'test-only-password',
        'db_database': 'application',
        **overrides,
    }
    return DataSourceSettings(**values)


@pytest.mark.parametrize('mode', ['disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'])
def test_postgresql_ssl_modes_reach_both_drivers(mode: str) -> None:
    source = _source(db_ssl_mode=mode)
    adapter = _driver_adapter(source)

    assert adapter.build_connect_args(source, sync=False)['ssl'] == mode
    assert adapter.build_connect_args(source, sync=True)['sslmode'] == mode


def test_unconfigured_ssl_preserves_driver_defaults() -> None:
    source = _source()
    adapter = _driver_adapter(source)

    assert source.db_ssl_mode is None
    assert 'ssl' not in adapter.build_connect_args(source, sync=False)
    assert 'sslmode' not in adapter.build_connect_args(source, sync=True)


def test_invalid_ssl_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _source(db_ssl_mode='typo-require')


def test_mysql_does_not_receive_postgresql_ssl_options() -> None:
    source = _source(db_type='mysql', db_port=3306)
    adapter = _driver_adapter(source)

    assert 'ssl' not in adapter.build_connect_args(source, sync=False)
    assert 'sslmode' not in adapter.build_connect_args(source, sync=True)


@pytest.mark.parametrize('external_port', [None, '19003'])
def test_local_override_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_port: str | None
) -> None:
    (tmp_path / '.env.dev').write_text('APP_PORT=19001\n', encoding='utf-8')
    (tmp_path / '.env.dev.local').write_text('APP_PORT=19002\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['test', '--env', 'dev'])
    monkeypatch.delenv('APP_PORT', raising=False)
    if external_port is not None:
        monkeypatch.setenv('APP_PORT', external_port)

    assert GetConfig.parse_cli_args() == 'dev'
    assert os.environ['APP_PORT'] == (external_port or '19002')
    assert AppSettings().app_port == int(external_port or '19002')


def test_base_environment_still_loads_without_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / '.env.dev').write_text('APP_PORT=19001\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['test', '--env', 'dev'])
    monkeypatch.delenv('APP_PORT', raising=False)

    assert GetConfig.parse_cli_args() == 'dev'
    assert os.environ['APP_PORT'] == '19001'
