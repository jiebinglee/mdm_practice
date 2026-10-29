import asyncio
import json
from collections.abc import Coroutine
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from common.constant import LockConstant
from config.env import AppConfig, SchedulerConfig
from config.scheduler import manager
from config.scheduler.manager import SchedulerManager

EXPECTED_NOTIFICATIONS = 2
EXPECTED_REACQUIRE_ATTEMPTS = 2


@pytest.fixture(autouse=True)
def scheduler_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离Manager状态和资源；即使断言失败也恢复原来的租约守护器。"""
    for name, value in (
        ('_redis', None),
        ('_is_leader', False),
        ('_is_closing', False),
        ('_lease_guard', None),
        ('_sync_listener_task', None),
        ('_sync_task', None),
        ('_reacquire_task', None),
        ('_lock_lost_task', None),
        ('_sync_pending', False),
        ('_sync_full_pending', False),
        ('_sync_pending_ids', set()),
        ('_scheduler', MagicMock(running=False)),
        ('_resources', MagicMock()),
        ('_jobs', MagicMock()),
        ('_synchronizer', MagicMock()),
    ):
        monkeypatch.setattr(SchedulerManager, name, value)

    # 不修改pytest和其他用例共用的asyncio模块，也不启动真实线程。
    isolated_asyncio = SimpleNamespace(**vars(asyncio))
    isolated_asyncio.to_thread = AsyncMock(side_effect=lambda function: function())
    monkeypatch.setattr(manager, 'asyncio', isolated_asyncio)


@pytest.mark.asyncio
@pytest.mark.parametrize('running', [False, True])
async def test_scheduler_activation_failure_stops_renewal_and_releases_lease(running: bool) -> None:
    """启动失败时关闭已运行的Scheduler并仅释放独立调度租约。"""
    redis = MagicMock()
    scheduler = SchedulerManager._scheduler
    scheduler.running = running
    dispose = SchedulerManager._resources.dispose
    SchedulerManager._is_leader = True

    with (
        patch.object(SchedulerManager, 'start_scheduler_lock_renewal') as start_renewal,
        patch.object(
            SchedulerManager,
            '_start_scheduler_as_leader',
            new=AsyncMock(side_effect=RuntimeError('scheduler startup failed')),
        ),
        patch.object(
            SchedulerManager,
            'stop_scheduler_lock_renewal',
            new_callable=AsyncMock,
        ) as stop_renewal,
        patch(
            'config.scheduler.manager.StartupUtil.release_application_leader',
            new_callable=AsyncMock,
        ) as release_application_leader,
        pytest.raises(RuntimeError, match='scheduler startup failed'),
    ):
        await SchedulerManager._activate_scheduler_as_leader(redis)

    start_renewal.assert_called_once_with(redis)
    assert scheduler.shutdown.call_args_list == ([call(wait=False)] if running else [])
    dispose.assert_called_once_with()
    stop_renewal.assert_awaited_once_with()
    release_application_leader.assert_awaited_once_with(
        redis,
        SchedulerConfig.lease_key,
        SchedulerManager.get_scheduler_lock_owner_token(),
    )
    assert SchedulerConfig.lease_key != LockConstant.APP_STARTUP_LOCK_KEY
    assert SchedulerManager._is_leader is False


@pytest.mark.asyncio
async def test_scheduler_restore_failure_cleans_up_without_resuming_or_masking_error() -> None:
    """持久化任务恢复失败必须走完整启动清理，且保留最初的异常。"""
    redis = MagicMock()
    scheduler = SchedulerManager._scheduler
    scheduler.running = True
    failure = RuntimeError('restore failed')

    with (
        patch.object(SchedulerManager, 'start_scheduler_lock_renewal') as start_renewal,
        patch.object(SchedulerManager, 'stop_scheduler_lock_renewal', new_callable=AsyncMock) as stop_renewal,
        patch.object(SchedulerManager, '_configure_scheduler') as configure,
        patch.object(SchedulerManager, '_sync_jobs_from_database', new=AsyncMock(side_effect=failure)) as restore,
        patch(
            'config.scheduler.manager.StartupUtil.release_application_leader',
            new=AsyncMock(side_effect=ConnectionError('release unavailable')),
        ) as release,
        pytest.raises(RuntimeError, match='restore failed') as raised,
    ):
        await SchedulerManager._activate_scheduler_as_leader(redis)

    assert raised.value is failure
    start_renewal.assert_called_once_with(redis)
    configure.assert_called_once_with()
    scheduler.start.assert_called_once_with(paused=True)
    restore.assert_awaited_once_with(raise_errors=True)
    scheduler.resume.assert_not_called()
    scheduler.add_job.assert_not_called()
    scheduler.shutdown.assert_called_once_with(wait=False)
    SchedulerManager._resources.dispose.assert_called_once_with()
    stop_renewal.assert_awaited_once_with()
    release.assert_awaited_once_with(redis, SchedulerConfig.lease_key, SchedulerManager.get_scheduler_lock_owner_token())
    assert SchedulerManager._is_leader is False
    assert SchedulerManager._sync_listener_task is None


def test_scheduler_lock_renewal_reuses_guard_without_starting_another_thread() -> None:
    """重复续租入口复用同一守护器，不创建线程或Redis连接。"""
    redis = MagicMock()
    replacement_redis = MagicMock()

    with patch('config.scheduler.manager.SchedulerLeaseGuard') as guard_class:
        SchedulerManager.start_scheduler_lock_renewal(redis)
        guard = SchedulerManager._lease_guard
        SchedulerManager.start_scheduler_lock_renewal(replacement_redis)

    guard_class.assert_called_once_with(SchedulerManager.get_scheduler_lock_owner_token())
    guard.start.assert_called_once_with()
    assert SchedulerManager._lease_guard is guard
    assert SchedulerManager._redis is replacement_redis


@pytest.mark.asyncio
async def test_scheduler_lock_renewal_stop_clears_guard_and_is_idempotent() -> None:
    guard = MagicMock()
    SchedulerManager._lease_guard = guard

    await SchedulerManager.stop_scheduler_lock_renewal()
    await SchedulerManager.stop_scheduler_lock_renewal()

    guard.stop.assert_called_once_with()
    manager.asyncio.to_thread.assert_awaited_once_with(guard.stop)
    assert SchedulerManager._lease_guard is None


@pytest.mark.parametrize('is_leader', [False, True])
@pytest.mark.parametrize('guard_valid', [None, False, True], ids=['no-guard', 'revoked-guard', 'valid-guard'])
def test_scheduler_leader_state_requires_valid_guard_if_present(is_leader: bool, guard_valid: bool | None) -> None:
    SchedulerManager._is_leader = is_leader
    SchedulerManager._lease_guard = None if guard_valid is None else MagicMock(valid=guard_valid)

    assert SchedulerManager.is_scheduler_leader() is (is_leader and guard_valid is not False)


@pytest.mark.asyncio
async def test_non_leader_scheduler_starts_reacquire_task() -> None:
    """校验首次获取独立Scheduler锁失败后会持续参与leader竞争。"""
    redis = MagicMock()
    scheduled_task = MagicMock()
    scheduled_task.done.return_value = False
    original_state = (
        SchedulerManager._redis,
        SchedulerManager._is_leader,
        SchedulerManager._reacquire_task,
        SchedulerManager._is_closing,
    )
    SchedulerManager._redis = None
    SchedulerManager._is_leader = False
    SchedulerManager._reacquire_task = None
    SchedulerManager._is_closing = False

    def create_task(coroutine: Coroutine) -> MagicMock:
        coroutine.close()
        return scheduled_task

    try:
        with (
            patch(
                'config.scheduler.manager.StartupUtil.acquire_application_leader',
                new=AsyncMock(return_value=False),
            ) as acquire_application_leader,
            patch('config.scheduler.manager.asyncio.create_task', side_effect=create_task) as create_task_mock,
        ):
            await SchedulerManager.init_system_scheduler(redis)

        acquire_application_leader.assert_awaited_once_with(
            redis=redis,
            lock_key=SchedulerConfig.lease_key,
            owner_token=SchedulerManager.get_scheduler_lock_owner_token(),
            lock_expire_seconds=SchedulerConfig.scheduler_lease_seconds,
        )
        create_task_mock.assert_called_once()
        assert SchedulerManager._reacquire_task is scheduled_task
        assert SchedulerManager._is_closing is False
    finally:
        (
            SchedulerManager._redis,
            SchedulerManager._is_leader,
            SchedulerManager._reacquire_task,
            SchedulerManager._is_closing,
        ) = original_state


def test_scheduler_reacquire_task_is_idempotent_and_disabled_while_closing() -> None:
    """校验每个worker只创建一个竞争任务，关闭期不再新建任务。"""
    redis = MagicMock()
    existing_task = MagicMock()
    existing_task.done.return_value = False
    original_state = (
        SchedulerManager._redis,
        SchedulerManager._reacquire_task,
        SchedulerManager._is_closing,
    )
    SchedulerManager._redis = redis
    SchedulerManager._reacquire_task = existing_task
    SchedulerManager._is_closing = False

    try:
        with patch('config.scheduler.manager.asyncio.create_task') as create_task_mock:
            SchedulerManager._ensure_reacquire_task()
            SchedulerManager._ensure_reacquire_task()

            create_task_mock.assert_not_called()
            assert SchedulerManager._reacquire_task is existing_task

            SchedulerManager._reacquire_task = None
            SchedulerManager._is_closing = True
            SchedulerManager._ensure_reacquire_task()

            create_task_mock.assert_not_called()
            assert SchedulerManager._reacquire_task is None
    finally:
        SchedulerManager._redis, SchedulerManager._reacquire_task, SchedulerManager._is_closing = original_state


def test_scheduler_reacquire_delay_includes_jitter() -> None:
    """校验重新竞争间隔包含有界随机抖动。"""
    with patch('config.scheduler.manager.random.uniform', return_value=0.25) as uniform:
        delay = SchedulerManager._get_reacquire_delay()

    uniform.assert_called_once_with(0, SchedulerManager._reacquire_jitter_seconds)
    assert delay == SchedulerManager._reacquire_interval_seconds + 0.25


@pytest.mark.asyncio
async def test_scheduler_close_releases_owner_lease_before_forgetting_redis() -> None:
    """校验关闭Scheduler时使用进程owner token原子释放租约。"""
    redis = MagicMock()
    original_redis = SchedulerManager._redis
    original_is_leader = SchedulerManager._is_leader
    original_is_closing = SchedulerManager._is_closing
    original_tasks = (
        SchedulerManager._sync_listener_task,
        SchedulerManager._sync_task,
        SchedulerManager._reacquire_task,
        SchedulerManager._lock_lost_task,
    )
    SchedulerManager._redis = redis
    SchedulerManager._is_leader = True
    SchedulerManager._is_closing = False
    SchedulerManager._sync_listener_task = None
    SchedulerManager._sync_task = None
    reacquire_task = asyncio.create_task(asyncio.Event().wait())
    SchedulerManager._reacquire_task = reacquire_task
    SchedulerManager._lock_lost_task = None

    try:
        with (
            patch.object(
                SchedulerManager,
                'stop_scheduler_lock_renewal',
                new_callable=AsyncMock,
            ) as stop_renewal,
            patch.object(SchedulerManager._resources, 'dispose') as dispose,
            patch('config.scheduler.manager.SchedulerManager._scheduler', running=False),
            patch(
                'config.scheduler.manager.StartupUtil.release_application_leader',
                new=AsyncMock(return_value=True),
            ) as release_application_leader,
        ):
            await SchedulerManager.close_system_scheduler()

        stop_renewal.assert_awaited_once_with()
        dispose.assert_called_once_with()
        release_application_leader.assert_awaited_once_with(
            redis,
            SchedulerConfig.lease_key,
            SchedulerManager.get_scheduler_lock_owner_token(),
        )
        assert SchedulerManager._redis is None
        assert SchedulerManager._is_leader is False
        assert SchedulerManager._is_closing is True
        assert reacquire_task.cancelled()
        assert SchedulerManager._reacquire_task is None
    finally:
        if not reacquire_task.done():
            reacquire_task.cancel()
        await asyncio.gather(reacquire_task, return_exceptions=True)
        SchedulerManager._redis = original_redis
        SchedulerManager._is_leader = original_is_leader
        SchedulerManager._is_closing = original_is_closing
        (
            SchedulerManager._sync_listener_task,
            SchedulerManager._sync_task,
            SchedulerManager._reacquire_task,
            SchedulerManager._lock_lost_task,
        ) = original_tasks


def test_scheduler_lock_lost_callback_revokes_leader_state() -> None:
    """校验租约丢失回调会立即撤销Scheduler leader状态并安排降级清理。"""
    original_is_leader = SchedulerManager._is_leader
    original_lock_lost_task = SchedulerManager._lock_lost_task
    scheduled_task = MagicMock()

    def create_task(coroutine: Coroutine) -> MagicMock:
        coroutine.close()
        return scheduled_task

    SchedulerManager._is_leader = True
    SchedulerManager._lock_lost_task = None
    try:
        with patch('config.scheduler.manager.asyncio.create_task', side_effect=create_task) as create_task_mock:
            SchedulerManager.on_lock_lost()

        assert SchedulerManager._is_leader is False
        assert SchedulerManager._lock_lost_task is scheduled_task
        create_task_mock.assert_called_once()
    finally:
        SchedulerManager._is_leader = original_is_leader
        SchedulerManager._lock_lost_task = original_lock_lost_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('acquire_results', 'activate_results'),
    [
        pytest.param([ConnectionError('redis unavailable'), True], [None], id='redis-failure'),
        pytest.param([True, True], [RuntimeError('restore failed'), None], id='restore-failure'),
    ],
)
async def test_scheduler_reacquires_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
    acquire_results: list[bool | Exception],
    activate_results: list[Exception | None],
) -> None:
    """Redis或调度器恢复短暂失败后，继续竞争租约并完成启动。"""
    redis = MagicMock()
    monkeypatch.setattr(SchedulerManager, '_redis', redis)
    monkeypatch.setattr(SchedulerManager, '_is_leader', False)
    monkeypatch.setattr(SchedulerManager, '_is_closing', False)
    monkeypatch.setattr(SchedulerManager, '_reacquire_task', None)
    with (
        patch(
            'config.scheduler.manager.StartupUtil.acquire_application_leader',
            new=AsyncMock(side_effect=acquire_results),
        ) as acquire,
        patch.object(
            SchedulerManager, '_activate_scheduler_as_leader', new=AsyncMock(side_effect=activate_results)
        ) as activate,
        patch.object(
            SchedulerManager, '_get_reacquire_delay', return_value=SchedulerManager._reacquire_interval_seconds
        ),
        patch(
            'config.scheduler.manager.asyncio.sleep',
            new=AsyncMock(side_effect=[None, None, AssertionError('unexpected extra retry')]),
        ) as sleep,
    ):
        await SchedulerManager._run_reacquire_loop()

    assert acquire.await_args_list == [
        call(
            redis=redis,
            lock_key=SchedulerConfig.lease_key,
            owner_token=SchedulerManager.get_scheduler_lock_owner_token(),
            lock_expire_seconds=SchedulerConfig.scheduler_lease_seconds,
        )
    ] * EXPECTED_REACQUIRE_ATTEMPTS
    assert activate.await_args_list == [call(redis)] * len(activate_results)
    assert sleep.await_args_list == [call(SchedulerManager._reacquire_interval_seconds)] * EXPECTED_REACQUIRE_ATTEMPTS
    assert SchedulerManager._reacquire_task is None


@pytest.mark.asyncio
async def test_single_worker_still_enables_sync_and_notifies_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """单Web worker也必须通知独立Scheduler，不能以worker数量跳过同步。"""
    monkeypatch.setattr(AppConfig, 'app_workers', 1)
    redis = MagicMock()
    redis.publish = AsyncMock()
    SchedulerManager._redis = redis
    SchedulerManager._is_leader = False

    with patch.object(SchedulerManager, '_sync_jobs_from_database', new_callable=AsyncMock) as sync:
        assert SchedulerManager._should_enable_scheduler_sync() is True
        result = await SchedulerManager.request_scheduler_sync({42}, immediate=True)
        await SchedulerManager.request_execution_dispatch()

    assert result['syncStatus'] == 'pending'
    assert redis.publish.await_count == EXPECTED_NOTIFICATIONS
    assert [notification.args[0] for notification in redis.publish.await_args_list] == [
        SchedulerManager._sync_channel,
        SchedulerManager._sync_channel,
    ]
    assert [json.loads(notification.args[1]) for notification in redis.publish.await_args_list] == [
        {'jobIds': [42]},
        {'executions': True},
    ]
    sync.assert_not_awaited()
    assert SchedulerManager._sync_task is None
