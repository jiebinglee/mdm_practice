from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from config.scheduler.client import SchedulerCommandClient
from config.scheduler.manager import SchedulerManager
from exceptions.exception import ServiceException
from module_admin.dao.job_dao import JobDao
from module_admin.entity.do.job_do import SysJob
from module_admin.entity.do.job_runtime_do import SysJobExecution, SysJobSync
from module_admin.entity.vo.job_vo import DeleteJobModel, EditJobModel, JobModel
from module_admin.service.job_service import JobService
from tests.scheduler_helpers import Runtime, job_info


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['add', 'edit', 'disable', 'delete'])
@pytest.mark.parametrize('commit_fails', [False, True])
async def test_job_mutations_only_request_targeted_sync_after_commit(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, action: str, commit_fails: bool
) -> None:
    if action != 'add':
        await runtime.save()
        await SchedulerManager._sync_jobs_from_database()
    original = runtime.scheduler.get_job('42')
    original_jobs = runtime.scheduler.get_jobs()
    expected_version = 1 if action == 'add' else 2
    events = []
    async with runtime.sessions() as db:
        commit = db.commit

        async def committing() -> None:
            assert runtime.scheduler.get_job('42') is original
            assert runtime.scheduler.get_jobs() == original_jobs
            events.append('commit')
            if commit_fails:
                raise RuntimeError('transaction failed')
            await commit()

        async def notify(job_ids: set[int]) -> dict:
            assert events == ['commit']
            assert len(job_ids) == 1
            assert runtime.scheduler.get_jobs() == original_jobs
            async with runtime.sessions() as committed_db:
                job_id = next(iter(job_ids))
                state = await committed_db.get(SysJobSync, job_id)
                assert state.sync_status == 'pending'
                assert state.config_version == expected_version
                assert state.applied_version == expected_version - 1
                assert state.deleted == (action == 'delete')
                job = await committed_db.get(SysJob, job_id)
                if action == 'delete':
                    assert job is None
                else:
                    assert job.status == ('1' if action == 'disable' else '0')
                    assert job.time_zone == ('UTC' if action == 'edit' else 'America/New_York')
            events.append('notify')
            return {
                'syncStatus': 'pending',
                'jobs': [{'jobId': job_id, 'syncStatus': 'pending'} for job_id in sorted(job_ids)],
            }

        request_sync = AsyncMock(side_effect=notify)
        monkeypatch.setattr(db, 'commit', committing)
        monkeypatch.setattr(SchedulerCommandClient, 'request_scheduler_sync', request_sync)
        method, argument = {
            'add': (JobService.add_job_services, job_info()),
            'edit': (JobService.edit_job_services, EditJobModel(jobId=42, timeZone='UTC')),
            'disable': (JobService.edit_job_services, EditJobModel(jobId=42, status='1', type='status')),
            'delete': (JobService.delete_job_services, DeleteJobModel(jobIds='42')),
        }[action]
        if commit_fails:
            with pytest.raises(RuntimeError, match='transaction failed'):
                await method(db, argument)
            assert events == ['commit']
            assert runtime.scheduler.get_job('42') is original
            assert runtime.scheduler.get_jobs() == original_jobs
            request_sync.assert_not_awaited()
        else:
            result = await method(db, argument)
            assert result.result['saved'] is True
            assert result.result['syncStatus'] == 'pending'
            assert events == ['commit', 'notify']
            assert len(result.result['jobs']) == 1
            state = result.result['jobs'][0]
            assert state['syncStatus'] == 'pending'
            assert state['configVersion'] == expected_version
            assert state['appliedVersion'] == expected_version - 1
            job_id = state['jobId']
            request_sync.assert_awaited_once_with({job_id})
            assert runtime.scheduler.get_job('42') is original
            assert runtime.scheduler.get_jobs() == original_jobs
            if original is not None:
                assert str(original.trigger.timezone) == 'America/New_York'
    async with runtime.sessions() as db:
        job = (await db.scalars(select(SysJob))).first()
        if commit_fails and action == 'add':
            assert job is None
            assert await db.scalar(select(func.count()).select_from(SysJobSync)) == 0
            assert runtime.scheduler.get_jobs() == []
        if action == 'disable' and not commit_fails:
            assert job.time_zone == 'America/New_York'
        if commit_fails and action != 'add':
            assert job.time_zone == 'America/New_York'
            assert job.status == '0'
            state = await db.get(SysJobSync, 42)
            assert state.sync_status == 'applied'
            assert state.config_version == state.applied_version == 1
        if not commit_fails:
            state = await db.get(SysJobSync, job_id)
            assert state.sync_status == 'pending'
            assert state.config_version == expected_version
            assert state.applied_version == expected_version - 1
            assert state.deleted == (action == 'delete')

    if commit_fails:
        return

    # 只有显式模拟独立调度Worker校准后，本地调度和数据库版本才生效。
    applied = await SchedulerManager._sync_jobs_from_database({job_id})
    assert applied['syncStatus'] == 'applied'
    assert len(applied['jobs']) == 1
    assert applied['jobs'][0]['configVersion'] == applied['jobs'][0]['appliedVersion'] == expected_version
    async with runtime.sessions() as db:
        state = await db.get(SysJobSync, job_id)
        assert state.sync_status == 'applied'
        assert state.config_version == state.applied_version == expected_version
        assert state.deleted == (action == 'delete')
    scheduled = runtime.scheduler.get_job(str(job_id))
    assert (scheduled is None) == (action in ('disable', 'delete'))
    if scheduled is not None:
        assert str(scheduled.trigger.timezone) == ('UTC' if action == 'edit' else 'America/New_York')


