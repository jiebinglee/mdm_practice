import ast
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CONFIGURATION_COUNT = 4


def test_frontend_tree_and_generator_only_offer_vue3() -> None:
    """工程目录及代码生成界面不能再暴露已删除的框架。"""
    frontend = ROOT / 'ruoyi-fastapi-frontend'
    assert {directory.name for directory in frontend.iterdir() if directory.is_dir()} == {'vue3'}
    form = (frontend / 'vue3/web/src/views/tool/gen/genInfoForm.vue').read_text(encoding='utf-8')
    assert 'value="element-plus"' in form
    assert 'value="element-ui"' not in form
    assert 'react-antd' not in form


def load_script(name: str) -> ModuleType:
    """
    按文件路径加载 CI 脚本，避免依赖仓库外的模块搜索路径。

    :param name: 不含扩展名的脚本名称
    :return: 已执行模块顶层定义的脚本模块
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / '.github/scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compose = load_script('check_compose')
reports = load_script('check_test_report')


@pytest.fixture
def engine(request: pytest.FixtureRequest) -> str:
    """选择 MySQL 或 PostgreSQL 的 Compose 后缀。"""
    return request.param


@pytest.fixture
def configuration(engine: str) -> dict[str, Any]:
    """提供前端、nginx 和后端框架一致的 Compose 配置。"""
    frontend = ROOT / 'ruoyi-fastapi-frontend/vue3/web'
    return {
        'services': {
            'ruoyi-frontend': {
                'build': {'context': str(frontend), 'dockerfile': 'Dockerfile'},
                'volumes': [
                    {
                        'source': str(frontend / f'bin/nginx.docker{engine}.conf'),
                        'target': '/etc/nginx/conf.d/default.conf',
                    }
                ],
            },
            f'ruoyi-backend-{engine}': {'environment': {'RUOYI_PLUGIN_FRONTEND_FRAMEWORK': 'vue3'}},
            f'ruoyi-scheduler-{engine}': {'environment': {'RUOYI_PLUGIN_FRONTEND_FRAMEWORK': 'vue3'}},
        }
    }


@pytest.mark.parametrize('engine', ['my', 'pg'], indirect=True)
def test_compose_accepts_vue3_deployment(configuration: dict[str, Any], engine: str) -> None:
    """两类数据库的 Vue3 Compose 配置均通过校验。"""
    compose.validate_configuration(configuration, 'vue3', engine, ROOT)


@pytest.mark.parametrize('target', ['web', 'mobile'])
def test_frontend_ci_commands_exist_in_every_project(target: str) -> None:
    """矩阵中的工程必须实际提供工作流调用的测试和构建命令。"""
    directory = ROOT / 'ruoyi-fastapi-frontend/vue3' / target
    package = json.loads((directory / 'package.json').read_text(encoding='utf-8'))
    required = {'test:unit', 'test:contract'}
    required.update({'build:prod', 'build:docker'} if target == 'web' else {'build:h5', 'build:mp-weixin'})
    assert package['packageManager'] == 'pnpm@11.28.6'
    assert (directory / 'pnpm-workspace.yaml').is_file()
    assert (directory / 'pnpm-lock.yaml').is_file()
    assert required <= package['scripts'].keys()


@pytest.mark.parametrize('engine', ['my', 'pg'], indirect=True)
@pytest.mark.parametrize('component', ['build', 'nginx', 'backend', 'scheduler'])
def test_compose_rejects_non_vue3_deployment(configuration: dict[str, Any], engine: str, component: str) -> None:
    """前端、nginx、后端和 Worker 均不得选择其他框架。"""
    invalid = deepcopy(configuration)
    if component == 'build':
        invalid['services']['ruoyi-frontend']['build']['context'] = str(ROOT / 'ruoyi-fastapi-frontend/obsolete/web')
    elif component == 'nginx':
        invalid['services']['ruoyi-frontend']['volumes'][0]['source'] = str(
            ROOT / f'ruoyi-fastapi-frontend/obsolete/web/bin/nginx.docker{engine}.conf'
        )
    else:
        service = f'ruoyi-{component}-{engine}'
        invalid['services'][service]['environment']['RUOYI_PLUGIN_FRONTEND_FRAMEWORK'] = 'obsolete'
    with pytest.raises(ValueError):
        compose.validate_configuration(invalid, 'vue3', engine, ROOT)


@pytest.mark.parametrize('engine', ['my', 'pg'], indirect=True)
def test_compose_rejects_unsupported_framework_argument(configuration: dict[str, Any], engine: str) -> None:
    """校验器不能通过传入其他框架名称恢复多框架部署。"""
    with pytest.raises(ValueError, match='仅支持 Vue3'):
        compose.validate_configuration(configuration, 'obsolete', engine, ROOT)


@pytest.mark.parametrize('engine', ['my', 'pg'])
def test_production_compose_paths_and_all_backend_roles_are_fixed_to_vue3(engine: str) -> None:
    """生产部署不再通过环境插值选择框架，Web 和 Worker 环境保持一致。"""
    config = yaml.safe_load((ROOT / f'docker-compose.{engine}.yml').read_text(encoding='utf-8'))
    services = config['services']
    frontend = services['ruoyi-frontend']
    assert frontend['build']['context'] == './ruoyi-fastapi-frontend/vue3/web'
    assert frontend['build']['dockerfile'] == 'Dockerfile'
    assert frontend['volumes'] == [
        f'./ruoyi-fastapi-frontend/vue3/web/bin/nginx.docker{engine}.conf:/etc/nginx/conf.d/default.conf'
    ]
    for role in ('backend', 'scheduler'):
        assert services[f'ruoyi-{role}-{engine}']['environment']['RUOYI_PLUGIN_FRONTEND_FRAMEWORK'] == 'vue3'


def test_compose_cli_only_checks_vue3_for_all_production_and_test_configurations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实际命令入口仅运行四组 Vue3 生产及测试 Compose 校验。"""
    calls = []
    validated = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """记录 Docker 调用并返回隔离配置。"""
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='{}')

    monkeypatch.setattr(compose.subprocess, 'run', run)
    monkeypatch.setattr(compose, 'validate_configuration', lambda *args: validated.append(args))
    monkeypatch.setattr(sys, 'argv', ['check_compose.py'])
    compose.main()
    assert len(calls) == len(compose.CONFIGURATIONS) == COMPOSE_CONFIGURATION_COUNT
    for (command, kwargs), (env_file, compose_file, engine), validation in zip(
        calls, compose.CONFIGURATIONS, validated, strict=True
    ):
        assert command == [
            'docker',
            'compose',
            '--env-file',
            env_file,
            '-f',
            compose_file,
            'config',
            '--format',
            'json',
        ]
        assert kwargs['env']['FRONTEND_FRAMEWORK'] == 'vue3'
        assert validation == ({}, 'vue3', engine, ROOT)


