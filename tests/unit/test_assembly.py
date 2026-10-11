# tests/unit/test_assembly.py
"""backtest/assembly.py 撮合执行器工厂注册表测试（P0-00 子步骤 A 第一刀，R-19 新模块必配）。

覆盖：
  - 注册/读取/清除全局工厂（register_broker_factory + get_broker_factory）
  - 防御式：非 callable 工厂抛 TypeError；None 工厂合法（清除语义）
  - 与 engine._resolve_broker 集成：未装配显式 ValueError、装配后返回工厂产物
  - 隔离：每个用例后复位全局注册表，避免污染其他测试
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import pytest

import myquant.backtest.assembly as assembly
from myquant.backtest.engine import BacktestEngine
from myquant.models.backtest import BacktestConfig


@pytest.fixture(autouse=True)
def _reset_factory():
    """每个用例前后复位全局注册表（隔离模块级状态）。"""
    assembly._broker_factory = None
    yield
    assembly._broker_factory = None


def test_default_unregistered_is_none():
    assert assembly.get_broker_factory() is None


def test_register_and_read():
    def fake_factory(config: BacktestConfig) -> str:
        return "broker-A"

    assembly.register_broker_factory(fake_factory)
    assert assembly.get_broker_factory() is fake_factory


def test_register_none_clears():
    def fake_factory(config: BacktestConfig) -> str:
        return "broker-B"

    assembly.register_broker_factory(fake_factory)
    assembly.register_broker_factory(None)
    assert assembly.get_broker_factory() is None


def test_register_non_callable_raises():
    with pytest.raises(TypeError, match="必须可调用"):
        assembly.register_broker_factory("not-a-callable")  # type: ignore[arg-type]


def test_resolve_broker_unregistered_raises():
    """_resolve_broker 未装配（实例级+全局均无）显式 ValueError。"""
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._broker_factory = None
    with pytest.raises(ValueError, match="需要撮合执行器工厂"):
        engine._resolve_broker(BacktestConfig())


def test_resolve_broker_uses_global_factory():
    """_resolve_broker 经全局注册表装配后返回工厂产物。"""
    def fake_factory(config: BacktestConfig) -> dict:
        return {"fake": True}

    assembly.register_broker_factory(fake_factory)
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._broker_factory = None
    broker = engine._resolve_broker(BacktestConfig())
    assert broker == {"fake": True}


def test_resolve_broker_instance_overrides_global():
    """实例级工厂优先于全局注册表。"""
    def global_factory(config: BacktestConfig) -> str:
        return "global"

    def instance_factory(config: BacktestConfig) -> str:
        return "instance"

    assembly.register_broker_factory(global_factory)
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._broker_factory = instance_factory
    assert engine._resolve_broker(BacktestConfig()) == "instance"


def test_resolve_broker_factory_returning_none_raises():
    """工厂返回 None 时 _resolve_broker 显式 ValueError。"""
    assembly.register_broker_factory(lambda config: None)
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._broker_factory = None
    with pytest.raises(ValueError, match="返回了 None"):
        engine._resolve_broker(BacktestConfig())


def test_reexport_register_broker_factory_from_engine():
    """engine.py re-export register_broker_factory 与 assembly 同一对象（外部调用方零改动）。"""
    from myquant.backtest import engine as engine_mod
    assert engine_mod.register_broker_factory is assembly.register_broker_factory
