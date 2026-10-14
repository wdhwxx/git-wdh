# 文件名：tests/unit/test_core_registry.py
"""
test_core_registry.py - G2-1 core/registry 注册表单测
====================================================
覆盖 register_collector / get_collector 的注册-读取-未注册降级语义，
消除 myquant/core/registry.py 的引用级零覆盖（R-19）。
"""
from __future__ import annotations

from typing import Any

import pytest

from myquant.core.registry import get_collector, register_collector


class _FakeCollector:
    """指标采集器桩（仅验证注册表持有与返回）。"""


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    """每个用例前清空注册表（防用例间污染）。"""
    register_collector(None)
    yield
    register_collector(None)


class TestCoreRegistry:
    """core/registry 注册表行为。"""

    def test_未注册返回_none(self) -> None:
        """未注册时 get_collector 返回 None（使用方降级不写指标）。"""
        assert get_collector() is None

    def test_注册后返回同一实例(self) -> None:
        """register 后再 get 返回同一实例（懒创建单例自注册语义）。"""
        inst = _FakeCollector()
        register_collector(inst)
        assert get_collector() is inst

    def test_清空后返回_none(self) -> None:
        """register(None) 清空后 get 返回 None。"""
        register_collector(_FakeCollector())
        register_collector(None)
        assert get_collector() is None

    def test_重复注册覆盖(self) -> None:
        """重复注册以最新实例为准（装配方重装配场景）。"""
        a = _FakeCollector()
        b = _FakeCollector()
        register_collector(a)
        register_collector(b)
        assert get_collector() is b