def read_workflow(name: str) -> dict[str, Any]:
    """读取工作流配置以约束步骤、复用输入和前端路径。"""
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'))


def test_frontend_workflow_keeps_vue3_tests_builds_and_mobile_smoke() -> None:
    """移除旧框架输入后仍保留两个 Node 版本及 Vue3 全部核心验证。"""
    workflow = read_workflow('ci-frontend.yml')
    assert set(workflow[True]['workflow_call']['inputs']) == {'matrix_json'}
    job = workflow['jobs']['frontend']
    assert job['strategy']['matrix']['node'] == ['22', '24']
    assert job['strategy']['matrix']['project'] == '${{ fromJSON(inputs.matrix_json).include }}'
    assert job['defaults']['run']['working-directory'] == 'ruoyi-fastapi-frontend/vue3/${{ matrix.project.target }}'
    steps = job['steps']
    runs = '\n'.join(step.get('run', '') for step in steps)
    for command in ('test:unit', 'test:contract', 'build:prod', 'build:docker', 'build:h5', 'build:mp-weixin'):
        assert f'pnpm run {command}' in runs
    for command in ('build:h5', 'build:mp-weixin'):
        step = next(step for step in steps if step.get('run') == f'pnpm run {command}')
        assert step['if'] == "matrix.project.target == 'mobile'"
    smoke = next(step for step in steps if 'mobile_smoke.py' in step.get('run', ''))
    assert smoke['run'] == 'python .github/scripts/mobile_smoke.py --framework vue3'
    assert smoke['if'] == "matrix.project.target == 'mobile'"
    assert read_workflow('ci.yml')['jobs']['frontend']['with'] == {
        'matrix_json': '${{ needs.plan.outputs.frontend_matrix }}'
    }


