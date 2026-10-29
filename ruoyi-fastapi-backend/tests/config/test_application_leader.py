import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

from common.constant import LockConstant
from config import application_leader

Coordinator = application_leader.ApplicationLeaderCoordinator
OWNER_TOKEN = 'application-leader-test-owner'
REACQUIRE_DELAY = 5.25


@pytest_asyncio.fixture(autouse=True)
async def leader_state(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    """Restore every coordinator field and drain every task, including on assertion failures."""
    for name, value in (
        ('_redis', None),
        ('_is_leader', False),
        ('_is_closing', False),
        ('_renewal_task', None),
        ('_reacquire_task', None),
    ):
        monkeypatch.setattr(Coordinator, name, value)

    tasks: list[asyncio.Task] = []

    def spawn(coroutine: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    # Do not patch the shared asyncio module used by pytest's own event loop.
    isolated_asyncio = SimpleNamespace(**vars(asyncio))
    isolated_asyncio.create_task = spawn
    monkeypatch.setattr(application_leader, 'asyncio', isolated_asyncio)

    state = SimpleNamespace(
        redis=AsyncMock(name='redis'),
        acquire=AsyncMock(return_value=False),
        release=AsyncMock(return_value=True),
        start_renewal=Mock(side_effect=lambda **_kwargs: spawn(asyncio.Event().wait())),
        owner_token=Mock(return_value=OWNER_TOKEN),
        worker_id=Mock(return_value='test-worker'),
        log_exception=Mock(),
        spawn=spawn,
        tasks=tasks,
    )
    monkeypatch.setattr(application_leader.StartupUtil, 'acquire_application_leader', state.acquire)
    monkeypatch.setattr(application_leader.StartupUtil, 'release_application_leader', state.release)
    monkeypatch.setattr(application_leader.StartupUtil, 'start_application_leader_renewal', state.start_renewal)
    monkeypatch.setattr(application_leader.StartupUtil, 'get_application_lock_owner_token', state.owner_token)
    monkeypatch.setattr(application_leader.WorkerIdUtil, 'get_worker_id', state.worker_id)
    monkeypatch.setattr(application_leader.logger, 'exception', state.log_exception)
    for target, name in (
        (socket, 'create_connection'),
        (socket.socket, 'connect'),
        (socket.socket, 'connect_ex'),
    ):
        monkeypatch.setattr(target, name, Mock(side_effect=AssertionError('Network access is forbidden')))

    try:
        yield state
    finally:
        Coordinator._is_closing = True
        for name in ('_renewal_task', '_reacquire_task'):
            task = getattr(Coordinator, name)
            if isinstance(task, asyncio.Task) and task not in tasks:
                tasks.append(task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def assert_renewal_started(state: SimpleNamespace) -> None:
    state.start_renewal.assert_called_once_with(
        redis=state.redis,
        lock_key=LockConstant.APP_STARTUP_LOCK_KEY,
        owner_token=OWNER_TOKEN,
        lock_expire_seconds=LockConstant.LOCK_EXPIRE_SECONDS,
        interval_seconds=LockConstant.LOCK_RENEWAL_INTERVAL,
        on_lock_lost=Coordinator.on_lock_lost,
    )


def test_owner_token_uses_configured_worker_identity(leader_state: SimpleNamespace) -> None:
    assert Coordinator.get_application_lock_owner_token() == OWNER_TOKEN
    leader_state.worker_id.assert_called_once_with(application_leader.LogConfig.log_worker_id)
    leader_state.owner_token.assert_called_once_with('test-worker')


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_renewal', [False, True], ids=['new-task', 'reuse-live-task'])
async def test_early_renewal_retains_redis_before_initialization(
    leader_state: SimpleNamespace, existing_renewal: bool
) -> None:
    previous_task = leader_state.spawn(asyncio.Event().wait()) if existing_renewal else None
    Coordinator._renewal_task = previous_task
    Coordinator._is_closing = True

    task = Coordinator.start_application_lock_renewal(leader_state.redis)

    assert Coordinator._redis is leader_state.redis
    assert Coordinator._renewal_task is task
    assert Coordinator.is_application_leader() is True
    assert Coordinator._is_closing is False
    assert not task.done()
    if existing_renewal:
        assert task is previous_task
        leader_state.start_renewal.assert_not_called()
    else:
        assert_renewal_started(leader_state)


@pytest.mark.asyncio
async def test_init_reuses_only_a_live_leader_renewal(leader_state: SimpleNamespace) -> None:
    task = Coordinator.start_application_lock_renewal(leader_state.redis)
    leader_state.start_renewal.reset_mock()

    await Coordinator.init_application_leader(leader_state.redis)

    assert Coordinator._renewal_task is task
    assert Coordinator.is_application_leader() is True
    leader_state.acquire.assert_not_awaited()
    leader_state.start_renewal.assert_not_called()
    assert Coordinator._reacquire_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize('renewal_state', ['missing', 'finished', 'cancelled'])
@pytest.mark.parametrize('acquired', [False, True], ids=['lease-denied', 'lease-acquired'])
async def test_init_does_not_trust_leader_flag_without_valid_renewal(
    leader_state: SimpleNamespace, renewal_state: str, acquired: bool
) -> None:
    previous_task = None
    if renewal_state == 'finished':
        previous_task = leader_state.spawn(asyncio.sleep(0))
        await previous_task
    elif renewal_state == 'cancelled':
        previous_task = leader_state.spawn(asyncio.Event().wait())
        previous_task.cancel()
        await asyncio.gather(previous_task, return_exceptions=True)
    Coordinator._renewal_task = previous_task
    Coordinator._is_leader = True
    leader_state.acquire.return_value = acquired

    await Coordinator.init_application_leader(leader_state.redis)

    leader_state.acquire.assert_awaited_once_with(
        leader_state.redis, LockConstant.APP_STARTUP_LOCK_KEY, OWNER_TOKEN, LockConstant.LOCK_EXPIRE_SECONDS
    )
    assert Coordinator._redis is leader_state.redis
    assert Coordinator.is_application_leader() is acquired
    if acquired:
        assert_renewal_started(leader_state)
        assert Coordinator._renewal_task is not previous_task
        assert not Coordinator._renewal_task.done()
        assert Coordinator._reacquire_task is None
    else:
        leader_state.start_renewal.assert_not_called()
        assert Coordinator._reacquire_task is not None
        assert not Coordinator._reacquire_task.done()


@pytest.mark.asyncio
async def test_init_rechecks_lease_if_leader_flag_is_revoked_despite_live_task(leader_state: SimpleNamespace) -> None:
    Coordinator._renewal_task = leader_state.spawn(asyncio.Event().wait())
    leader_state.acquire.return_value = True

    await Coordinator.init_application_leader(leader_state.redis)

    leader_state.acquire.assert_awaited_once_with(
        leader_state.redis, LockConstant.APP_STARTUP_LOCK_KEY, OWNER_TOKEN, LockConstant.LOCK_EXPIRE_SECONDS
    )
    assert Coordinator.is_application_leader() is True


@pytest.mark.asyncio
async def test_nonleader_schedules_one_reacquire_task(leader_state: SimpleNamespace) -> None:
    await Coordinator.init_application_leader(leader_state.redis)
    task = Coordinator._reacquire_task
    Coordinator._ensure_reacquire()
    Coordinator._ensure_reacquire()

    assert Coordinator._redis is leader_state.redis
    assert Coordinator.is_application_leader() is False
    assert Coordinator._is_closing is False
    assert Coordinator._reacquire_task is task
    assert task is not None and not task.done()
    assert leader_state.tasks == [task]
    leader_state.start_renewal.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_reacquire', [False, True], ids=['schedule-retry', 'keep-existing-retry'])
async def test_lock_loss_revokes_leadership_and_ensures_reacquire(
    leader_state: SimpleNamespace, existing_reacquire: bool
) -> None:
    Coordinator._redis = leader_state.redis
    Coordinator._is_leader = True
    previous_task = leader_state.spawn(asyncio.Event().wait()) if existing_reacquire else None
    Coordinator._reacquire_task = previous_task

    Coordinator.on_lock_lost()
    task = Coordinator._reacquire_task
    Coordinator.on_lock_lost()

    assert Coordinator.is_application_leader() is False
    assert Coordinator._redis is leader_state.redis
    assert task is not None and not task.done()
    assert Coordinator._reacquire_task is task
    assert leader_state.tasks == [task]
    if existing_reacquire:
        assert task is previous_task


@pytest.mark.asyncio
@pytest.mark.parametrize('closing', [False, True], ids=['no-redis', 'closing'])
async def test_lock_loss_does_not_schedule_without_redis_or_while_closing(
    leader_state: SimpleNamespace, closing: bool
) -> None:
    Coordinator._redis = leader_state.redis if closing else None
    Coordinator._is_closing = closing
    Coordinator._is_leader = True

    Coordinator.on_lock_lost()

    assert Coordinator.is_application_leader() is False
    assert Coordinator._reacquire_task is None
    assert not leader_state.tasks


@pytest.mark.asyncio
async def test_reacquire_replaces_a_finished_task(leader_state: SimpleNamespace) -> None:
    previous_task = leader_state.spawn(asyncio.sleep(0))
    await previous_task
    Coordinator._redis = leader_state.redis
    Coordinator._reacquire_task = previous_task

    Coordinator._ensure_reacquire()

    assert Coordinator._reacquire_task is not previous_task
    assert not Coordinator._reacquire_task.done()


@pytest.mark.asyncio
async def test_close_cancels_both_tasks_and_releases_before_forgetting_redis(leader_state: SimpleNamespace) -> None:
    renewal_task = Coordinator.start_application_lock_renewal(leader_state.redis)
    reacquire_task = leader_state.spawn(asyncio.Event().wait())
    Coordinator._reacquire_task = reacquire_task
    during_release = []

    async def release(redis: object, key: str, owner: str) -> bool:
        during_release.append(
            (
                Coordinator._redis,
                renewal_task.cancelled(),
                reacquire_task.cancelled(),
                Coordinator.is_application_leader(),
                Coordinator._is_closing,
            )
        )
        assert (redis, key, owner) == (leader_state.redis, LockConstant.APP_STARTUP_LOCK_KEY, OWNER_TOKEN)
        return True

    leader_state.release.side_effect = release

    await Coordinator.close()

    leader_state.release.assert_awaited_once_with(leader_state.redis, LockConstant.APP_STARTUP_LOCK_KEY, OWNER_TOKEN)
    assert during_release == [(leader_state.redis, True, True, False, True)]
    assert Coordinator._renewal_task is None
    assert Coordinator._reacquire_task is None
    assert Coordinator._redis is None
    assert Coordinator.is_application_leader() is False
    assert Coordinator._is_closing is True
    await Coordinator.close()
    leader_state.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_reacquire_recovers_after_transient_redis_failure(
    monkeypatch: pytest.MonkeyPatch, leader_state: SimpleNamespace
) -> None:
    leader_state.acquire.side_effect = [False, RedisConnectionError('temporary Redis failure'), True]
    # A third sleep fails outside the retry handler, bounding even a broken retry loop.
    sleep = AsyncMock(side_effect=[None, None, AssertionError('Unexpected extra reacquire attempt')])
    uniform = Mock(return_value=0.25)
    monkeypatch.setattr(application_leader.asyncio, 'sleep', sleep)
    monkeypatch.setattr(application_leader, 'random', SimpleNamespace(uniform=uniform))

    await Coordinator.init_application_leader(leader_state.redis)
    retry_task = Coordinator._reacquire_task
    assert retry_task is not None
    await asyncio.wait_for(retry_task, timeout=1)

    acquisition = call(
        leader_state.redis, LockConstant.APP_STARTUP_LOCK_KEY, OWNER_TOKEN, LockConstant.LOCK_EXPIRE_SECONDS
    )
    assert leader_state.acquire.await_args_list == [acquisition, acquisition, acquisition]
    assert sleep.await_args_list == [call(REACQUIRE_DELAY), call(REACQUIRE_DELAY)]
    assert uniform.call_args_list == [call(0, 1), call(0, 1)]
    leader_state.log_exception.assert_called_once()
    assert_renewal_started(leader_state)
    assert Coordinator.is_application_leader() is True
    assert not Coordinator._renewal_task.done()
    assert Coordinator._reacquire_task is None


@pytest.mark.asyncio
async def test_startup_error_cleanup_releases_early_lease_without_full_initialization(leader_state: SimpleNamespace) -> None:
    renewal_task = Coordinator.start_application_lock_renewal(leader_state.redis)

    with pytest.raises(RuntimeError, match='startup failed before runtime initialization'):
        try:
            raise RuntimeError('startup failed before runtime initialization')
        finally:
            await Coordinator.close()

    assert renewal_task.cancelled()
    leader_state.acquire.assert_not_awaited()
    leader_state.release.assert_awaited_once_with(leader_state.redis, LockConstant.APP_STARTUP_LOCK_KEY, OWNER_TOKEN)
    assert Coordinator._redis is None
    assert Coordinator._renewal_task is None
    assert Coordinator._reacquire_task is None
    assert Coordinator.is_application_leader() is False
    assert Coordinator._is_closing is True


def test_fresh_import_does_not_import_scheduler_or_access_network() -> None:
    script = dedent(
        '''
        import importlib
        import socket
        import sys

        def forbid_network(*args, **kwargs):
            raise AssertionError('Import attempted network access')

        socket.create_connection = forbid_network
        socket.socket.connect = forbid_network
        socket.socket.connect_ex = forbid_network
        importlib.import_module('config.application_leader')
        scheduler_modules = sorted(
            name for name in sys.modules
            if name in ('config.scheduler', 'apscheduler')
            or name.startswith(('config.scheduler.', 'apscheduler.'))
        )
        assert not scheduler_modules, scheduler_modules
        print('application_leader import is scheduler-free')
        '''
    )
    # Inherit the parent's dummy DB environment; never fall back to service configuration.
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
    assert 'application_leader import is scheduler-free' in result.stdout
