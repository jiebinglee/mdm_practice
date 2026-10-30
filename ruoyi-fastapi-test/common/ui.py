import re


def button_name(label: str) -> re.Pattern[str]:
    """按文字后缀匹配 Vue3 按钮，容许图标前缀及文字间空白。"""
    text = r'\s*'.join(re.escape(character) for character in label)
    return re.compile(rf'{text}\s*$')
