import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apscheduler.util import obj_to_ref, ref_to_obj

from config.env import DataBaseConfig, SchedulerConfig
from config.scheduler.manager import SchedulerManager

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_scheduler_database_engines_keep_jobstore_logging_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    jobstore_engine = MagicMock()
    listener_engine = MagicMock()
    create_engine = MagicMock(return_value=jobstore_engine)

    monkeypatch.setattr(SchedulerManager._resources, '_jobstore_engine', None)
    monkeypatch.setattr(SchedulerManager._resources, '_listener_engine', None)
    monkeypatch.setattr(SchedulerManager._resources, '_session_local', None)
    monkeypatch.setattr(SchedulerManager._resources, '_disposed', False)
    monkeypatch.setattr('config.scheduler.resources.create_sync_db_engine', create_engine)
    monkeypatch.setattr('config.scheduler.resources.DataSourceRegistry.get_sync_engine', lambda _name: listener_engine)

    source = DataBaseConfig.get_source()
    original_config = source.model_dump()
    assert SchedulerManager._resources.jobstore_engine() is jobstore_engine
    assert SchedulerManager._resources.listener_engine() is listener_engine
    assert jobstore_engine is not listener_engine
    create_engine.assert_called_once()
    assert create_engine.call_args.args == ()
    kwargs = create_engine.call_args.kwargs
    assert kwargs['echo'] is False
    copied_source = kwargs['config']
    assert copied_source is not source
    assert source.model_dump() == original_config
    timeout_fields = {'db_connect_timeout', 'db_pool_timeout'}
    assert copied_source.model_dump(exclude=timeout_fields) == source.model_dump(exclude=timeout_fields)
    timeout = SchedulerConfig.scheduler_io_timeout_seconds
    assert 0 < copied_source.db_connect_timeout <= timeout
    assert 0 < copied_source.db_pool_timeout <= timeout
    if source.db_type == 'postgresql':
        assert kwargs['connect_args'] == {
            'options': f'-c timezone=UTC -c statement_timeout={max(1, int(timeout * 1000))}'
        }
    else:
        assert kwargs['connect_args'] == {'read_timeout': timeout, 'write_timeout': timeout}


def test_scheduler_cleanup_does_not_dispose_registry_listener_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    jobstore_engine = MagicMock()
    listener_engine = MagicMock()
    monkeypatch.setattr(SchedulerManager._resources, '_jobstore_engine', jobstore_engine)
    monkeypatch.setattr(SchedulerManager._resources, '_listener_engine', listener_engine)
    monkeypatch.setattr(SchedulerManager._resources, '_session_local', MagicMock())
    monkeypatch.setattr(SchedulerManager._resources, '_disposed', False)

    SchedulerManager._resources.dispose()

    jobstore_engine.dispose.assert_called_once_with()
    listener_engine.dispose.assert_not_called()
    assert SchedulerManager._resources._jobstore_engine is None
    assert SchedulerManager._resources._listener_engine is None


@pytest.mark.parametrize('method_name', ['request_scheduler_sync', '_drain_execution_requests'])
def test_registered_scheduler_callbacks_survive_serialization(method_name: str) -> None:
    method = getattr(SchedulerManager, method_name)
    assert ref_to_obj(obj_to_ref(method)) == method


def test_dao_can_import_adapter_before_scheduler_without_circular_import() -> None:
    script = """
import json
import sys

import module_admin.dao.job_runtime_dao

manager_loaded_by_dao = 'config.scheduler.manager' in sys.modules
from config.scheduler.manager import SchedulerManager, scheduler

print(json.dumps({
    'managerLoadedByDao': manager_loaded_by_dao,
    'sameScheduler': SchedulerManager._scheduler is scheduler,
    'schedulerRunning': scheduler.running,
    'schedulerTimezone': str(scheduler.timezone),
}))
"""
    completed = subprocess.run(
        [sys.executable, '-s', '-c', script],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        'managerLoadedByDao': False,
        'sameScheduler': True,
        'schedulerRunning': False,
        'schedulerTimezone': 'UTC',
    }
