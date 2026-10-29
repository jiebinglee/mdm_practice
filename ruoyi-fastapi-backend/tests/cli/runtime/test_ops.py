import builtins
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli.exit_codes import SCHEDULER_ERROR
from cli.runtime.ops import OperationsRuntimeService
from cli.runtime.ops.gateway import OperationsInfrastructureGateway
from cli.runtime.ops.support import OperationsDependencyInspector, OperationsServerInfoSupport


def test_operations_dependency_inspector_reports_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    校验运维依赖检查器会报告缺失的必需依赖。

    :param monkeypatch: pytest monkeypatch 工具
    :return: None
    """
    inspector = OperationsDependencyInspector()
    monkeypatch.setattr(
        inspector,
        'read_package_version',
        lambda distribution_name: None if distribution_name == 'fastapi' else '1.0.0',
    )

    payload = inspector.inspect()

    assert payload['ok'] is False
    assert payload['missingRequired'] == ['fastapi']
    assert payload['packages']['python']['installed'] is True


def test_operations_server_info_support_resolves_non_loopback_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    校验服务器信息支持对象会在主机名解析失败时回退到网卡地址。

    :param monkeypatch: pytest monkeypatch 工具
    :return: None
    """

    class FakePsutil:
        """
        模拟 psutil 模块。
        """

        @staticmethod
        def net_if_addrs() -> dict[str, list[SimpleNamespace]]:
            """
            返回模拟网卡地址。

            :return: 模拟网卡地址映射
            """
            return {
                'en0': [
                    SimpleNamespace(family=socket.AF_INET, address='127.0.0.1'),
                    SimpleNamespace(family=socket.AF_INET, address='192.168.1.8'),
                ]
            }

    gateway = OperationsInfrastructureGateway()
    support = OperationsServerInfoSupport(gateway)

    def _fake_get_psutil_module() -> FakePsutil:
        return FakePsutil()

    def _fake_gethostbyname(hostname: str) -> str:
        del hostname
        raise OSError('boom')

    monkeypatch.setattr(gateway, 'get_psutil_module', _fake_get_psutil_module)
    monkeypatch.setattr(socket, 'gethostbyname', _fake_gethostbyname)

    resolved_ip = support.resolve_server_ip('demo-host')

    assert resolved_ip == '192.168.1.8'


def test_operations_gateway_loads_only_notification_client_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object()
    load_module = MagicMock(return_value=SimpleNamespace(SchedulerCommandClient=client))
    monkeypatch.setattr('cli.runtime.ops.gateway.import_module', load_module)
    gateway = OperationsInfrastructureGateway()

    load_module.assert_not_called()
    assert gateway.get_scheduler_client() is client
    load_module.assert_called_once_with('config.scheduler.client')
    assert not hasattr(gateway, 'get_scheduler_manager')


@pytest.fixture
def forbid_scheduler_runtime_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == 'config.scheduler' or name.startswith('config.scheduler.'):
            raise AssertionError(f'同步通知不得导入调度运行时：{name}')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(
        'cli.runtime.ops.gateway.import_module',
        MagicMock(side_effect=AssertionError('同步通知只能使用已注入的客户端')),
    )


@pytest.mark.asyncio
async def test_sync_jobs_returns_pending_without_starting_or_closing_scheduler(
    forbid_scheduler_runtime_imports: None,
) -> None:
    del forbid_scheduler_runtime_imports
    pending = {'syncStatus': 'pending', 'jobs': []}
    # spec_set 保证 CLI 只能请求通知，不可绑定资源或控制调度器生命周期。
    client = MagicMock(spec_set=['request_scheduler_sync', 'request_execution_dispatch'])
    client.request_scheduler_sync = AsyncMock(return_value=pending)
    gateway = MagicMock(spec_set=OperationsInfrastructureGateway)
    gateway.get_scheduler_client.return_value = client
    service = OperationsRuntimeService(infrastructure_gateway=gateway)

    payload = await service.sync_jobs()

    assert payload['ok'] is True
    assert payload['schedulerSyncRequested'] is True
    assert payload['operation'] == 'sync'
    assert payload['operationLabel'] == '同步调度配置'
    assert payload['result'] == pending
    assert '等待 Worker 应用' in payload['message']
    assert '不保证送达' in payload['message']
    assert 'isLeader' not in payload
    client.request_scheduler_sync.assert_awaited_once_with()
    client.request_execution_dispatch.assert_not_called()
    gateway.get_redis_util.assert_not_called()
    gateway.get_scheduler_client.assert_called_once_with()


@pytest.mark.asyncio
async def test_sync_jobs_reports_client_failure_without_opening_resources(
    forbid_scheduler_runtime_imports: None,
) -> None:
    del forbid_scheduler_runtime_imports
    client = MagicMock(spec_set=['request_scheduler_sync'])
    client.request_scheduler_sync = AsyncMock(side_effect=RuntimeError('client unavailable'))
    gateway = MagicMock(spec_set=OperationsInfrastructureGateway)
    gateway.get_scheduler_client.return_value = client
    service = OperationsRuntimeService(infrastructure_gateway=gateway)

    payload = await service.sync_jobs()

    assert payload['ok'] is False
    assert payload['schedulerSyncRequested'] is False
    assert payload['error'] == 'client unavailable'
    assert payload['exit_code'] == SCHEDULER_ERROR
    assert 'isLeader' not in payload
    client.request_scheduler_sync.assert_awaited_once_with()
    gateway.get_redis_util.assert_not_called()
