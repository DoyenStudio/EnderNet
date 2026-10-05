# coding=utf-8
"""万用样板采集器的进程内结果；只有显式保存才替换内容。"""

from copy import deepcopy


# 保持对象身份不变，允许其他客户端模块直接持有这个字典。
# GetUiItem 不提供数量或完整 NBT；每一项代表一个被选中的渲染控件。
SAVED_CONFIG = {"inputs": [], "outputs": []}
_HAS_CONFIG = False
_VERSION = 0


def save_config(inputs, outputs):
    # type: (list[dict], list[dict]) -> None
    global _HAS_CONFIG, _VERSION
    snapshot = deepcopy({"inputs": inputs, "outputs": outputs})
    SAVED_CONFIG.clear()
    SAVED_CONFIG.update(snapshot)
    _HAS_CONFIG = True
    _VERSION += 1


def has_saved_config():
    # type: () -> bool
    """显式保存的空配置也算一份配置，可以用来清空终端虚格。"""
    return _HAS_CONFIG


def saved_config_version():
    # type: () -> int
    return _VERSION


def clear_saved_config(expected_version=None):
    # type: (object) -> bool
    """导入成功才消费；旧请求的回执不能清掉玩家后来保存的新配置。"""
    global _HAS_CONFIG, _VERSION
    if expected_version is not None and expected_version != _VERSION:
        return False
    SAVED_CONFIG.clear()
    SAVED_CONFIG.update({"inputs": [], "outputs": []})
    _HAS_CONFIG = False
    _VERSION += 1
    return True


def get_saved_config():
    # type: () -> dict
    """返回独立副本，调用者修改它不会改动已保存配置。"""
    return deepcopy(SAVED_CONFIG)
