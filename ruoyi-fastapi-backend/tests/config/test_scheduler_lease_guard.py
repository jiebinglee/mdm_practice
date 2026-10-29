import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from config.env import RedisConfig, SchedulerConfig
from config.scheduler import executors, lease
from config.scheduler.events import JobSnapshot, TimedJobExecutionEvent

OWNER_TOKEN = 'scheduler-guard-test-owner'
CLOCK_START = 0.0
EXIT_CODE = 7
EXPECTED_OWNERSHIP_CHECKS = 2
EXPECTED_RENEWALS = 2


@pytest.fixture(autouse=True)
def guard_state(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """使用同步Redis替身、假线程和时钟，禁止真实进程退出。"""
    redis = MagicMock(name='sync_redis')
    redis.__enter__.return_value = redis
    redis.__exit__.return_value = False
    redis.eval.return_value = 1
    redis_class = Mock(return_value=redis)
    thread = Mock(ident=None)
    thread_class = Mock(return_value=thread)
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    clock = Mock(return_value=CLOCK_START)
    fatal = Mock()
    exit_process = Mock()
    children = Mock(return_value=[])

    monkeypatch.setattr(lease, 'Redis', redis_class)
    monkeypatch.setattr(lease, 'threading', SimpleNamespace(Thread=thread_class, Event=Mock(return_value=stop)))
    monkeypatch.setattr(lease, 'time', SimpleNamespace(monotonic=clock))
    # 替换模块引用而非全局os._exit，其他测试和pytest自身不受影响。
    monkeypatch.setattr(lease, 'os', SimpleNamespace(getpid=Mock(return_value=1234), _exit=exit_process))
    monkeypatch.setattr(lease, 'multiprocessing', SimpleNamespace(active_children=children))

    guard = lease.SchedulerLeaseGuard(OWNER_TOKEN, fatal=fatal)
    revoked = []
    fatal.side_effect = lambda exit_code: revoked.append((exit_code, guard.valid))
    return SimpleNamespace(
        guard=guard,
        redis=redis,
        redis_class=redis_class,
        thread=thread,
        thread_class=thread_class,
        stop=stop,
        clock=clock,
        fatal=fatal,
        revoked=revoked,
        exit_process=exit_process,
        children=children,
    )


def test_guard_starts_a_dedicated_daemon_without_using_an_async_client(guard_state: SimpleNamespace) -> None:
    guard = guard_state.guard

    guard.start()

    guard_state.thread_class.assert_called_once_with(target=guard._run, name='scheduler-lease-guard', daemon=True)
    guard_state.thread.start.assert_called_once_with()
    guard_state.redis_class.assert_not_called()
    assert guard.owner == OWNER_TOKEN
    assert guard.valid is True
    assert guard.ready is False
    guard_state.fatal.assert_not_called()


def test_lease_client_is_synchronous_and_has_bounded_io_without_retries(guard_state: SimpleNamespace) -> None:
    with patch.object(lease, 'Retry') as retry, patch.object(lease, 'NoBackoff') as backoff:
        client = lease.lease_redis()

    assert client is guard_state.redis
    backoff.assert_called_once_with()
    retry.assert_called_once_with(backoff.return_value, 0)
    guard_state.redis_class.assert_called_once_with(
        host=RedisConfig.redis_host,
        port=RedisConfig.redis_port,
        username=RedisConfig.redis_username or None,
        password=RedisConfig.redis_password or None,
        db=RedisConfig.redis_database,
        decode_responses=True,
        socket_connect_timeout=SchedulerConfig.scheduler_io_timeout_seconds,
        socket_timeout=SchedulerConfig.scheduler_io_timeout_seconds,
        retry=retry.return_value,
    )


@pytest.mark.parametrize('ready', [False, True], ids=['starting', 'leader-ready'])
def test_renew_uses_owner_cas_and_reports_starting_or_ready_health(guard_state: SimpleNamespace, ready: bool) -> None:
    guard = guard_state.guard
    guard.ready = ready

    assert guard._renew(guard_state.redis) is True

    guard_state.redis.eval.assert_called_once_with(
        lease.StartupUtil._RENEW_SCRIPT,
        1,
        SchedulerConfig.lease_key,
        OWNER_TOKEN,
        SchedulerConfig.scheduler_lease_seconds,
    )
    guard_state.redis.get.assert_not_called()
    guard_state.redis.set.assert_called_once()
    health = guard_state.redis.set.call_args
    assert health.args[0] == SchedulerConfig.health_key
    assert json.loads(health.args[1]) == {
        'pid': lease.os.getpid(),
        'owner': OWNER_TOKEN,
        'status': 'leader' if ready else 'starting',
    }
    assert health.kwargs == {'ex': SchedulerConfig.scheduler_lease_seconds}
    assert guard.valid is True
    guard_state.fatal.assert_not_called()


def test_renewal_owner_mismatch_immediately_invalidates_before_fatal(guard_state: SimpleNamespace) -> None:
    guard_state.redis.eval.return_value = 0

    guard_state.guard._run()

    assert guard_state.guard.valid is False
    assert guard_state.revoked == [(1, False)]
    guard_state.fatal.assert_called_once_with(1)
    guard_state.redis.eval.assert_called_once_with(
        lease.StartupUtil._RENEW_SCRIPT,
        1,
        SchedulerConfig.lease_key,
        OWNER_TOKEN,
        SchedulerConfig.scheduler_lease_seconds,
    )
    guard_state.redis.set.assert_not_called()
    guard_state.redis.delete.assert_not_called()
    guard_state.stop.wait.assert_not_called()
    guard_state.redis.__exit__.assert_called_once()
    guard_state.exit_process.assert_not_called()


@pytest.mark.parametrize('failure_at', ['client', 'enter', 'renew', 'health'])
@pytest.mark.parametrize('error_type', [RedisConnectionError, RedisTimeoutError])
def test_redis_errors_invalidate_before_fatal(
    guard_state: SimpleNamespace, failure_at: str, error_type: type[Exception]
) -> None:
    failure = error_type('Redis unavailable')
    failing_call = {
        'client': guard_state.redis_class,
        'enter': guard_state.redis.__enter__,
        'renew': guard_state.redis.eval,
        'health': guard_state.redis.set,
    }[failure_at]
    failing_call.side_effect = failure
    guard_state.stop.is_set.side_effect = [False, False]

    guard_state.guard._run()

    assert guard_state.guard.valid is False
    assert guard_state.revoked == [(1, False)]
    guard_state.fatal.assert_called_once_with(1)
    failing_call.assert_called_once()
    guard_state.redis.delete.assert_not_called()
    guard_state.stop.wait.assert_not_called()
    guard_state.exit_process.assert_not_called()


@pytest.mark.parametrize('overdue', [0.0, 1.0], ids=['at-deadline', 'after-deadline'])
def test_stale_pulse_invalidates_before_renewal_and_fatal(guard_state: SimpleNamespace, overdue: float) -> None:
    guard_state.clock.return_value = CLOCK_START + SchedulerConfig.scheduler_stall_seconds + overdue

    guard_state.guard._run()

    assert guard_state.guard.valid is False
    assert guard_state.revoked == [(1, False)]
    guard_state.fatal.assert_called_once_with(1)
    guard_state.redis.eval.assert_not_called()
    guard_state.redis.set.assert_not_called()
    guard_state.stop.wait.assert_not_called()
    guard_state.exit_process.assert_not_called()


def test_valid_guard_renews_repeatedly_with_fresh_pulses(guard_state: SimpleNamespace) -> None:
    guard = guard_state.guard
    guard.ready = True
    guard_state.stop.is_set.side_effect = [False, False, True]

    def pulse_after_wait(_timeout: float) -> None:
        guard_state.clock.return_value += SchedulerConfig.scheduler_renew_seconds
        guard.pulse()

    guard_state.stop.wait.side_effect = pulse_after_wait

    guard._run()

    assert guard.valid is True
    assert guard._pulse == guard_state.clock.return_value
    assert guard_state.redis.eval.call_args_list == [
        call(
            lease.StartupUtil._RENEW_SCRIPT,
            1,
            SchedulerConfig.lease_key,
            OWNER_TOKEN,
            SchedulerConfig.scheduler_lease_seconds,
        )
    ] * EXPECTED_RENEWALS
    assert guard_state.redis.set.call_count == EXPECTED_RENEWALS
    assert guard_state.stop.wait.call_args_list == [call(0.25)] * EXPECTED_RENEWALS
    guard_state.redis_class.assert_called_once()
    guard_state.redis.__enter__.assert_called_once_with()
    guard_state.redis.__exit__.assert_called_once_with(None, None, None)
    guard_state.fatal.assert_not_called()
    guard_state.exit_process.assert_not_called()


@pytest.mark.parametrize('started', [False, True], ids=['not-started', 'started'])
def test_stop_invalidates_and_joins_without_killing_process(guard_state: SimpleNamespace, started: bool) -> None:
    guard_state.thread.ident = 1234 if started else None

    guard_state.guard.stop()

    assert guard_state.guard.valid is False
    guard_state.stop.set.assert_called_once_with()
    assert guard_state.thread.join.call_args_list == (
        [call(timeout=SchedulerConfig.scheduler_io_timeout_seconds + 1)] if started else []
    )
    guard_state.fatal.assert_not_called()
    guard_state.children.assert_not_called()
    guard_state.exit_process.assert_not_called()
    guard_state.redis_class.assert_not_called()


def test_redis_error_during_stop_does_not_invoke_fatal(guard_state: SimpleNamespace) -> None:
    def stop_during_renewal(*_args) -> None:
        guard_state.guard.stop()
        raise RedisConnectionError('connection closed while stopping')

    guard_state.redis.eval.side_effect = stop_during_renewal
    guard_state.stop.is_set.side_effect = [False, True]

    guard_state.guard._run()

    assert guard_state.guard.valid is False
    guard_state.stop.set.assert_called_once_with()
    guard_state.fatal.assert_not_called()
    guard_state.exit_process.assert_not_called()


@pytest.mark.parametrize('current_owner', [OWNER_TOKEN, 'replacement-owner', None], ids=['owned', 'replaced', 'expired'])
def test_owns_scheduler_lease_compares_owner_on_independent_key(
    guard_state: SimpleNamespace, current_owner: str | None
) -> None:
    guard_state.redis.get.return_value = current_owner

    assert lease.owns_scheduler_lease(OWNER_TOKEN) is (current_owner == OWNER_TOKEN)

    guard_state.redis.get.assert_called_once_with(SchedulerConfig.lease_key)
    guard_state.redis.__enter__.assert_called_once_with()
    guard_state.redis.__exit__.assert_called_once_with(None, None, None)
    guard_state.redis.eval.assert_not_called()


def test_owns_scheduler_lease_propagates_redis_error_and_closes_client(guard_state: SimpleNamespace) -> None:
    guard_state.redis.get.side_effect = RedisConnectionError('lease unavailable')

    with pytest.raises(RedisConnectionError, match='lease unavailable'):
        lease.owns_scheduler_lease(OWNER_TOKEN)

    guard_state.redis.__exit__.assert_called_once()
    guard_state.fatal.assert_not_called()


@pytest.mark.parametrize('terminate_error', [None, OSError('already exited'), ValueError('already closed')])
def test_terminate_worker_stops_children_before_mocked_exit(
    guard_state: SimpleNamespace, terminate_error: Exception | None
) -> None:
    first, second = Mock(), Mock()
    first.terminate.side_effect = terminate_error
    guard_state.children.return_value = [first, second]
    operations = Mock()
    operations.attach_mock(first.terminate, 'first')
    operations.attach_mock(second.terminate, 'second')
    operations.attach_mock(guard_state.exit_process, 'exit')

    lease.terminate_worker(EXIT_CODE)

    guard_state.children.assert_called_once_with()
    assert operations.mock_calls == [call.first(), call.second(), call.exit(EXIT_CODE)]


@pytest.fixture
def execution_event() -> TimedJobExecutionEvent:
    snapshot = JobSnapshot(
        name='lease-guard-job',
        jobstore='default',
        executor='default',
        invoke_target='module_task.scheduler_test.job',
        args='',
        kwargs='{}',
        trigger='date',
        time_zone='UTC',
        job_id=42,
        managed=True,
        scheduler_owner=OWNER_TOKEN,
    )
    return TimedJobExecutionEvent(
        EVENT_JOB_EXECUTED, Mock(id='42'), datetime(2026, 1, 1, tzinfo=timezone.utc), snapshot
    )


@pytest.mark.parametrize(
    ('ownership_results', 'claimed'),
    [
        pytest.param([False], False, id='lost-before-claim'),
        pytest.param([RedisConnectionError('offline')], False, id='redis-error-before-claim'),
        pytest.param([True, False], True, id='lost-after-claim'),
        pytest.param([True, RedisConnectionError('offline')], True, id='redis-error-after-claim'),
    ],
)
def test_execution_lease_check_fails_closed_using_owns_helper(
    execution_event: TimedJobExecutionEvent, ownership_results: list[bool | Exception], claimed: bool
) -> None:
    with (
        patch.object(executors, 'owns_scheduler_lease', side_effect=ownership_results) as owns,
        patch.object(
            executors.JobExecutionStore, 'claim', return_value=SimpleNamespace(accepted=True, token='execution-owner')
        ) as claim,
        patch.object(executors.JobExecutionStore, 'finish') as finish,
    ):
        token = executors._claim_execution(execution_event)

    assert token is None
    assert owns.call_args_list == [call(OWNER_TOKEN)] * len(ownership_results)
    assert claim.call_count == int(claimed)
    assert finish.call_count == int(claimed)
    assert execution_event.code == EVENT_JOB_ERROR
    assert execution_event.execution_status == 'failed'
    assert isinstance(execution_event.exception, RuntimeError)
    assert execution_event.start_time is execution_event.end_time is execution_event.run_duration_ms is None
    if claimed:
        finish.assert_called_once_with(
            execution_event.execution_id,
            'execution-owner',
            failed=True,
            message=str(execution_event.exception),
            start_time=None,
            end_time=None,
            run_duration_ms=None,
        )


def test_execution_lease_check_accepts_only_owner_valid_before_and_after_claim(
    execution_event: TimedJobExecutionEvent,
) -> None:
    with (
        patch.object(executors, 'owns_scheduler_lease', return_value=True) as owns,
        patch.object(
            executors.JobExecutionStore, 'claim', return_value=SimpleNamespace(accepted=True, token='execution-owner')
        ) as claim,
        patch.object(executors.JobExecutionStore, 'finish') as finish,
    ):
        token = executors._claim_execution(execution_event)

    assert token == 'execution-owner'
    assert owns.call_args_list == [call(OWNER_TOKEN)] * EXPECTED_OWNERSHIP_CHECKS
    claim.assert_called_once_with(
        execution_event.execution_id,
        execution_event.snapshot.job_id,
        execution_event.snapshot.execution_snapshot(),
        execution_event.scheduled_run_time,
        dispatch_token=execution_event.snapshot.dispatch_token,
    )
    finish.assert_not_called()
    assert execution_event.code == EVENT_JOB_EXECUTED
    assert execution_event.exception is None