def test_prettier_setup_and_formatting_only_use_vue3_projects() -> None:
    """格式化工具从 Vue3 package.json 设置，且仅检查 Vue3 web/mobile。"""
    steps = read_workflow('ci-quality.yml')['jobs']['checks']['steps']
    setup = next(step for step in steps if step.get('uses') == 'pnpm/action-setup@v5')
    assert setup['with']['package_json_file'] == 'ruoyi-fastapi-frontend/vue3/web/package.json'
    formatting = next(step['run'] for step in steps if step.get('name') == 'Check frontend formatting')
    assert 'for target in web mobile; do' in formatting
    assert 'ruoyi-fastapi-frontend/vue3/$target' in formatting
    assert 'pnpm dlx prettier@3.9.10 --check .' in formatting
    assert 'for framework' not in formatting


def test_e2e_and_network_workflows_pin_vue3_paths_and_backend_environment() -> None:
    """浏览器和网络验收不能通过工作流参数重新选择旧框架。"""
    browser = read_workflow('ci-e2e.yml')['jobs']['browser']
    assert browser['env']['FRONTEND_FRAMEWORK'] == 'vue3'
    build = next(step for step in browser['steps'] if step.get('uses') == 'docker/build-push-action@v6')
    assert build['with']['context'] == 'ruoyi-fastapi-frontend/vue3/web'
    assert build['with']['file'] == 'ruoyi-fastapi-frontend/vue3/web/Dockerfile'
    generate = next(step['run'] for step in browser['steps'] if step.get('name') == 'Generate isolated Compose project')
    assert '--framework vue3' in generate
    network = read_workflow('ci-integration.yml')['jobs']['network']
    assert network['if'] == "inputs.network_frameworks != '[]'"
    assert network['strategy']['matrix']['framework'] == ['vue3']
    assert network['env']['RUOYI_PLUGIN_FRONTEND_FRAMEWORK'] == 'vue3'


def test_mobile_smoke_only_accepts_vue3_and_uses_its_h5_build() -> None:
    """不安装浏览器依赖也能约束 smoke 的 CLI 和静态构建入口。"""
    tree = ast.parse((ROOT / '.github/scripts/mobile_smoke.py').read_text(encoding='utf-8'))
    arguments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'add_argument'
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == '--framework'
    ]
    (argument,) = arguments
    options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in argument.keywords}
    assert options == {'choices': ('vue3',), 'default': 'vue3'}
    (distribution,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'distribution' for target in node.targets)
    ]
    assert ast.unparse(distribution.value) == "ROOT / 'ruoyi-fastapi-frontend/vue3/mobile/dist/build/h5'"


@pytest.mark.parametrize(
    'cases',
    ['', '<testcase><skipped /></testcase>', '<testcase><failure /></testcase>', '<testcase><error /></testcase>'],
)
def test_report_rejects_empty_skipped_or_failed_verification(tmp_path: Path, cases: str) -> None:
    """空报告、全部跳过和失败用例都不能视为验收成功。"""
    report = tmp_path / 'results.xml'
    report.write_text(f'<testsuites><testsuite>{cases}</testsuite></testsuites>', encoding='utf-8')
    with pytest.raises(ValueError):
        reports.check_report(report)


def test_expected_platform_skip_still_requires_executed_tests(tmp_path: Path) -> None:
    """允许预期平台跳过时仍要求实际执行测试。"""
    report = tmp_path / 'results.xml'
    report.write_text(
        '<testsuites><testsuite><testcase/><testcase><skipped/></testcase></testsuite></testsuites>', encoding='utf-8'
    )
    assert reports.check_report(report, allow_skips=True) == (1, 1)


def test_missing_report_is_not_success(tmp_path: Path) -> None:
    """报告缺失时不能视为验收成功。"""
    with pytest.raises(FileNotFoundError):
        reports.check_report(tmp_path / 'missing.xml')
