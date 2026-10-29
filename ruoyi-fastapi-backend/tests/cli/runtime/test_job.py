import builtins
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli.output import OutputRenderer
from cli.runtime.job import JobRuntimeService
from cli.runtime.job.gateway import JobInfrastructureGateway
from cli.runtime.job.support import JobDomainSupport
from common.vo import CrudResponseModel

MISSING_JOB_ID = 99


@pytest.mark.parametrize(
    ('operation', 'label', 'message'),
    [
        ('run-once', '执行一次任务', '定时任务执行请求已提交，等待 Worker 执行'),
        ('pause', '暂停任务', '定时任务暂停请求已提交，等待 Worker 应用'),
        ('resume', '恢复任务', '定时任务恢复请求已提交，等待 Worker 应用'),
    ],
)
def test_job_domain_support_builds_operation_metadata(operation: str, label: str, message: str) -> None:
    """
    校验任务领域支持对象会返回统一的任务操作元数据。

    :return: None
    """
    support = JobDomainSupport(JobInfrastructureGateway())

    payload = support.build_job_operation_metadata(operation)

    assert payload == {
        'operationLabel': label,
        'successMessage': message,
    }


def test_job_domain_support_serializes_model_dump_items() -> None:
    """
    校验任务领域支持对象会序列化带 `model_dump` 的任务项。

    :return: None
    """

    class FakeJobModel:
        """
        模拟任务模型。
        """

        @staticmethod
        def model_dump(*, by_alias: bool = False, exclude_none: bool = False) -> dict[str, object]:
            """
            返回模拟序列化结果。

            :param by_alias: 是否按别名输出
            :param exclude_none: 是否排除空值
            :return: 模拟序列化结果
            """
            assert by_alias is True
            assert exclude_none is True
            return {'jobId': 1, 'jobName': 'sync-job'}

    support = JobDomainSupport(JobInfrastructureGateway())

    payload = support.serialize_job_item(FakeJobModel())

    assert payload == {'jobId': 1, 'jobName': 'sync-job'}


def test_job_gateway_loads_only_notification_client_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    网关实例化不加载调度器，显式获取时只加载通知客户端。

    :return: None
    """
    client = object()
    load_module = MagicMock(return_value=SimpleNamespace(SchedulerCommandClient=client))
    monkeypatch.setattr('cli.runtime.job.gateway.import_module', load_module)
    gateway = JobInfrastructureGateway()

    load_module.assert_not_called()
    assert gateway.get_scheduler_client() is client
    load_module.assert_called_once_with('config.scheduler.client')
    assert not hasattr(gateway, 'get_scheduler_manager')
    assert not hasattr(gateway, 'get_redis_util')


@pytest.fixture
def forbid_scheduler_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止普通命令及帮助路径导入任何调度运行时。"""
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == 'config.scheduler' or name.startswith('config.scheduler.'):
            raise AssertionError(f'普通 CLI 命令不得导入调度运行时：{name}')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(
        'cli.runtime.job.gateway.import_module',
        MagicMock(side_effect=AssertionError('任务操作只能使用已注入的数据库和任务服务')),
    )


@pytest.mark.asyncio
async def test_job_runtime_service_reports_missing_job_detail() -> None:
    """
    校验任务运行时在任务详情不存在时会返回统一结果。

    :return: None
    """

    class FakeSession:
        """
        模拟异步会话。
        """

        async def __aenter__(self) -> 'FakeSession':
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: object,
        ) -> None:
            del exc_type, exc, tb

    class FakeSessionFactory:
        """
        模拟异步会话工厂。
        """

        def __call__(self) -> FakeSession:
            return FakeSession()

    class FakeJobService:
        """
        模拟任务服务。
        """

        @staticmethod
        async def job_detail_services(session: FakeSession, job_id: int) -> dict[str, object]:
            """
            返回不存在任务的空结果。

            :param session: 数据库会话
            :param job_id: 任务 ID
            :return: 空结果
            """
            assert isinstance(session, FakeSession)
            assert job_id == MISSING_JOB_ID
            return {}

    gateway = JobInfrastructureGateway()
    service = JobRuntimeService(infrastructure_gateway=gateway)

    def _fake_get_async_session_local() -> FakeSessionFactory:
        return FakeSessionFactory()

    def _fake_get_job_service() -> FakeJobService:
        return FakeJobService()

    object.__setattr__(gateway, 'get_async_session_local', _fake_get_async_session_local)
    object.__setattr__(gateway, 'get_job_service', _fake_get_job_service)

    payload = await service.get_job_detail(MISSING_JOB_ID)

    assert payload['ok'] is False
    assert payload['jobId'] == MISSING_JOB_ID
    assert payload['message'] == f'定时任务不存在：{MISSING_JOB_ID}'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['run-once', 'pause', 'resume'])
