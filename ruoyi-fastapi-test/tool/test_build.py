import asyncio
import re
from pathlib import Path

import pytest
from playwright.async_api import expect

from common.browser_harness import BrowserHarness
from common.config import Config
from common.ui import button_name

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_form_builder_page(browser_harness: BrowserHarness) -> None:
    """编辑真实设计器并校验导出的 Vue 组件内容。"""
    page = await browser_harness.new_page(authenticated=True)
    await page.goto(Config.frontend_url + '/tool/build')
    await page.get_by_role('button', name=button_name('清空')).click()
    await page.get_by_role('button', name=button_name('确定')).click()
    empty = page.locator('.empty-info')
    await expect(empty).to_be_visible()
    await page.locator('.components-item').filter(has_text='单行文本').click()
    board = page.locator('.drawing-board')
    await expect(board.locator('.el-form-item')).to_have_count(1)
    panel = page.locator('.right-board')
    await panel.get_by_placeholder('请输入字段名（v-model）', exact=True).fill('e2eCustomer')
    await panel.get_by_placeholder('请输入标题').fill('测试客户名称')
    await panel.get_by_placeholder('请输入占位提示', exact=True).fill('请填写测试客户')
    await expect(board.get_by_text('测试客户名称', exact=True)).to_be_visible()
    await expect(board.get_by_placeholder('请填写测试客户')).to_be_visible()
    await page.get_by_role('button', name=re.compile('导出vue文件', re.IGNORECASE)).click()
    dialog = page.get_by_role('dialog', name='选择生成类型')
    filename = 'e2e-customer.vue'
    await dialog.get_by_placeholder('请输入文件名').fill(filename)
    async with page.expect_download() as pending:
        await dialog.get_by_role('button', name=button_name('确定')).click()
    download = await pending.value
    assert download.suggested_filename == filename
    source = await asyncio.to_thread(Path(await download.path()).read_text, encoding='utf-8')
    assert re.search(r'<script(?:\s+setup)?>', source), '导出文件缺少 Vue 脚本'
    assert '<template>' in source and '</script>' in source
    for expected in ('e2eCustomer', '测试客户名称', '请填写测试客户'):
        assert expected in source, f'导出文件缺少 {expected}'
    await expect(dialog).to_be_hidden()
    await page.get_by_role('button', name=button_name('清空')).click()
    await page.get_by_role('button', name=button_name('确定')).click()
    await expect(board.locator('.el-form-item')).to_have_count(0)
    await expect(empty).to_be_visible()
