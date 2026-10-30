import os


def frontend_framework() -> str:
    """保留历史环境变量入口，但只允许实际存在的 Vue3 工程。"""
    for name in ('TEST_FRONTEND_FRAMEWORK', 'FRONTEND_FRAMEWORK'):
        value = os.environ.get(name)
        if value is not None and value != 'vue3':
            raise ValueError(f'{name} only supports vue3, got {value!r}')
    return 'vue3'


class Config:
    frontend_url = os.environ.get('TEST_FRONTEND_URL', 'http://localhost:80')
    backend_url = os.environ.get('TEST_BACKEND_URL', 'http://localhost:9099')
    browser_channel = os.environ.get('TEST_BROWSER_CHANNEL') or None
    swagger_disabled = os.environ.get('TEST_SWAGGER_DISABLED', 'true').lower() == 'true'
    frontend_framework = frontend_framework()