@pytest.mark.parametrize('service_message', ['已提交，等待调度器处理', ''])
async def test_job_operation_retains_execution_and_sync_results(
    operation: str,
    service_message: str,
    capsys: pytest.CaptureFixture[str],
    forbid_scheduler_imports: None,
) -> None:
    del forbid_scheduler_imports
    result = (
        {'executionId': 'execution-123', 'status': 'pending'}
        if operation == 'run-once'
        else {'saved': True, 'synced': False, 'jobs': [{'jobId': 7, 'syncStatus': 'pending'}]}
    )
    response = CrudResponseModel(is_success=True, message=service_message, result=result)
    job_service = SimpleNamespace(
        execute_job_once_services=AsyncMock(return_value=response),
        edit_job_services=AsyncMock(return_value=response),
    )
    gateway = MagicMock(spec_set=JobInfrastructureGateway)
    session_context = AsyncMock()
    session = session_context.__aenter__.return_value
    gateway.get_async_session_local.return_value = MagicMock(return_value=session_context)
    gateway.get_job_service.return_value = job_service
    service = JobRuntimeService(infrastructure_gateway=gateway)
    method_name = {'run-once': 'run_job_once', 'pause': 'pause_job', 'resume': 'resume_job'}[operation]
    payload = await getattr(service, method_name)(7)
    expected_status = {'run-once': None, 'pause': '1', 'resume': '0'}[operation]
    assert payload['ok'] is True
    assert payload['jobId'] == 7
    assert payload['operation'] == operation
    assert payload['operationLabel'] == service.domain_support.build_job_operation_metadata(operation)['operationLabel']
    assert payload['targetStatus'] == expected_status
    assert payload['result'] == result
    assert payload['serviceMessage'] == response.message
    assert payload['message'] == (
        response.message or service.domain_support.build_job_operation_metadata(operation)['successMessage']
    )
    job_vo_module = gateway.get_job_vo_module.return_value
    if operation == 'run-once':
        job_vo_module.JobModel.assert_called_once_with(jobId=7)
        job_service.execute_job_once_services.assert_awaited_once_with(session, job_vo_module.JobModel.return_value)
        job_service.edit_job_services.assert_not_awaited()
    else:
        job_vo_module.EditJobModel.assert_called_once_with(jobId=7, status=expected_status, type='status')
        job_service.edit_job_services.assert_awaited_once_with(session, job_vo_module.EditJobModel.return_value)
        job_service.execute_job_once_services.assert_not_awaited()
    OutputRenderer().emit_output(payload, 'json')
    assert json.loads(capsys.readouterr().out)['result'] == result
    text = OutputRenderer().render_text_output(payload)
    if operation == 'run-once':
        assert 'execution-123' in text
        assert 'pending' in text
    else:
        assert 'saved' in text and 'synced' in text
    session_context.__aexit__.assert_awaited_once()
    # JobService 已负责提交后的通知，CLI 不重复通知，也不绑定或关闭资源。
    gateway.get_scheduler_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['run-once', 'pause', 'resume'])
async def test_job_operation_failure_only_closes_database_session(
    operation: str, forbid_scheduler_imports: None
) -> None:
    del forbid_scheduler_imports
    gateway = MagicMock(spec_set=JobInfrastructureGateway)
    session_context = AsyncMock()
    gateway.get_async_session_local.return_value = MagicMock(return_value=session_context)
    job_service = SimpleNamespace(
        execute_job_once_services=AsyncMock(side_effect=RuntimeError('database unavailable')),
        edit_job_services=AsyncMock(side_effect=RuntimeError('database unavailable')),
    )
    gateway.get_job_service.return_value = job_service
    service = JobRuntimeService(infrastructure_gateway=gateway)

    payload = await service.run_job_operation(operation, 7)

    assert payload['ok'] is False
    assert payload['error'] == 'database unavailable'
    assert payload['exit_code'] == 22
    session_context.__aexit__.assert_awaited_once()
    gateway.get_scheduler_client.assert_not_called()


@pytest.mark.parametrize('arguments', [['--help'], ['worker', '--help']])
def test_job_help_does_not_import_scheduler(arguments: list[str], forbid_scheduler_imports: None) -> None:
    del forbid_scheduler_imports
    from typer.testing import CliRunner

    from cli.groups.job.command import app

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert 'worker' in result.output


def test_job_worker_command_delegates_environment_without_loading_runtime(
    monkeypatch: pytest.MonkeyPatch, forbid_scheduler_imports: None
) -> None:
    del forbid_scheduler_imports
    from typer.testing import CliRunner

    from cli.groups.job import command

    controller = SimpleNamespace(worker=MagicMock())
    monkeypatch.setattr(command, '_JOB_COMMAND_CONTROLLER', controller)

    result = CliRunner().invoke(command.app, ['worker', '--env=dockerpg'])

    assert result.exit_code == 0
    controller.worker.assert_called_once_with('dockerpg')


def test_job_worker_prepares_environment_before_lazy_import(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.groups.job.controller import JobCommandController

    events: list[str] = []
    builder = SimpleNamespace(build=MagicMock(side_effect=lambda *args: events.append('environment')))
    context_factory = MagicMock(cli_context_builder=builder)
    run_worker = MagicMock(side_effect=lambda: events.append('worker'))
    monkeypatch.setitem(sys.modules, 'config.scheduler.worker', SimpleNamespace(run_worker=run_worker))
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == 'config.scheduler.worker':
            assert events == ['environment']
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    controller = JobCommandController(context_factory=context_factory)

    controller.worker('dockerpg')

    builder.build.assert_called_once_with('dockerpg', 'text', False, False, False)
    context_factory.get_log_policy.assert_not_called()
    run_worker.assert_called_once_with()
    assert events == ['environment', 'worker']