@pytest.mark.asyncio
async def test_sync_failure_after_commit_reports_saved_and_keeps_retryable_state(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    await runtime.save()
    await SchedulerManager._sync_jobs_from_database()
    original = runtime.scheduler.get_job('42')
    request_sync = AsyncMock(side_effect=RuntimeError('sync unavailable'))
    monkeypatch.setattr(SchedulerCommandClient, 'request_scheduler_sync', request_sync)
    async with runtime.sessions() as db:
        result = await JobService.delete_job_services(db, DeleteJobModel(jobIds='42'))
    assert result.result['saved'] is True
    assert result.result['syncStatus'] == 'pending'
    assert '删除已提交' in result.message
    assert any(warning in result.message for warning in ('等待', '通知', '重试'))
    if result.result.get('notificationError') is not None:
        assert 'sync unavailable' in result.result['notificationError']
    request_sync.assert_awaited_once_with({42})
    assert runtime.scheduler.get_job('42') is original
    assert len(result.result['jobs']) == 1
    assert result.result['jobs'][0]['syncStatus'] == 'pending'
    assert result.result['jobs'][0]['configVersion'] == 2
    assert result.result['jobs'][0]['appliedVersion'] == 1
    async with runtime.sessions() as db:
        assert (await db.scalars(select(SysJob))).first() is None
        state = await db.get(SysJobSync, 42)
        assert state.deleted
        assert state.sync_status == 'pending'
        assert state.config_version == 2
        assert state.applied_version == 1

    applied = await SchedulerManager._sync_jobs_from_database({42})
    assert applied['syncStatus'] == 'applied'
    assert runtime.scheduler.get_job('42') is None
    async with runtime.sessions() as db:
        state = await db.get(SysJobSync, 42)
        assert state.deleted
        assert state.sync_status == 'applied'
        assert state.config_version == state.applied_version == 2


@pytest.mark.asyncio
async def test_disable_broken_target_remains_possible_but_enable_is_rejected(runtime: Runtime) -> None:
    await runtime.save(job_info(invokeTarget='module_task.scheduler_test.missing'))
    async with runtime.sessions() as db:
        result = await JobService.edit_job_services(db, EditJobModel(jobId=42, status='1', type='status'))
    assert result.result['saved'] is True
    assert result.result['syncStatus'] == 'pending'
    assert result.result['jobs'][0]['syncStatus'] == 'pending'
    assert runtime.scheduler.get_job('42') is None
    async with runtime.sessions() as db:
        state = await db.get(SysJobSync, 42)
        assert state.sync_status == 'pending'
        assert state.config_version == 2
        assert state.applied_version == 0
        assert (await JobDao.get_job_detail_by_id(db, 42)).status == '1'

    applied = await SchedulerManager._sync_jobs_from_database({42})
    assert applied['syncStatus'] == 'applied'
    assert runtime.scheduler.get_job('42') is None
    async with runtime.sessions() as db:
        state = await db.get(SysJobSync, 42)
        assert state.sync_status == 'applied'
        assert state.config_version == state.applied_version == 2
    async with runtime.sessions() as db:
        with pytest.raises(ServiceException):
            await JobService.edit_job_services(db, EditJobModel(jobId=42, status='0', type='status'))
        assert (await JobDao.get_job_detail_by_id(db, 42)).status == '1'


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_fails', [False, True])
async def test_run_once_notifies_dispatch_only_after_commit_without_local_execution(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, commit_fails: bool
) -> None:
    await runtime.save()
    events = []
    async with runtime.sessions() as db:
        commit = db.commit

        async def committing() -> None:
            assert runtime.scheduler.get_jobs() == []
            events.append('commit')
            if commit_fails:
                raise RuntimeError('transaction failed')
            await commit()

        async def notify() -> None:
            assert events == ['commit']
            assert runtime.scheduler.get_jobs() == []
            async with runtime.sessions() as committed_db:
                execution = (await committed_db.scalars(select(SysJobExecution))).one()
                assert execution.job_id == 42
                assert execution.status == 'pending'
                assert execution.requested_by == 'operator'
            events.append('notify')

        request_dispatch = AsyncMock(side_effect=notify)
        monkeypatch.setattr(db, 'commit', committing)
        monkeypatch.setattr(SchedulerCommandClient, 'request_execution_dispatch', request_dispatch)
        if commit_fails:
            with pytest.raises(RuntimeError, match='transaction failed'):
                await JobService.execute_job_once_services(db, JobModel(jobId=42), requested_by='operator')
            assert events == ['commit']
            request_dispatch.assert_not_awaited()
        else:
            result = await JobService.execute_job_once_services(db, JobModel(jobId=42), requested_by='operator')
            assert result.result['status'] == 'pending'
            assert result.result['jobId'] == 42
            assert result.result['requestedBy'] == 'operator'
            assert events == ['commit', 'notify']
            request_dispatch.assert_awaited_once_with()

    assert runtime.scheduler.get_jobs() == []
    async with runtime.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SysJobExecution)) == (0 if commit_fails else 1)
        if not commit_fails:
            execution = await db.get(SysJobExecution, result.result['executionId'])
            assert execution.status == 'pending'


@pytest.mark.asyncio
async def test_add_job_rejects_plugin_invoke_target_from_system_job_form() -> None:
    """
    校验系统定时任务入口不会放开到整个 plugins 包。

    :return: None
    """
    job = JobModel(
        jobName='plugin-core-task',
        jobGroup='default',
        jobExecutor='default',
        invokeTarget='plugins.core.lifecycle.jobs.PluginJobInstaller.pause_plugin_jobs',
        cronExpression='0/5 * * * * ?',
        misfireGraceTime=1,
        maxInstances=1,
        status='1',
    )

    with pytest.raises(ServiceException) as exc_info:
        await JobService.add_job_services(object(), job)

    assert '目标字符串不在白名单内' in exc_info.value.message
